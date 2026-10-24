// release-notify is an unprivileged webhook receiver; it has no command runner.
package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/stewardful/deploy-delivery/notify"
)

// systemd credentials are read-only 0440 files in a service-specific 0550
// directory. Accept that layout only at the manager-provided credential path;
// ordinary secret files must remain 0600/0400 with no group/other access.
func privateSecret(path string, mode os.FileMode) bool {
	if mode.Perm()&0o077 == 0 {
		return true
	}
	credentials := os.Getenv("CREDENTIALS_DIRECTORY")
	if mode.Perm() != 0o440 || !filepath.IsAbs(credentials) || filepath.Clean(path) != filepath.Join(credentials, "webhook.secret") {
		return false
	}
	parent, err := os.Lstat(credentials)
	return err == nil && parent.IsDir() && parent.Mode().Perm()&0o027 == 0
}

func readSecret(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || !privateSecret(path, info.Mode()) || info.Size() > 1024 {
		return nil, fmt.Errorf("secret must be a private regular file of at most 1024 bytes")
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("cannot open secret file")
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !os.SameFile(info, opened) {
		return nil, fmt.Errorf("secret file changed")
	}
	data, err := io.ReadAll(io.LimitReader(file, 1025))
	if err != nil || len(data) > 1024 {
		return nil, fmt.Errorf("cannot read bounded secret")
	}
	data = bytes.TrimSpace(data)
	if len(data) < 32 || bytes.ContainsAny(data, "\r\n") {
		return nil, fmt.Errorf("secret must be one line of at least 32 bytes")
	}
	return data, nil
}

func loopbackAddress(address string) bool {
	host, port, err := net.SplitHostPort(address)
	number, numberErr := strconv.Atoi(port)
	if err != nil || numberErr != nil || number < 1 || number > 65535 {
		return false
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8090", "Loopback HTTP address; expose only the webhook through HTTPS reverse proxy")
	repository := flag.String("repository", "stewardful/deploy-platform", "One allowed source repository")
	secretFile := flag.String("secret-file", "", "Private webhook secret file; not the secret value")
	stateDir := flag.String("state-dir", "./state", "Private persistent directory")
	flag.Parse()
	if flag.NArg() != 0 || !loopbackAddress(*listen) {
		log.Fatal("no positional arguments; a literal loopback IP and nonzero port are required")
	}
	secret, err := readSecret(*secretFile)
	if err != nil {
		log.Fatal(err)
	}
	store, err := notify.OpenStore(*stateDir)
	if err != nil {
		log.Fatal("cannot open notification state; check its permissions and format")
	}
	defer store.Close()
	handler, err := notify.NewHandler(notify.Config{Repository: *repository, Secret: secret}, store)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	server := &http.Server{Addr: *listen, Handler: handler, ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 8 * 1024,
		BaseContext: func(net.Listener) context.Context { return ctx }}
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	log.Printf("release notifications listening on %s", *listen)
	if err := server.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
		log.Fatal("notification listener failed")
	}
	<-stopped
}
