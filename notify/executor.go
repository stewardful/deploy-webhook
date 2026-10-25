package notify

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const maxInbox = 1024 * 1024

type Envelope struct {
	Body      []byte `json:"body"`
	Signature string `json:"signature"`
	Delivery  string `json:"delivery"`
}
type Inbox struct {
	Schema int       `json:"schema_version"`
	Dev    *Envelope `json:"dev"`
	Stable *Envelope `json:"stable"`
}
type Store struct {
	dir string
	mu  sync.Mutex
}

func OpenInbox(dir string) (*Store, error) {
	if !filepath.IsAbs(dir) {
		return nil, errors.New("absolute state directory required")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	info, err := os.Stat(dir)
	if err != nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 {
		return nil, errors.New("private state directory required")
	}
	return &Store{dir: dir}, nil
}
func privateRead(path string, limit int64) ([]byte, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() > limit {
		return nil, errors.New("invalid private record")
	}
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(b)) > limit {
		return nil, errors.New("record exceeds limit")
	}
	return b, nil
}
func readJSON(path string, v any, limit int64) error {
	b, err := privateRead(path, limit)
	if err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if d.Decode(v) != nil || d.Decode(new(any)) != io.EOF {
		return errors.New("invalid record")
	}
	return nil
}
func atomicJSON(dir, name string, v any) error {
	b, err := json.Marshal(v)
	if err != nil || len(b) > maxInbox {
		return errors.New("record too large")
	}
	f, err := os.CreateTemp(dir, ".write-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(b); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(f.Name(), filepath.Join(dir, name))
	}
	if err != nil {
		return err
	}
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
func parse(e *Envelope, cfg Config) (Notice, error) {
	if e == nil || len(e.Body) > maxBody || !deliveryPattern.MatchString(e.Delivery) || !signatureOK(cfg.Secret, e.Signature, e.Body) {
		return Notice{}, errors.New("untrusted envelope")
	}
	var p releasePayload
	if json.Unmarshal(e.Body, &p) != nil {
		return Notice{}, errors.New("bad event")
	}
	n, relevant, err := p.notice(cfg.Repository)
	if err != nil || !relevant {
		return Notice{}, errors.New("unsupported event")
	}
	return n, nil
}
func runNumber(tag string) uint64 {
	n, _ := strconv.ParseUint(strings.TrimPrefix(tag, "dev-"), 10, 64)
	return n
}
func newer(a, b Notice) bool {
	if a.Channel == "dev" {
		return runNumber(a.Tag) > runNumber(b.Tag)
	}
	x := strings.Split(strings.TrimPrefix(a.Tag, "v"), ".")
	y := strings.Split(strings.TrimPrefix(b.Tag, "v"), ".")
	for i := 0; i < 3; i++ {
		nx, _ := strconv.ParseUint(x[i], 10, 64)
		ny, _ := strconv.ParseUint(y[i], 10, 64)
		if nx != ny {
			return nx > ny
		}
	}
	return false
}
func (s *Store) Accept(r *http.Request, body []byte, n Notice) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	value := Inbox{}
	err := readJSON(filepath.Join(s.dir, "inbox.json"), &value, maxInbox)
	if os.IsNotExist(err) {
		value.Schema = 1
	} else if err != nil {
		return err
	}
	if value.Schema != 1 {
		return errors.New("unsupported inbox")
	}
	e := &Envelope{Body: append([]byte(nil), body...), Signature: r.Header.Get("X-Hub-Signature-256"), Delivery: r.Header.Get("X-GitHub-Delivery")}
	slot := &value.Dev
	if n.Channel == "stable" {
		slot = &value.Stable
	}
	// Handler has verified both body and signature. Decode the prior tag only for monotonic coalescing.
	if *slot != nil {
		var old releasePayload
		if json.Unmarshal((*slot).Body, &old) != nil {
			return errors.New("invalid prior inbox")
		}
		prior, ok, err := old.notice(n.Repository)
		if err != nil || !ok {
			return errors.New("invalid prior source")
		}
		if !newer(n, prior) {
			return nil
		}
	}
	*slot = e
	return atomicJSON(s.dir, "inbox.json", value)
}

type Attempt struct {
	Notice Notice `json:"notice"`
	Phase  string `json:"phase"`
}
type Ledger struct {
	Schema int      `json:"schema_version"`
	Dev    *Attempt `json:"dev"`
	Stable *Notice  `json:"stable"`
}

func ReadLedger(dir string) (Ledger, error) {
	v := Ledger{}
	err := readJSON(filepath.Join(dir, "status.json"), &v, 16384)
	if os.IsNotExist(err) {
		return Ledger{Schema: 1}, nil
	}
	if err != nil || v.Schema != 1 {
		return v, errors.New("invalid worker ledger")
	}
	if v.Dev != nil {
		if !v.Dev.Notice.Valid() || v.Dev.Notice.Channel != "dev" {
			return v, errors.New("invalid prior build")
		}
		switch v.Dev.Phase {
		case "attempted", "succeeded", "failed", "blocked", "unchanged", "skipped":
		default:
			return v, errors.New("invalid prior phase")
		}
	}
	if v.Stable != nil && (!v.Stable.Valid() || v.Stable.Channel != "stable") {
		return v, errors.New("invalid stable notice")
	}
	return v, nil
}

type Runner func(context.Context, Notice) (string, error)

func Work(ctx context.Context, inboxDir, ledgerDir string, cfg Config, run Runner) error {
	if !validRepository(cfg.Repository) || len(cfg.Secret) < 32 || !filepath.IsAbs(inboxDir) || !filepath.IsAbs(ledgerDir) {
		return errors.New("invalid local configuration")
	}
	info, err := os.Lstat(ledgerDir)
	if os.IsNotExist(err) {
		err = os.MkdirAll(ledgerDir, 0700)
		info, _ = os.Lstat(ledgerDir)
	}
	if err != nil || info == nil || !info.IsDir() || info.Mode().Perm()&0077 != 0 || info.Sys().(*syscall.Stat_t).Uid != uint32(os.Geteuid()) {
		return errors.New("private root ledger required")
	}
	lock, err := os.OpenFile(filepath.Join(ledgerDir, "worker.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return err
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	for ctx.Err() == nil {
		ledger, err := ReadLedger(ledgerDir)
		if ledger.Stable != nil && ledger.Stable.Repository != cfg.Repository {
			return errors.New("stable ledger source differs")
		}
		if err != nil {
			return err
		}
		var inbox Inbox
		if err := readJSON(filepath.Join(inboxDir, "inbox.json"), &inbox, maxInbox); err != nil {
			if os.IsNotExist(err) {
				return nil
			}
			return err
		}
		if inbox.Schema != 1 {
			return errors.New("unsupported inbox")
		}
		changed := false
		if inbox.Stable != nil {
			n, err := parse(inbox.Stable, cfg)
			if err != nil || n.Channel != "stable" {
				return errors.New("bad stable event")
			}
			if ledger.Stable == nil || newer(n, *ledger.Stable) {
				ledger.Stable = &n
				changed = true
			}
		}
		if inbox.Dev == nil {
			if changed {
				return atomicJSON(ledgerDir, "status.json", ledger)
			}
			return nil
		}
		n, err := parse(inbox.Dev, cfg)
		if err != nil || n.Channel != "dev" {
			return errors.New("bad dev event")
		}
		if ledger.Dev != nil {
			if ledger.Dev.Notice.Repository != cfg.Repository {
				return errors.New("ledger source differs")
			}
			if !newer(n, ledger.Dev.Notice) {
				if changed {
					return atomicJSON(ledgerDir, "status.json", ledger)
				}
				return nil
			}
		}
		ledger.Dev = &Attempt{Notice: n, Phase: "attempted"}
		if err := atomicJSON(ledgerDir, "status.json", ledger); err != nil {
			return err
		}
		phase, runErr := run(ctx, n)
		if runErr != nil {
			phase = "failed"
		}
		ledger.Dev.Phase = phase
		if err := atomicJSON(ledgerDir, "status.json", ledger); err != nil {
			return err
		}
		if runErr != nil {
			return errors.New("fixed cloudctl task failed")
		}
	}
	return ctx.Err()
}

type boundedBuffer struct {
	data bytes.Buffer
	over bool
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	if b.data.Len()+len(p) > 32768 {
		b.over = true
		return 0, errors.New("CLI output limit")
	}
	return b.data.Write(p)
}
func ExecuteCloudctl(ctx context.Context, n Notice) (string, error) {
	if !n.Valid() || n.Channel != "dev" {
		return "failed", errors.New("fixed dev task required")
	}
	ctx, cancel := context.WithTimeout(ctx, 41*time.Minute)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/local/bin/cloudctl", "install", "--channel", "dev", "--repository", n.Repository, "--run-id", strings.TrimPrefix(n.Tag, "dev-"), "--commit", n.Commit, "--automatic")
	var out, stderr boundedBuffer
	command.Stdout = &out
	command.Stderr = &stderr
	if command.Run() != nil || out.over || stderr.over {
		return "failed", errors.New("cloudctl installation failed")
	}
	var result struct {
		Phase string `json:"phase"`
	}
	if json.Unmarshal(out.data.Bytes(), &result) != nil {
		return "failed", errors.New("invalid cloudctl result")
	}
	switch result.Phase {
	case "succeeded", "unchanged", "blocked", "skipped":
		return result.Phase, nil
	}
	return "failed", errors.New("cloudctl result not terminal")
}
