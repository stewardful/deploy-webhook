package notify

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func notice(tag, channel string) Notice {
	return Notice{Repository: testRepository, Tag: tag, Channel: channel, ReleaseID: 200, PublishedAt: time.Date(2026, 10, 25, 2, 0, 0, 0, time.UTC)}
}

func osReadSnapshot(path string) (Snapshot, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Snapshot{}, err
	}
	var value diskState
	err = json.Unmarshal(data, &value)
	return value.Snapshot, err
}

func TestStoreRestartPreservesDeduplicationAndVersionOrdering(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "state")
	s, err := OpenStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	changed, err := s.Accept(delivery(1), notice("dev-42", "dev"))
	if !changed || err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(s.path)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatal("state is not private")
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	s, err = OpenStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	for _, id := range []string{delivery(1), delivery(2)} {
		if changed, err := s.Accept(id, notice("dev-42", "dev")); changed || err != nil {
			t.Fatal("replay accepted after restart")
		}
	}
	copy := s.Snapshot()
	delete(copy.Latest, "dev")
	if len(s.Snapshot().Latest) != 1 {
		t.Fatal("snapshot escaped by reference")
	}
}

func TestStoreFailuresDoNotAdvanceMemoryOrWakeSubscribers(t *testing.T) {
	_, s := testHandler(t)
	ch, cleanup, _ := s.subscribe(1)
	defer cleanup()
	s.persist = func(string, diskState) error { return errors.New("disk unavailable") }
	if changed, err := s.Accept(delivery(1), notice("dev-42", "dev")); changed || err == nil {
		t.Fatal("failed persistence accepted")
	}
	if s.Snapshot().Revision != 0 {
		t.Fatal("memory changed")
	}
	select {
	case <-ch:
		t.Fatal("subscriber woke before commit")
	default:
	}
	if _, err := os.Stat(s.path); !os.IsNotExist(err) {
		t.Fatal("state file created")
	}
}

func TestStoreRejectsConcurrentProcessAndUnsafeState(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "state")
	s, err := OpenStore(directory)
	if err != nil {
		t.Fatal(err)
	}
	if second, err := OpenStore(directory); err == nil {
		_ = second.Close()
		t.Fatal("concurrent store allowed")
	}
	_ = s.Close()
	for _, body := range []string{`{}`, `{"schema_version":0}`, `{"schema_version":2}`, `{"schema_version":1,"snapshot":{"revision":0,"latest":{}},"deliveries":[],"unknown":true}`, `{"schema_version":1,"snapshot":{"revision":0,"latest":{}},"deliveries":[]} {}`, strings.Repeat("x", maxStateBytes+1)} {
		if err := os.WriteFile(filepath.Join(directory, "state.json"), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		if opened, err := OpenStore(directory); err == nil {
			_ = opened.Close()
			t.Fatal("invalid state allowed")
		}
	}
	_ = os.Remove(filepath.Join(directory, "state.json"))
	target := filepath.Join(t.TempDir(), "external")
	_ = os.WriteFile(target, []byte(`{}`), 0o600)
	if err := os.Symlink(target, filepath.Join(directory, "state.json")); err != nil {
		t.Fatal(err)
	}
	if opened, err := OpenStore(directory); err == nil {
		_ = opened.Close()
		t.Fatal("linked state accepted")
	}
	private := t.TempDir()
	_ = os.Chmod(private, 0o755)
	if opened, err := OpenStore(private); err == nil {
		_ = opened.Close()
		t.Fatal("public state directory accepted")
	}
}

func TestStoreCoalescesSlowConsumersAndBoundsDiskHistory(t *testing.T) {
	_, s := testHandler(t)
	ch, cleanup, _ := s.subscribe(1)
	defer cleanup()
	for i := 1; i <= maxDeliveries+3; i++ {
		if _, err := s.Accept(delivery(i), notice(fmt.Sprintf("dev-%d", i), "dev")); err != nil {
			t.Fatal(err)
		}
	}
	if len(ch) != 1 || len(s.state.Deliveries) != maxDeliveries {
		t.Fatal("unbounded notification history")
	}
	if s.Snapshot().Latest["dev"].Tag != "dev-259" {
		t.Fatal("latest lost")
	}
	if _, err := s.Accept(delivery(1), notice("dev-1", "dev")); err != nil {
		t.Fatal(err)
	}
	if s.Snapshot().Revision != 259 {
		t.Fatal("evicted old replay caused notification")
	}
}

func TestConcurrentUpdatesKeepHighestBuild(t *testing.T) {
	_, s := testHandler(t)
	var group sync.WaitGroup
	for i := 1; i <= 40; i++ {
		group.Add(1)
		go func(number int) {
			defer group.Done()
			if _, err := s.Accept(delivery(number), notice(fmt.Sprintf("dev-%d", number), "dev")); err != nil {
				t.Error(err)
			}
		}(i)
	}
	group.Wait()
	if s.Snapshot().Latest["dev"].Tag != "dev-40" {
		t.Fatal("out of order concurrent update replaced latest")
	}
}

func TestRepositoryConfigurationCannotReuseOtherSourceState(t *testing.T) {
	_, s := testHandler(t)
	_, _ = s.Accept(delivery(1), notice("dev-42", "dev"))
	if _, err := NewHandler(Config{Repository: "other/repo", Secret: testSecret}, s); err == nil {
		t.Fatal("source state mixed")
	}
	for _, cfg := range []Config{{Repository: "../repo", Secret: testSecret}, {Repository: testRepository, Secret: []byte("short")}} {
		if _, err := NewHandler(cfg, s); err == nil {
			t.Fatal("bad config accepted")
		}
	}
}

func TestPrivateStateDirectoryAliasSupportsSystemdDynamicUser(t *testing.T) {
	root := t.TempDir()
	target := filepath.Join(root, "private-state")
	if err := os.Mkdir(target, 0o700); err != nil {
		t.Fatal(err)
	}
	alias := filepath.Join(root, "service-state")
	if err := os.Symlink(target, alias); err != nil {
		t.Fatal(err)
	}
	s, err := OpenStore(alias)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if _, err = s.Accept(delivery(1), notice("dev-42", "dev")); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(filepath.Join(target, "state.json")); err != nil {
		t.Fatal("state did not use canonical target")
	}
}
