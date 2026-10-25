package notify

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"regexp"
	"strings"
)

const maxBody = 256 * 1024

var deliveryPattern = regexp.MustCompile(`^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$`)

// Config contains only local administrator policy and a fixed task submission boundary.
type Config struct {
	Repository string
	Secret     []byte
}
type Handler struct {
	config func() (Config, error)
	submit func(*http.Request, []byte, Notice) error
	busy   chan struct{}
}

func NewHandler(config func() (Config, error), submit func(*http.Request, []byte, Notice) error) http.Handler {
	return &Handler{config: config, submit: submit, busy: make(chan struct{}, 8)}
}
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "no-store")
	if r.URL.Path == "/healthz" && r.Method == "GET" {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"status":"ok"}`)
		return
	}
	if r.URL.Path != "/hooks/github/releases" {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", "POST")
		http.Error(w, "method not allowed", 405)
		return
	}
	h.webhook(w, r)
}

func signatureOK(secret []byte, signature string, body []byte) bool {
	if !strings.HasPrefix(signature, "sha256=") || len(signature) != len("sha256=")+64 {
		return false
	}
	decoded, err := hex.DecodeString(signature[len("sha256="):])
	if err != nil {
		return false
	}
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write(body)
	return hmac.Equal(decoded, mac.Sum(nil))
}

func (h *Handler) webhook(w http.ResponseWriter, r *http.Request) {
	cfg, err := h.config()
	if err != nil || !validRepository(cfg.Repository) || len(cfg.Secret) < 32 || len(cfg.Secret) > 1024 {
		http.Error(w, "webhook not configured", 503)
		return
	}
	select {
	case h.busy <- struct{}{}:
		defer func() { <-h.busy }()
	default:
		w.Header().Set("Retry-After", "1")
		http.Error(w, "busy", http.StatusServiceUnavailable)
		return
	}
	mediaType, _, mediaErr := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if mediaErr != nil || mediaType != "application/json" {
		http.Error(w, "application/json required", http.StatusUnsupportedMediaType)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxBody)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			http.Error(w, "payload too large", http.StatusRequestEntityTooLarge)
		} else {
			http.Error(w, "cannot read payload", http.StatusBadRequest)
		}
		return
	}
	if !signatureOK(cfg.Secret, r.Header.Get("X-Hub-Signature-256"), body) {
		http.Error(w, "invalid signature", http.StatusUnauthorized)
		return
	}
	if !deliveryPattern.MatchString(r.Header.Get("X-GitHub-Delivery")) {
		http.Error(w, "invalid delivery identity", http.StatusBadRequest)
		return
	}
	if r.Header.Get("X-GitHub-Event") != "release" {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	var payload releasePayload
	if err := json.Unmarshal(body, &payload); err != nil {
		http.Error(w, "invalid JSON", http.StatusBadRequest)
		return
	}
	notice, relevant, err := payload.notice(cfg.Repository)
	if err != nil {
		http.Error(w, "untrusted or unsupported release", http.StatusUnprocessableEntity)
		return
	}
	if !relevant {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	err = h.submit(r, body, notice)
	if err != nil {
		http.Error(w, "notification persistence failed", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(struct {
		Accepted bool `json:"accepted"`
	}{Accepted: true})
}
