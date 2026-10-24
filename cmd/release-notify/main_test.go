package main

import (
	"context"
	"github.com/stewardful/deploy-delivery/notify"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPrivateSecretFileValidation(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "webhook.secret")
	for _, value := range []string{"short", strings.Repeat("x", 31), strings.Repeat("x", 1025), strings.Repeat("x", 32) + "\nsecondline"} {
		if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := readSecret(path); err == nil {
			t.Fatal("unsafe secret accepted")
		}
	}
	value := strings.Repeat("x", 64)
	_ = os.WriteFile(path, []byte(value+"\n"), 0o600)
	if actual, err := readSecret(path); err != nil || string(actual) != value {
		t.Fatal("valid secret rejected")
	}
	_ = os.Chmod(path, 0o644)
	if _, err := readSecret(path); err == nil {
		t.Fatal("public secret accepted")
	}
	_ = os.Chmod(path, 0o600)
	link := filepath.Join(root, "linked.secret")
	_ = os.Symlink(path, link)
	if _, err := readSecret(link); err == nil {
		t.Fatal("linked secret accepted")
	}
}

func TestListenerRejectsPublicBindAndAmbiguousAddresses(t *testing.T) {
	for _, address := range []string{"0.0.0.0:8090", "[::]:8090", "localhost:8090", ":8090", "127.0.0.1:0", "127.0.0.1:-1", "127.0.0.1:65536", "127.0.0.1:http"} {
		if loopbackAddress(address) {
			t.Fatalf("unsafe address accepted: %s", address)
		}
	}
	for _, address := range []string{"127.0.0.1:8090", "[::1]:8090"} {
		if !loopbackAddress(address) {
			t.Fatal("loopback rejected")
		}
	}
}

func TestReadOnlySystemdCredentialIsAcceptedOnlyAtManagerProvidedPath(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "credentials")
	if err := os.Mkdir(directory, 0o750); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "webhook.secret")
	if err := os.WriteFile(path, []byte(strings.Repeat("x", 64)), 0o440); err != nil {
		t.Fatal(err)
	}
	t.Setenv("CREDENTIALS_DIRECTORY", "")
	if _, err := readSecret(path); err == nil {
		t.Fatal("ordinary group-readable secret accepted")
	}
	t.Setenv("CREDENTIALS_DIRECTORY", directory)
	if _, err := readSecret(path); err != nil {
		t.Fatal(err)
	}
	for _, permissions := range []os.FileMode{0o770, 0o755} {
		if err := os.Chmod(directory, permissions); err != nil {
			t.Fatal(err)
		}
		if _, err := readSecret(path); err == nil {
			t.Fatal("insecure credential directory accepted")
		}
	}
}

func TestForwardingIsFixedAndPreservesSignedBody(t *testing.T) {
	for _, target := range []string{"https://example.com/hooks/github/releases", "http://localhost:8088/hooks/github/releases", "http://127.0.0.1:8088/api/v1/apps", "http://127.0.0.1:8088/hooks/github/releases?command=sh"} {
		if _, err := forwarder(target); err == nil {
			t.Fatal("unsafe target", target)
		}
	}
	calls := 0
	code := 200
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		data, _ := io.ReadAll(r.Body)
		if string(data) != "signed original" || r.Header.Get("X-Hub-Signature-256") != "signature" {
			t.Error("body or signature changed")
		}
		w.WriteHeader(code)
	}))
	defer server.Close()
	forward, err := forwarder(server.URL + "/hooks/github/releases")
	if err != nil {
		t.Fatal(err)
	}
	r, _ := http.NewRequestWithContext(context.Background(), "POST", "/hooks/github/releases", nil)
	r.Header.Set("X-Hub-Signature-256", "signature")
	if err := forward(r, []byte("signed original"), notify.Notice{}); err != nil {
		t.Fatal(err)
	}
	code = 503
	if err := forward(r, []byte("signed original"), notify.Notice{}); err == nil {
		t.Fatal("acknowledged failed task")
	}
	if calls != 2 {
		t.Fatal(calls)
	}
}
