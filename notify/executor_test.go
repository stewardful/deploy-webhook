package notify

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func envelope(body string) *Envelope {
	mac := hmac.New(sha256.New, []byte(testSecret))
	_, _ = mac.Write([]byte(body))
	return &Envelope{Body: []byte(body), Signature: "sha256=" + hex.EncodeToString(mac.Sum(nil)), Delivery: "00000000-0000-0000-0000-000000000001"}
}
func save(t *testing.T, s *Store, body string) {
	t.Helper()
	e := envelope(body)
	n, err := parse(e, Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)})
	if err != nil {
		t.Fatal(err)
	}
	r, _ := http.NewRequest("POST", "/hooks/github/releases", nil)
	r.Header.Set("X-Hub-Signature-256", e.Signature)
	r.Header.Set("X-GitHub-Delivery", e.Delivery)
	if err := s.Accept(r, e.Body, n); err != nil {
		t.Fatal(err)
	}
}
func TestWorkerDurablyDeduplicatesAndCoalescesWithoutShell(t *testing.T) {
	inbox := t.TempDir()
	_ = os.Chmod(inbox, 0700)
	ledger := filepath.Join(t.TempDir(), "ledger")
	s, err := OpenInbox(inbox)
	if err != nil {
		t.Fatal(err)
	}
	save(t, s, payload)
	cfg := Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)}
	calls := 0
	run := func(ctx context.Context, n Notice) (string, error) {
		calls++
		if calls == 1 {
			save(t, s, strings.ReplaceAll(payload, "dev-42", "dev-43"))
		}
		if n.Commit != strings.Repeat("a", 40) {
			t.Fatal("wrong pinned commit")
		}
		return "succeeded", nil
	}
	if err := Work(context.Background(), inbox, ledger, cfg, run); err != nil {
		t.Fatal(err)
	}
	if err := Work(context.Background(), inbox, ledger, cfg, run); err != nil {
		t.Fatal(err)
	}
	save(t, s, payload)
	if err := Work(context.Background(), inbox, ledger, cfg, run); err != nil {
		t.Fatal(err)
	}
	v, err := ReadLedger(ledger)
	if err != nil || calls != 2 || v.Dev.Notice.Tag != "dev-43" || v.Dev.Phase != "succeeded" {
		t.Fatal(v, calls, err)
	}
}
func TestStableOnlyRecordsManualNotice(t *testing.T) {
	inbox := t.TempDir()
	_ = os.Chmod(inbox, 0700)
	s, _ := OpenInbox(inbox)
	save(t, s, strings.ReplaceAll(strings.ReplaceAll(payload, "dev-42", "v0.5.9"), `"prerelease":true`, `"prerelease":false`))
	ledger := filepath.Join(t.TempDir(), "ledger")
	err := Work(context.Background(), inbox, ledger, Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)}, func(context.Context, Notice) (string, error) { t.Fatal("stable executed install"); return "", nil })
	v, _ := ReadLedger(ledger)
	if err != nil || v.Stable == nil || v.Dev != nil {
		t.Fatal(v, err)
	}
}
func TestTamperedInboxAndAttemptedFailureNeverExecuteAgain(t *testing.T) {
	inbox := t.TempDir()
	_ = os.Chmod(inbox, 0700)
	s, _ := OpenInbox(inbox)
	save(t, s, payload)
	ledger := filepath.Join(t.TempDir(), "ledger")
	cfg := Config{Repository: "stewardful/deploy-platform", Secret: []byte(testSecret)}
	calls := 0
	run := func(context.Context, Notice) (string, error) { calls++; return "blocked", nil }
	if err := Work(context.Background(), inbox, ledger, cfg, run); err != nil {
		t.Fatal(err)
	}
	if err := Work(context.Background(), inbox, ledger, cfg, run); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatal(calls)
	}
	b, _ := os.ReadFile(filepath.Join(inbox, "inbox.json"))
	b = []byte(strings.ReplaceAll(string(b), "sha256=", "invalid="))
	_ = os.WriteFile(filepath.Join(inbox, "inbox.json"), b, 0600)
	if err := Work(context.Background(), inbox, ledger, cfg, run); err == nil {
		t.Fatal("root worker trusted HTTP directory without reverifying signature")
	}
	if calls != 1 {
		t.Fatal("tampered work executed")
	}
}

func TestCLIOutputLimitCannotBeBypassedByIOCopy(t *testing.T) {
	var b boundedBuffer
	_, err := io.Copy(&b, io.LimitReader(strings.NewReader(strings.Repeat("x", 65536)), 65536))
	if err == nil || !b.over || b.data.Len() > 32768 {
		t.Fatal("unbounded command output", b.data.Len(), err)
	}
}
