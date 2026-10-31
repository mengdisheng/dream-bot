//go:build linux

package host

import (
	"context"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"syscall"
)

type peerKey struct{}

func peerUID(ctx context.Context) (uint32, bool) {
	u, ok := ctx.Value(peerKey{}).(uint32)
	return u, ok
}
func connectionPeerUID(c net.Conn) (uint32, bool) {
	return peerUID(peerContext(context.Background(), c))
}
func diskSpace(path string) (available, total uint64, err error) {
	var st syscall.Statfs_t
	if err = syscall.Statfs(path, &st); err != nil {
		return
	}
	return st.Bavail * uint64(st.Bsize), st.Blocks * uint64(st.Bsize), nil
}
func acquireStoreLock(dir string) (*os.File, error) {
	f, e := os.OpenFile(filepath.Join(dir, "gateway.lock"), os.O_RDWR|os.O_CREATE, 0600)
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		f.Close()
		return nil, errors.New("control database is owned by a running gateway")
	}
	return f, nil
}
func releaseStoreLock(f *os.File) {
	if f != nil {
		_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
		_ = f.Close()
	}
}
func peerContext(ctx context.Context, c net.Conn) context.Context {
	u, ok := c.(*net.UnixConn)
	if !ok {
		return ctx
	}
	raw, e := u.SyscallConn()
	if e != nil {
		return ctx
	}
	var cred *syscall.Ucred
	var callErr error
	e = raw.Control(func(fd uintptr) {
		cred, callErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	})
	if e != nil || callErr != nil || cred == nil {
		return ctx
	}
	return context.WithValue(ctx, peerKey{}, cred.Uid)
}
func listenUnix(path string) (net.Listener, error) {
	if e := os.MkdirAll(filepath.Dir(path), 0770); e != nil {
		return nil, e
	}
	if st, e := os.Lstat(path); e == nil {
		if st.Mode()&os.ModeSocket == 0 {
			return nil, errors.New("socket path is occupied by a non-socket")
		}
		if e = os.Remove(path); e != nil {
			return nil, e
		}
	} else if !os.IsNotExist(e) {
		return nil, e
	}
	l, e := net.Listen("unix", path)
	if e != nil {
		return nil, e
	}
	if e = os.Chmod(path, 0660); e != nil {
		l.Close()
		return nil, e
	}
	return l, nil
}
func unixServer(h http.Handler) *http.Server {
	return &http.Server{Handler: h, ConnContext: peerContext, ReadHeaderTimeout: 5e9, IdleTimeout: 30e9, MaxHeaderBytes: 16384}
}
func rootOwned(path string) error {
	st, e := os.Lstat(path)
	if e != nil {
		return e
	}
	if !st.Mode().IsRegular() || st.Mode().Perm()&0022 != 0 {
		return errors.New("helper config must be a regular file not writable by group/others")
	}
	v, ok := st.Sys().(*syscall.Stat_t)
	if !ok || v.Uid != 0 {
		return errors.New("helper config must be root owned")
	}
	parent, e := os.Stat(filepath.Dir(path))
	if e != nil {
		return e
	}
	v, ok = parent.Sys().(*syscall.Stat_t)
	if !ok || v.Uid != 0 || parent.Mode().Perm()&0022 != 0 {
		return errors.New("helper config directory must be root controlled")
	}
	return nil
}
