package notify

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const testSecret = "01234567890123456789012345678901"
const payload = `{"action":"published","repository":{"full_name":"stewardful/deploy-platform","id":1},"release":{"id":2,"tag_name":"dev-42","target_commitish":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","draft":false,"prerelease":true,"published_at":"2026-10-25T00:00:00Z","body":"ignored command and URL"}}`

func hook(h http.Handler, body string, mutate func(*http.Request)) *httptest.ResponseRecorder {
	r := httptest.NewRequest("POST", "/hooks/github/releases", strings.NewReader(body))
	r.Header.Set("Content-Type", "application/json")
	r.Header.Set("X-GitHub-Event", "release")
	r.Header.Set("X-GitHub-Delivery", "00000000-0000-0000-0000-000000000001")
	mac := hmac.New(sha256.New, []byte(testSecret))
	_, _ = mac.Write([]byte(body))
	r.Header.Set("X-Hub-Signature-256", "sha256="+hex.EncodeToString(mac.Sum(nil)))
	if mutate != nil {
		mutate(r)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}
func TestWebhookBoundary(t *testing.T) {
	calls := 0
	h := NewHandler(func() (Config, error) {
		return Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)}, nil
	}, func(_ *http.Request, _ []byte, n Notice) error {
		calls++
		if n.Tag != "dev-42" {
			t.Fatal(n)
		}
		return nil
	})
	cases := []struct {
		name, body string
		mutate     func(*http.Request)
		code       int
	}{
		{"valid", payload, nil, 200},
		{"tamper", payload, func(r *http.Request) { r.Header.Set("X-Hub-Signature-256", "sha256="+strings.Repeat("0", 64)) }, 401},
		{"wrong source", strings.ReplaceAll(payload, "stewardful/deploy-platform", "fork/repo"), nil, 422},
		{"draft", strings.ReplaceAll(payload, `"draft":false`, `"draft":true`), nil, 204},
		{"rolling", strings.ReplaceAll(payload, "dev-42", "dev-latest"), nil, 204},
		{"action", strings.ReplaceAll(payload, "published", "edited"), nil, 204},
		{"event", payload, func(r *http.Request) { r.Header.Set("X-GitHub-Event", "ping") }, 204},
		{"identity", payload, func(r *http.Request) { r.Header.Set("X-GitHub-Delivery", "bad") }, 400},
		{"json", "{", nil, 400},
		{"overflow", strings.Repeat("a", maxBody+1), nil, 413},
		{"media", payload, func(r *http.Request) { r.Header.Set("Content-Type", "text/plain") }, 415},
		{"method", payload, func(r *http.Request) { r.Method = "GET" }, 405},
		{"removed SSE", payload, func(r *http.Request) { r.URL.Path = "/v1/events" }, 404},
		{"bad tag", strings.ReplaceAll(payload, "dev-42", "dev-18446744073709551616"), nil, 422},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := hook(h, c.body, c.mutate).Code; got != c.code {
				t.Fatalf("%d want %d", got, c.code)
			}
		})
	}
	if calls != 1 {
		t.Fatal("invalid webhook submitted work", calls)
	}
}
func TestNoSuccessBeforeDurableSubmission(t *testing.T) {
	h := NewHandler(func() (Config, error) {
		return Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)}, nil
	}, func(*http.Request, []byte, Notice) error { return errors.New("disk unavailable") })
	if got := hook(h, payload, nil).Code; got != 503 {
		t.Fatal(got)
	}
	h = NewHandler(func() (Config, error) { return Config{}, errors.New("secret unavailable") }, func(*http.Request, []byte, Notice) error { t.Fatal("submitted without secret"); return nil })
	if got := hook(h, payload, nil).Code; got != 503 {
		t.Fatal(got)
	}
}
func TestStableReleaseIsOnlyANotice(t *testing.T) {
	called := false
	h := NewHandler(func() (Config, error) {
		return Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)}, nil
	}, func(_ *http.Request, _ []byte, n Notice) error {
		called = true
		if n.Channel != "stable" || n.Tag != "v0.5.9" {
			t.Fatal(n)
		}
		return nil
	})
	body := strings.ReplaceAll(strings.ReplaceAll(payload, "dev-42", "v0.5.9"), `"prerelease":true`, `"prerelease":false`)
	if hook(h, body, nil).Code != 200 || !called {
		t.Fatal("stable rejected")
	}
}
