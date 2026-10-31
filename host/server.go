package host

import (
	"context"
	"crypto/rand"
	"encoding/base32"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"time"
)

func (g *Gateway) AdminHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		uid, ok := peerUID(r.Context())
		if !ok || (uid != 0 && uid != uint32(os.Getuid())) {
			writeError(w, 403, "peer_forbidden", "local administrative peer required", false)
			return
		}
		if r.Method != "POST" {
			writeError(w, 405, "method_not_allowed", "POST required", false)
			return
		}
		var out any
		var e error
		switch r.URL.Path {
		case "/v1/status":
			out = g.Lifecycle.Status()
		case "/v1/doctor":
			var sqlite string
			e = g.Store.DB.QueryRow("SELECT sqlite_version()").Scan(&sqlite)
			if e == nil {
				instance, err := g.Lifecycle.Manager.Inspect(r.Context())
				out = map[string]any{"architecture": runtime.GOARCH, "sqlite_version": sqlite, "control_database": true, "helper_reachable": err == nil, "container": instance, "runtime": g.Lifecycle.Status(), "maintenance": g.MaintenanceStatus()}
			}
		case "/v1/suspend":
			var v struct {
				Reason string `json:"reason"`
			}
			if !decodeJSON(w, r, &v) {
				return
			}
			if len(v.Reason) < 1 || len(v.Reason) > 300 {
				writeError(w, 400, "reason_required", "provide a short suspension reason", false)
				return
			}
			e = g.Lifecycle.Suspend(v.Reason)
			out = map[string]bool{"suspended": e == nil}
		case "/v1/resume":
			e = g.Lifecycle.Resume()
			out = map[string]bool{"resumed": e == nil}
		case "/v1/retry":
			e = g.Lifecycle.Retry()
			if e == nil {
				e = g.Lifecycle.EnsureReady(r.Context())
			}
			out = g.Lifecycle.Status()
		case "/v1/stop":
			var v struct {
				Force bool `json:"force"`
			}
			if !decodeJSON(w, r, &v) {
				return
			}
			e = g.Lifecycle.Stop(r.Context(), v.Force, "local_management")
			out = g.Lifecycle.Status()
		case "/v1/backup":
			out, e = g.Backup(r.Context())
		case "/v1/recover-owner":
			if uid != 0 {
				writeError(w, 403, "root_required", "owner recovery requires local root", false)
				return
			}
			var v struct {
				Password string `json:"new_password"`
			}
			if !decodeJSON(w, r, &v) {
				return
			}
			if len(v.Password) < 12 || len(v.Password) > 1024 {
				writeError(w, 400, "password_invalid", "password must contain 12–1024 bytes", false)
				return
			}
			out, e = g.recoverOwner(v.Password)
		default:
			writeError(w, 404, "not_found", "unknown fixed administrative operation", false)
			return
		}
		if e != nil {
			writeError(w, 409, "admin_failed", e.Error(), true)
			return
		}
		writeJSON(w, 200, out)
	})
}

func (g *Gateway) recoverOwner(password string) (map[string]any, error) {
	select {
	case g.authGate <- struct{}{}:
		defer func() { <-g.authGate }()
	default:
		return nil, errors.New("authentication operation is in progress")
	}
	var id, username string
	if e := g.Store.DB.QueryRow("SELECT id,username FROM owner").Scan(&id, &username); e != nil {
		return nil, e
	}
	seed := make([]byte, 20)
	if _, e := rand.Read(seed); e != nil {
		return nil, e
	}
	encrypted, e := g.Store.Secrets.Encrypt(seed, "totp:"+id)
	if e != nil {
		return nil, e
	}
	codes := make([]string, 10)
	for i := range codes {
		codes[i] = randomID()
	}
	tx, e := g.Store.DB.Begin()
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	if _, e = tx.Exec("UPDATE owner SET password=?,totp=?,last_totp_step=0 WHERE id=?", passwordHash(password), encrypted, id); e != nil {
		return nil, e
	}
	for _, query := range []string{"DELETE FROM sessions", "DELETE FROM recovery_codes", "DELETE FROM enrollment", "DELETE FROM login_limits"} {
		if _, e = tx.Exec(query); e != nil {
			return nil, e
		}
	}
	for _, code := range codes {
		if _, e = tx.Exec("INSERT INTO recovery_codes(hash,owner_id) VALUES(?,?)", digest(code), id); e != nil {
			return nil, e
		}
	}
	if _, e = tx.Exec("INSERT INTO audit(kind,subject,created_at) VALUES('owner.local-recovery',?,?)", id, timestamp(time.Now())); e != nil {
		return nil, e
	}
	if e = tx.Commit(); e != nil {
		return nil, e
	}
	g.mu.Lock()
	ids := make([]string, 0, len(g.connections))
	for session := range g.connections {
		ids = append(ids, session)
	}
	g.mu.Unlock()
	for _, session := range ids {
		g.revoke(session)
	}
	return map[string]any{"username": username, "totp_secret": base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(seed), "recovery_codes": codes, "sessions_revoked": true}, nil
}

func ServeGateway(ctx context.Context, c Config) error {
	if _, e := os.Stat(filepath.Join(filepath.Dir(c.StateDir), "restore-in-progress.json")); e == nil {
		return errors.New("interrupted restore must be reconciled by the root helper before gateway startup")
	}
	store, e := OpenStore(c.StateDir)
	if e != nil {
		return e
	}
	defer store.Close()
	if !store.Initialized() {
		if _, e = store.SetupToken(); e != nil {
			return e
		}
	}
	manager := NewHelperClient(c.HelperSocket)
	l, e := NewLifecycle(store, manager, NewRuntimeClient(c.RuntimeURL, store.Secrets), c)
	if e != nil {
		return e
	}
	g, e := NewGateway(c, store, l)
	if e != nil {
		return e
	}
	recovery, finishRecovery := context.WithTimeout(ctx, 10*time.Second)
	_ = g.RecoverMaintenance(recovery)
	finishRecovery()
	host, _, e := net.SplitHostPort(c.Listen)
	if e != nil || !(host == "127.0.0.1" || host == "::1") {
		return errors.New("gateway must bind a loopback address")
	}
	listener, e := net.Listen("tcp", c.Listen)
	if e != nil {
		return e
	}
	defer listener.Close()
	if e = os.MkdirAll(filepath.Dir(c.AdminSocket), 0700); e != nil {
		return e
	}
	if e = os.MkdirAll(filepath.Dir(c.BridgeSocket), 0750); e != nil {
		return e
	}
	if e = os.Chown(filepath.Dir(c.BridgeSocket), -1, c.IPCGID); e != nil {
		return e
	}
	if e = os.Chmod(filepath.Dir(c.BridgeSocket), 0750); e != nil {
		return e
	}
	bridge, e := listenUnix(c.BridgeSocket)
	if e != nil {
		return e
	}
	defer bridge.Close()
	if e = os.Chown(c.BridgeSocket, -1, c.IPCGID); e != nil {
		return e
	}
	admin, e := listenUnix(c.AdminSocket)
	if e != nil {
		return e
	}
	defer admin.Close()
	publicServer := &http.Server{Handler: g.Handler(), ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	bridgeServer := unixServer(l.BridgeHandler())
	adminServer := unixServer(g.AdminHandler())
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	errorsCh := make(chan error, 3)
	go func() { errorsCh <- publicServer.Serve(listener) }()
	go func() { errorsCh <- bridgeServer.Serve(bridge) }()
	go func() { errorsCh <- adminServer.Serve(admin) }()
	var workers sync.WaitGroup
	workers.Add(3)
	go func() { defer workers.Done(); l.Run(runCtx) }()
	go func() {
		defer workers.Done()
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-runCtx.Done():
				return
			case <-ticker.C:
				work, stop := context.WithTimeout(runCtx, 70*time.Second)
				_ = l.DispatchDue(work)
				stop()
			}
		}
	}()
	go func() {
		defer workers.Done()
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-runCtx.Done():
				return
			case <-ticker.C:
				cleanup, finishCleanup := context.WithTimeout(runCtx, 10*time.Second)
				_ = g.RecoverMaintenance(cleanup)
				finishCleanup()
				work, stop := context.WithTimeout(runCtx, 35*time.Second)
				_ = g.Push.Deliver(work)
				stop()
				g.maybeDailyBackup(runCtx)
			}
		}
	}()
	select {
	case <-ctx.Done():
		e = nil
	case e = <-errorsCh:
		if errors.Is(e, http.ErrServerClosed) {
			e = nil
		}
	}
	cancel()
	g.mu.Lock()
	for _, sessions := range g.connections {
		for _, close := range sessions {
			close()
		}
	}
	g.mu.Unlock()
	shutdown, stop := context.WithTimeout(context.Background(), 10*time.Second)
	defer stop()
	_ = publicServer.Shutdown(shutdown)
	_ = bridgeServer.Shutdown(shutdown)
	_ = adminServer.Shutdown(shutdown)
	workers.Wait()
	return e
}

func (g *Gateway) maybeDailyBackup(ctx context.Context) {
	loc, e := time.LoadLocation("Asia/Shanghai")
	if e != nil {
		return
	}
	now := time.Now().In(loc)
	if now.Hour() < 3 {
		return
	}
	var recipient, last string
	_ = g.Store.DB.QueryRow("SELECT value FROM settings WHERE key='backup_recipient'").Scan(&recipient)
	if recipient == "" {
		recipient = g.Config.BackupRecipient
	}
	if recipient == "" || g.Store.Frozen() || g.Lifecycle.Snapshot().SafetySuspended {
		return
	}
	today := now.Format("2006-01-02")
	_ = g.Store.DB.QueryRow("SELECT value FROM settings WHERE key='daily_backup_attempt'").Scan(&last)
	if last == today {
		return
	}
	state := g.Lifecycle.Snapshot()
	if state.State == "READY" {
		var activity Activity
		check, cancel := context.WithTimeout(ctx, 3*time.Second)
		e = g.Lifecycle.Runtime.Call(check, "GET", "/internal/activity", nil, &activity)
		cancel()
		if e != nil || !dailyBackupIdle(state, activity, time.Now(), g.Config.IdleSeconds) {
			return
		}
	} else if state.State != "ABSENT" {
		return
	}
	backup, cancel := context.WithTimeout(ctx, 15*time.Minute)
	_, e = g.backup(backup, &backupAdmission{Observed: state, DailyDate: today})
	cancel()
	if e != nil {
		_ = g.Store.Audit("backup.daily-failed", "see maintenance status")
	}
}

func dailyBackupIdle(state RuntimeState, activity Activity, now time.Time, idleSeconds int) bool {
	if activity.RuntimeID != state.RuntimeID || !activity.SafeIdle || activity.HumanControl || activity.ExecutableTasks > 0 {
		return false
	}
	if idleSeconds <= 0 {
		idleSeconds = 300
	}
	last := state.LastActive
	if last.IsZero() {
		last = state.ReadyAt
	}
	if stamp, err := time.Parse(time.RFC3339Nano, activity.LastActivity); err == nil && stamp.After(last) {
		last = stamp
	}
	if activity.IdleSeconds != nil {
		if *activity.IdleSeconds < 0 {
			return false
		}
		stamp := now.Add(-time.Duration(*activity.IdleSeconds * float64(time.Second)))
		if stamp.After(last) {
			last = stamp
		}
	}
	return !last.IsZero() && !last.After(now) && now.Sub(last) >= time.Duration(idleSeconds)*time.Second
}

func ServeHelper(ctx context.Context, c HelperConfig) error {
	if os.Getuid() != 0 {
		return errors.New("restricted helper must run as root")
	}
	if e := RecoverRestoreJournal(c.DataRoot); e != nil {
		return e
	}
	if e := os.MkdirAll(filepath.Dir(c.Socket), 0750); e != nil {
		return e
	}
	if e := os.Chown(filepath.Dir(c.Socket), 0, int(c.GatewayGID)); e != nil {
		return e
	}
	if e := os.Chmod(filepath.Dir(c.Socket), 0750); e != nil {
		return e
	}
	listener, e := listenUnix(c.Socket)
	if e != nil {
		return e
	}
	defer listener.Close()
	if e = os.Chown(c.Socket, 0, int(c.GatewayGID)); e != nil {
		return e
	}
	server := unixServer(NewDockerManager(c).Handler())
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	select {
	case e = <-done:
		if errors.Is(e, http.ErrServerClosed) {
			return nil
		}
		return e
	case <-ctx.Done():
		stop, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(stop)
	}
}
