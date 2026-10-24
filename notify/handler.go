package notify

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"strings"
	"time"
)

const maxBody = 256 * 1024
const maxSubscribers = 16

// Config pins the one permitted source repository. Secret is never returned by an API.
type Config struct {
	Repository string
	Secret     []byte
}

// Handler exposes only release hints: no shell, runtime, download or upgrade operations.
type Handler struct {
	config Config
	store  *Store
	busy   chan struct{}
}

func NewHandler(config Config, store *Store) (*Handler, error) {
	if !validRepository(config.Repository) || len(config.Secret) < 32 || len(config.Secret) > 1024 || store == nil {
		return nil, fmt.Errorf("valid repository, 32-1024 byte secret and store required")
	}
	for _, previous := range store.Snapshot().Latest {
		if previous.Repository != config.Repository {
			return nil, fmt.Errorf("stored repository differs; use a separate state directory")
		}
	}
	config.Secret = append([]byte(nil), config.Secret...)
	return &Handler{config: config, store: store, busy: make(chan struct{}, 8)}, nil
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "no-store")
	switch r.URL.Path {
	case "/hooks/github/releases":
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", "POST")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		h.webhook(w, r)
	case "/healthz":
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, "{\"status\":\"ok\"}\n")
	case "/v1/releases/latest":
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(h.store.Snapshot())
	case "/v1/events":
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		h.events(w, r)
	default:
		http.NotFound(w, r)
	}
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
	if !signatureOK(h.config.Secret, r.Header.Get("X-Hub-Signature-256"), body) {
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
	notice, relevant, err := payload.notice(h.config.Repository)
	if err != nil {
		http.Error(w, "untrusted or unsupported release", http.StatusUnprocessableEntity)
		return
	}
	if !relevant {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	changed, err := h.store.Accept(r.Header.Get("X-GitHub-Delivery"), notice)
	if err != nil {
		http.Error(w, "notification persistence failed", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(struct {
		Accepted bool `json:"accepted"`
	}{Accepted: changed})
}

func (h *Handler) events(w http.ResponseWriter, r *http.Request) {
	changed, unsubscribe, ok := h.store.subscribe(maxSubscribers)
	if !ok {
		http.Error(w, "subscriber limit reached", http.StatusServiceUnavailable)
		return
	}
	defer unsubscribe()
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("X-Accel-Buffering", "no")
	controller := http.NewResponseController(w)
	write := func(text string) error {
		if err := controller.SetWriteDeadline(time.Now().Add(5 * time.Second)); err != nil && !errors.Is(err, http.ErrNotSupported) {
			return err
		}
		if _, err := io.WriteString(w, text); err != nil {
			return err
		}
		return controller.Flush()
	}
	snapshot := func() error {
		value := h.store.Snapshot()
		data, err := json.Marshal(value)
		if err != nil {
			return err
		}
		return write(fmt.Sprintf("id: %d\nevent: snapshot\ndata: %s\n\n", value.Revision, data))
	}
	// Always send a complete snapshot, even with Last-Event-ID: no unbounded event history.
	if err := snapshot(); err != nil {
		return
	}
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case <-changed:
			if err := snapshot(); err != nil {
				return
			}
		case <-ticker.C:
			if err := write(": heartbeat\n\n"); err != nil {
				return
			}
		}
	}
}
