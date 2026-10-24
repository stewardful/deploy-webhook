package notify

import (
	"bufio"
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var testSecret = bytes.Repeat([]byte("s"), 32)

const testRepository = "stewardful/deploy-platform"

func testHandler(t *testing.T) (*Handler, *Store) {
	t.Helper()
	store, err := OpenStore(filepath.Join(t.TempDir(), "state"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	handler, err := NewHandler(Config{Repository: testRepository, Secret: testSecret}, store)
	if err != nil {
		t.Fatal(err)
	}
	return handler, store
}

func payload(tag string, prerelease bool) []byte {
	value := map[string]any{
		"action":     "published",
		"repository": map[string]any{"id": 100, "full_name": testRepository},
		"release": map[string]any{"id": 200, "tag_name": tag, "prerelease": prerelease,
			"draft": false, "published_at": "2026-10-25T02:00:00Z"},
	}
	data, _ := json.Marshal(value)
	return data
}

func delivery(number int) string { return fmt.Sprintf("00000000-0000-4000-8000-%012x", number) }

func signedRequest(body []byte, id string) *http.Request {
	request := httptest.NewRequest(http.MethodPost, "/hooks/github/releases", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-GitHub-Event", "release")
	request.Header.Set("X-GitHub-Delivery", id)
	mac := hmac.New(sha256.New, testSecret)
	_, _ = mac.Write(body)
	request.Header.Set("X-Hub-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	return request
}

func send(t *testing.T, handler http.Handler, body []byte, id string, code int) string {
	t.Helper()
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, signedRequest(body, id))
	if recorder.Code != code {
		t.Fatalf("got status %d, want %d: %s", recorder.Code, code, recorder.Body.String())
	}
	return recorder.Body.String()
}

func TestPublishedDevAndStableHints(t *testing.T) {
	h, store := testHandler(t)
	if !strings.Contains(send(t, h, payload("dev-42", true), delivery(1), 200), `"accepted":true`) {
		t.Fatal("not accepted")
	}
	send(t, h, payload("v1.9.99", false), delivery(2), 200)
	send(t, h, payload("v1.10.0", false), delivery(3), 200)
	snapshot := store.Snapshot()
	if snapshot.Revision != 3 || snapshot.Latest["dev"].Tag != "dev-42" || snapshot.Latest["stable"].Tag != "v1.10.0" {
		t.Fatalf("wrong snapshot: %+v", snapshot)
	}
	for _, path := range []string{"/healthz", "/v1/releases/latest"} {
		r := httptest.NewRecorder()
		h.ServeHTTP(r, httptest.NewRequest("GET", path, nil))
		if r.Code != 200 || strings.Contains(r.Body.String(), string(testSecret)) {
			t.Fatal("bad read response")
		}
	}
}

func TestSignatureMustMatchExactUnmodifiedBody(t *testing.T) {
	for _, signature := range []string{"", "sha1=invalid", "sha256=" + strings.Repeat("0", 64), "sha256=" + strings.Repeat("z", 64)} {
		t.Run(signature, func(t *testing.T) {
			h, store := testHandler(t)
			r := signedRequest(payload("dev-42", true), delivery(1))
			r.Header.Set("X-Hub-Signature-256", signature)
			out := httptest.NewRecorder()
			h.ServeHTTP(out, r)
			if out.Code != 401 || store.Snapshot().Revision != 0 {
				t.Fatal("invalid signature was accepted")
			}
		})
	}
	h, store := testHandler(t)
	r := signedRequest(payload("dev-42", true), delivery(1))
	r.Body = io.NopCloser(bytes.NewReader(payload("dev-43", true)))
	out := httptest.NewRecorder()
	h.ServeHTTP(out, r)
	if out.Code != 401 || store.Snapshot().Revision != 0 {
		t.Fatal("tampered body accepted")
	}
}

func TestInvalidAndUnrelatedEventsNeverChangeState(t *testing.T) {
	cases := []struct {
		name string
		body []byte
		code int
	}{
		{"wrong repository", bytes.ReplaceAll(payload("dev-42", true), []byte(testRepository), []byte("other/repo")), 422},
		{"no repository", []byte(`{"action":"published"}`), 422},
		{"broken JSON", []byte(`{"action":`), 400},
		{"deleted", bytes.ReplaceAll(payload("dev-42", true), []byte("published\""), []byte("deleted\"")), 204},
		{"draft", bytes.ReplaceAll(payload("dev-42", true), []byte(`"draft":false`), []byte(`"draft":true`)), 204},
		{"mutable alias", payload("dev-latest", true), 204},
		{"shell tag", payload("dev-42;touch /tmp/unsafe", true), 422},
		{"wrong prerelease", payload("v1.0.0", true), 422},
		{"wrong stable", payload("dev-42", false), 422},
		{"alpha", payload("v1.0.0-alpha", true), 422},
		{"overflow", payload("dev-99999999999999999999", true), 422},
		{"missing time", bytes.ReplaceAll(payload("dev-42", true), []byte(`"2026-10-25T02:00:00Z"`), []byte(`null`)), 422},
		{"oversize", bytes.Repeat([]byte("x"), maxBody+1), 413},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h, s := testHandler(t)
			send(t, h, c.body, delivery(1), c.code)
			if s.Snapshot().Revision != 0 {
				t.Fatal("state changed")
			}
		})
	}
	for _, event := range []string{"ping", "push", "workflow_run"} {
		h, s := testHandler(t)
		r := signedRequest([]byte(`{}`), delivery(1))
		r.Header.Set("X-GitHub-Event", event)
		out := httptest.NewRecorder()
		h.ServeHTTP(out, r)
		if out.Code != 204 || s.Snapshot().Revision != 0 {
			t.Fatal("unrelated event accepted")
		}
	}
}

func TestRepeatedReplayedAndOutOfOrderHintsDoNotReNotify(t *testing.T) {
	h, s := testHandler(t)
	send(t, h, payload("dev-42", true), delivery(1), 200)
	for _, pair := range []struct {
		tag string
		id  int
	}{{"dev-42", 1}, {"dev-42", 2}, {"dev-41", 3}} {
		if !strings.Contains(send(t, h, payload(pair.tag, true), delivery(pair.id), 200), `"accepted":false`) {
			t.Fatal("old hint accepted")
		}
	}
	if s.Snapshot().Revision != 1 {
		t.Fatal("replay changed revision")
	}
}

func TestArbitraryPayloadFieldsAreNeverReturnedOrExecuted(t *testing.T) {
	h, s := testHandler(t)
	body := append([]byte(`{"command":"touch /tmp/unsafe","download_url":"https://evil.example/installer","secret":"hidden",`), payload("dev-42", true)[1:]...)
	send(t, h, body, delivery(1), 200)
	data, _ := json.Marshal(s.Snapshot())
	for _, value := range []string{"command", "download_url", "hidden", "evil.example"} {
		if bytes.Contains(data, []byte(value)) {
			t.Fatal("untrusted fields were retained")
		}
	}
}

func TestMethodContentTypeIdentityAndConcurrencyLimits(t *testing.T) {
	h, _ := testHandler(t)
	for _, path := range []string{"/hooks/github/releases", "/healthz", "/v1/releases/latest", "/v1/events"} {
		out := httptest.NewRecorder()
		h.ServeHTTP(out, httptest.NewRequest("DELETE", path, nil))
		if out.Code != 405 {
			t.Fatalf("method accepted: %s", path)
		}
	}
	r := signedRequest(payload("dev-42", true), delivery(1))
	r.Header.Set("Content-Type", "text/plain")
	out := httptest.NewRecorder()
	h.ServeHTTP(out, r)
	if out.Code != 415 {
		t.Fatal("wrong content type accepted")
	}
	r = signedRequest(payload("dev-42", true), "invalid")
	out = httptest.NewRecorder()
	h.ServeHTTP(out, r)
	if out.Code != 400 {
		t.Fatal("invalid delivery accepted")
	}
	for i := 0; i < cap(h.busy); i++ {
		h.busy <- struct{}{}
	}
	send(t, h, payload("dev-42", true), delivery(1), 503)
	for i := 0; i < cap(h.busy); i++ {
		<-h.busy
	}
	r = signedRequest(payload("dev-42", true), delivery(2))
	r.Header.Set("Content-Type", "application/json; charset=utf-8")
	out = httptest.NewRecorder()
	h.ServeHTTP(out, r)
	if out.Code != 200 {
		t.Fatal("JSON charset rejected")
	}
}

func readSSE(t *testing.T, reader *bufio.Reader) Snapshot {
	t.Helper()
	var value Snapshot
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(line, "data: ") {
			if err = json.Unmarshal([]byte(strings.TrimSpace(strings.TrimPrefix(line, "data: "))), &value); err != nil {
				t.Fatal(err)
			}
		}
		if line == "\n" {
			return value
		}
	}
}

func TestSSEPublishesDurableSnapshotsAndReconnectsWithoutHistory(t *testing.T) {
	h, s := testHandler(t)
	server := httptest.NewServer(h)
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "GET", server.URL+"/v1/events", nil)
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	reader := bufio.NewReader(response.Body)
	if got := readSSE(t, reader); got.Revision != 0 {
		t.Fatal("wrong initial snapshot")
	}
	send(t, h, payload("dev-42", true), delivery(1), 200)
	if got := readSSE(t, reader); got.Revision != 1 || got.Latest["dev"].Tag != "dev-42" {
		t.Fatal("missing release notification")
	}
	restored, err := osReadSnapshot(s.path)
	if err != nil || restored.Revision != 1 {
		t.Fatal("notification preceded durable state")
	}
	cancel()
	_ = response.Body.Close()
	req, _ = http.NewRequest("GET", server.URL+"/v1/events", nil)
	req.Header.Set("Last-Event-ID", "999999")
	response2, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response2.Body.Close()
	if got := readSSE(t, bufio.NewReader(response2.Body)); got.Revision != 1 {
		t.Fatal("reconnect did not get current snapshot")
	}
}

func TestSSESubscriberLimit(t *testing.T) {
	h, s := testHandler(t)
	for i := 0; i < maxSubscribers; i++ {
		_, cleanup, ok := s.subscribe(maxSubscribers)
		if !ok {
			t.Fatal("unexpected limit")
		}
		defer cleanup()
	}
	out := httptest.NewRecorder()
	h.ServeHTTP(out, httptest.NewRequest("GET", "/v1/events", nil))
	if out.Code != 503 {
		t.Fatal("unbounded subscribers")
	}
}
