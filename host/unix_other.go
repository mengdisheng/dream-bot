//go:build !linux

package host

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
)

type peerKey struct{}

func acquireStoreLock(dir string) (*os.File, error) {
	return os.OpenFile(filepath.Join(dir, "gateway.lock"), os.O_RDWR|os.O_CREATE, 0600)
}
func releaseStoreLock(f *os.File) {
	if f != nil {
		_ = f.Close()
	}
}

func connectionPeerUID(net.Conn) (uint32, bool) { return 0, false }
func diskSpace(string) (uint64, uint64, error)  { return 1 << 60, 1 << 60, nil }

func peerUID(ctx context.Context) (uint32, bool) {
	u, ok := ctx.Value(peerKey{}).(uint32)
	return u, ok
}
func listenUnix(string) (net.Listener, error) {
	return nil, errors.New("production Unix peer authentication requires Linux")
}
func unixServer(h http.Handler) *http.Server { return &http.Server{Handler: h} }
func rootOwned(string) error                 { return errors.New("root ownership validation requires Linux") }
