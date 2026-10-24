package notify

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"syscall"
)

const maxDeliveries = 256
const maxStateBytes = 128 * 1024

var deliveryPattern = regexp.MustCompile(`^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$`)

// Snapshot contains the latest hint per channel. Reconnecting clients always get a snapshot.
type Snapshot struct {
	Revision uint64            `json:"revision"`
	Latest   map[string]Notice `json:"latest"`
}

type diskState struct {
	Schema     int      `json:"schema_version"`
	Snapshot   Snapshot `json:"snapshot"`
	Deliveries []string `json:"deliveries"`
}

// Store commits hints before waking clients. It retains bounded deduplication state.
type Store struct {
	lock        *os.File
	closed      bool
	mu          sync.Mutex
	state       diskState
	path        string
	subscribers map[chan struct{}]struct{}
	persist     func(string, diskState) error
}

// OpenStore requires a private directory and the current state format; it does not migrate old data.
func OpenStore(directory string) (*Store, error) {
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, err
	}
	// systemd DynamicUser exposes StateDirectory through a symlink. Resolve the
	// locally configured path once, then keep using the canonical private target.
	resolved, err := filepath.EvalSymlinks(directory)
	if err != nil {
		return nil, fmt.Errorf("cannot resolve state directory")
	}
	directory, err = filepath.Abs(resolved)
	if err != nil {
		return nil, err
	}
	info, err := os.Lstat(directory)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0o077 != 0 {
		return nil, fmt.Errorf("state directory must resolve to a private directory")
	}
	lock, err := os.OpenFile(filepath.Join(directory, ".lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return nil, fmt.Errorf("cannot open private state lock")
	}
	lockInfo, err := lock.Stat()
	if err != nil || !lockInfo.Mode().IsRegular() || lockInfo.Mode().Perm()&0o077 != 0 {
		_ = lock.Close()
		return nil, fmt.Errorf("invalid state lock")
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = lock.Close()
		return nil, fmt.Errorf("state is in use")
	}
	store, err := loadStore(directory)
	if err != nil {
		_ = lock.Close()
		return nil, err
	}
	store.lock = lock
	return store, nil
}

// Close releases the process lock. Call after stopping all HTTP handlers.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil
	}
	s.closed = true
	return s.lock.Close()
}

func loadStore(directory string) (*Store, error) {
	s := &Store{path: filepath.Join(directory, "state.json"), subscribers: make(map[chan struct{}]struct{}), persist: save,
		state: diskState{Schema: 1, Snapshot: Snapshot{Latest: make(map[string]Notice)}, Deliveries: []string{}}}
	info, err := os.Lstat(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o077 != 0 || info.Size() > maxStateBytes {
		return nil, fmt.Errorf("invalid private state file")
	}
	file, err := os.Open(s.path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return nil, fmt.Errorf("state file changed")
	}
	data, err := io.ReadAll(io.LimitReader(file, maxStateBytes+1))
	if err != nil || len(data) > maxStateBytes {
		return nil, fmt.Errorf("cannot read bounded state")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var loaded diskState
	if err := decoder.Decode(&loaded); err != nil {
		return nil, fmt.Errorf("invalid state format")
	}
	if decoder.Decode(new(any)) != io.EOF || !validState(loaded) {
		return nil, fmt.Errorf("unsupported state format")
	}
	s.state = loaded
	return s, nil
}

func validState(state diskState) bool {
	if state.Schema != 1 || state.Deliveries == nil || state.Snapshot.Latest == nil || len(state.Snapshot.Latest) > 2 || len(state.Deliveries) > maxDeliveries {
		return false
	}
	if state.Snapshot.Revision == 0 && len(state.Snapshot.Latest) != 0 {
		return false
	}
	for channel, notice := range state.Snapshot.Latest {
		if channel != notice.Channel || !notice.valid() {
			return false
		}
	}
	seen := make(map[string]bool)
	for _, id := range state.Deliveries {
		if !deliveryPattern.MatchString(id) || seen[id] {
			return false
		}
		seen[id] = true
	}
	return true
}

func cloneSnapshot(value Snapshot) Snapshot {
	copy := Snapshot{Revision: value.Revision, Latest: make(map[string]Notice)}
	for channel, notice := range value.Latest {
		copy.Latest[channel] = notice
	}
	return copy
}

func (s *Store) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	return cloneSnapshot(s.state.Snapshot)
}

// Accept returns false for repeated or older hints. Delivery IDs cannot override version ordering.
func (s *Store) Accept(id string, notice Notice) (bool, error) {
	if !deliveryPattern.MatchString(id) || !notice.valid() {
		return false, fmt.Errorf("invalid notice")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return false, fmt.Errorf("state is closed")
	}
	for _, previous := range s.state.Deliveries {
		if previous == id {
			return false, nil
		}
	}
	previous, exists := s.state.Snapshot.Latest[notice.Channel]
	if exists && previous.Repository != notice.Repository {
		return false, fmt.Errorf("repository differs")
	}
	if exists && !newer(notice, previous) {
		return false, nil
	}
	if s.state.Snapshot.Revision == math.MaxUint64 {
		return false, fmt.Errorf("revision exhausted")
	}
	next := diskState{Schema: 1, Snapshot: cloneSnapshot(s.state.Snapshot), Deliveries: append([]string(nil), s.state.Deliveries...)}
	next.Snapshot.Revision++
	next.Snapshot.Latest[notice.Channel] = notice
	next.Deliveries = append(next.Deliveries, id)
	if len(next.Deliveries) > maxDeliveries {
		next.Deliveries = next.Deliveries[len(next.Deliveries)-maxDeliveries:]
	}
	if err := s.persist(s.path, next); err != nil {
		return false, fmt.Errorf("persist notification: %w", err)
	}
	s.state = next
	for subscriber := range s.subscribers {
		select {
		case subscriber <- struct{}{}:
		default: // Slow consumers fetch the latest snapshot; do not accumulate a queue.
		}
	}
	return true, nil
}

func (s *Store) subscribe(limit int) (<-chan struct{}, func(), bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed || len(s.subscribers) >= limit {
		return nil, nil, false
	}
	ch := make(chan struct{}, 1)
	s.subscribers[ch] = struct{}{}
	return ch, func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		delete(s.subscribers, ch)
	}, true
}

func save(path string, state diskState) error {
	data, err := json.Marshal(state)
	if err != nil || len(data) > maxStateBytes {
		return fmt.Errorf("state exceeds limit")
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".state-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(file.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}
