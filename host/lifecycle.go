package host

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

type RuntimeClient struct {
	URL            string
	Secrets        Secrets
	HTTP           *http.Client
	CurrentRuntime func() string
}
type RuntimeHTTPError struct{ Status int }

func (e *RuntimeHTTPError) Error() string {
	return fmt.Sprintf("runtime control rejected (%d)", e.Status)
}

func NewRuntimeClient(url string, s Secrets) *RuntimeClient {
	return &RuntimeClient{URL: strings.TrimRight(url, "/"), Secrets: s, HTTP: &http.Client{Timeout: 65 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("runtime redirects disabled") }}}
}
func (s Secrets) Assertion(method, path, requestID, sub, role, session string) string {
	return s.assertionForRuntime(method, path, requestID, sub, role, session, "")
}
func (s Secrets) assertionForRuntime(method, path, requestID, sub, role, session, runtimeID string) string {
	v := map[string]any{"sub": sub, "role": role, "method": method, "path": path, "request_id": requestID, "exp": time.Now().Add(30 * time.Second).Unix(), "deployment_epoch": s.Epoch}
	if session != "" {
		v["session_id"] = session
	}
	if runtimeID != "" {
		v["runtime_id"] = runtimeID
	}
	b, _ := json.Marshal(v)
	payload := base64.RawURLEncoding.EncodeToString(b)
	sig := ed25519.Sign(s.Signing, []byte(payload))
	return payload + "." + base64.RawURLEncoding.EncodeToString(sig)
}
func (r *RuntimeClient) Call(ctx context.Context, method, path string, in, out any) error {
	runtimeID := ""
	if r.CurrentRuntime != nil {
		runtimeID = r.CurrentRuntime()
	}
	return r.callForRuntime(ctx, method, path, in, out, runtimeID)
}

// Maintenance recovery must target the recorded generation even before lifecycle
// adoption, and must never let a late release follow CurrentRuntime to a successor.
func (r *RuntimeClient) callForRuntime(ctx context.Context, method, path string, in, out any, runtimeID string) error {
	var body io.Reader
	if in != nil {
		b, e := json.Marshal(in)
		if e != nil {
			return e
		}
		body = bytes.NewReader(b)
	}
	req, e := http.NewRequestWithContext(ctx, method, r.URL+path, body)
	if e != nil {
		return e
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Personal-Bot-Assertion", r.Secrets.assertionForRuntime(method, path, randomID(), "host", "control", "", runtimeID))
	resp, e := r.HTTP.Do(req)
	if e != nil {
		return e
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		if out != nil {
			// Quiesce returns its pre-gate active-work refusal with HTTP 409.
			// Retain its bounded structured evidence; callers must still check e.
			_ = json.NewDecoder(io.LimitReader(resp.Body, 8192)).Decode(out)
		} else {
			io.Copy(io.Discard, io.LimitReader(resp.Body, 8192))
		}
		return &RuntimeHTTPError{Status: resp.StatusCode}
	}
	if out != nil {
		return json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(out)
	}
	return nil
}

type RuntimeState struct {
	State                   string    `json:"state"`
	Phase                   string    `json:"phase"`
	RuntimeID               string    `json:"runtime_id"`
	ContainerID             string    `json:"container_id"`
	Epoch                   string    `json:"deployment_epoch"`
	RecoveryRequired        bool      `json:"recovery_required"`
	Attempts                int       `json:"attempts"`
	RecoveryRound           string    `json:"recovery_round"`
	Error                   string    `json:"error"`
	DiagnosticID            string    `json:"diagnostic_id"`
	PhaseStarted            time.Time `json:"phase_started"`
	ReadyAt                 time.Time `json:"ready_at"`
	LastActive              time.Time `json:"last_active"`
	ActivityRevision        uint64    `json:"activity_revision,omitempty"`
	IdleSince               time.Time `json:"idle_since"`
	LastRemoved             time.Time `json:"last_removed_at"`
	RemovalReason           string    `json:"last_removal_reason"`
	SafetySuspended         bool      `json:"safety_suspended"`
	SuspensionReason        string    `json:"suspension_reason"`
	MemoryGuardAuditPending bool      `json:"memory_guard_audit_pending,omitempty"`
	IntakeRevision          int64     `json:"intake_revision"`
	NextRetry               time.Time `json:"next_retry"`
	IdleBlockers            []string  `json:"idle_blockers"`
	DrainID                 string    `json:"drain_id,omitempty"`
	DrainRevision           int64     `json:"drain_revision,omitempty"`
	DrainReason             string    `json:"drain_reason,omitempty"`
}
type Ready struct {
	Ready          bool   `json:"ready"`
	RuntimeID      string `json:"runtime_id"`
	Epoch          string `json:"deployment_epoch"`
	Reconciled     bool   `json:"reconciled"`
	IntakeRevision int64  `json:"intake_revision"`
}
type Activity struct {
	RuntimeID       string   `json:"runtime_id"`
	ExecutableTasks int      `json:"executable_tasks"`
	HumanControl    bool     `json:"human_control"`
	LastActivity    string   `json:"last_activity_at"`
	SafeIdle        bool     `json:"safe_idle"`
	IntakeRevision  int64    `json:"intake_revision"`
	IdleSeconds     *float64 `json:"idle_seconds"`
}
type DrainProof struct {
	Safe            bool   `json:"safe_to_remove"`
	Epoch           string `json:"deployment_epoch"`
	RuntimeID       string `json:"runtime_id"`
	DrainID         string `json:"drain_id"`
	IntakeRevision  int64  `json:"intake_revision"`
	PendingOutbox   int    `json:"pending_outbox"`
	PendingExternal int    `json:"pending_external_actions"`
	Reason          string `json:"reason,omitempty"`
}
type Lifecycle struct {
	Store            *Store
	Manager          ContainerManager
	Runtime          *RuntimeClient
	Config           Config
	mu               sync.Mutex
	state            RuntimeState
	changed          chan struct{}
	busy             bool
	stableSince      time.Time
	healthFailures   int
	attemptCounted   bool
	startCancel      context.CancelFunc
	startDone        chan struct{}
	memoryReader     hostMemoryReader
	memoryClock      func() time.Time
	memoryBaseline   bool
	lastOOM          uint64
	lastOOMKills     uint64
	lowMemorySamples int
	lowMemorySince   time.Time
}

// Startup records contain only fixed stage names, identities and measurements.
// They must never include runtime replies, credentials or task content.
func emitStartupTiming(stage, runtimeID string, started time.Time, created, success bool) {
	seconds := time.Since(started).Seconds()
	if seconds < 0 {
		seconds = 0
	}
	record := struct {
		Kind      string  `json:"kind"`
		Stage     string  `json:"stage"`
		Seconds   float64 `json:"seconds"`
		RuntimeID string  `json:"runtime_id"`
		Created   bool    `json:"created"`
		Adopted   bool    `json:"adopted"`
		Success   bool    `json:"success"`
	}{"performance", stage, seconds, runtimeID, created, !created, success}
	encoded, err := json.Marshal(record)
	if err == nil {
		log.Print(string(encoded))
	}
}

func NewLifecycle(s *Store, m ContainerManager, r *RuntimeClient, c Config) (*Lifecycle, error) {
	if e := validateMemoryGuard(c); e != nil {
		return nil, e
	}
	l := &Lifecycle{Store: s, Manager: m, Runtime: r, Config: c, changed: make(chan struct{})}
	l.state = RuntimeState{State: "ABSENT", Phase: "sleeping", Epoch: s.Secrets.Epoch, PhaseStarted: time.Now(), IdleBlockers: []string{}}
	var raw string
	e := s.DB.QueryRow("SELECT value FROM settings WHERE key='lifecycle'").Scan(&raw)
	if e == nil {
		if e = json.Unmarshal([]byte(raw), &l.state); e != nil {
			return nil, e
		}
	} else if !errors.Is(e, sql.ErrNoRows) {
		return nil, e
	}
	if l.state.Epoch != s.Secrets.Epoch {
		return nil, errors.New("control state deployment epoch mismatch; restore reconciliation required")
	}
	// Reconcile persisted phases before the gateway can serve HTTP. Run must
	// never apply an old startup snapshot over a newly started generation.
	if l.state.State == "DRAINING" || l.state.State == "STOPPING" {
		l.setPhaseLocked("DRAINING", "awaiting_handoff")
		l.state.NextRetry = time.Time{}
	} else if l.state.State != "ABSENT" && l.state.State != "FAULTED" {
		l.state.State = "ABSENT"
		l.state.RecoveryRequired = true
	}
	if e = l.persistLocked(); e != nil {
		return nil, e
	}
	r.CurrentRuntime = func() string { return l.Snapshot().RuntimeID }
	return l, nil
}
func (l *Lifecycle) persistLocked() error {
	b, e := json.Marshal(l.state)
	if e != nil {
		return e
	}
	_, e = l.Store.DB.Exec("INSERT INTO settings(key,value) VALUES('lifecycle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(b))
	return e
}
func (l *Lifecycle) signalLocked() { close(l.changed); l.changed = make(chan struct{}) }
func (l *Lifecycle) setPhaseLocked(state, phase string) {
	l.state.State = state
	l.state.Phase = phase
	l.state.PhaseStarted = time.Now()
	l.signalLocked()
}
func (l *Lifecycle) Snapshot() RuntimeState {
	l.mu.Lock()
	defer l.mu.Unlock()
	v := l.state
	v.IdleBlockers = append([]string{}, v.IdleBlockers...)
	return v
}
func (l *Lifecycle) Status() map[string]any {
	s := l.Snapshot()
	nullable := func(v string) any {
		if v == "" {
			return nil
		}
		return v
	}
	when := func(v time.Time) any {
		if v.IsZero() {
			return nil
		}
		return timestamp(v)
	}
	var deadline any
	if !s.IdleSince.IsZero() {
		deadline = timestamp(s.IdleSince.Add(time.Duration(l.Config.IdleSeconds) * time.Second))
	}
	return map[string]any{"state": s.State, "phase": s.Phase, "runtime_id": nullable(s.RuntimeID), "elapsed_seconds": int(time.Since(s.PhaseStarted).Seconds()), "server_time": timestamp(time.Now()), "idle_since": when(s.IdleSince), "idle_deadline": deadline, "idle_blockers": s.IdleBlockers, "last_removed_at": when(s.LastRemoved), "last_removal_reason": nullable(s.RemovalReason), "safety_suspended": s.SafetySuspended, "suspension_reason": nullable(s.SuspensionReason), "error": nullable(s.Error), "diagnostic_id": nullable(s.DiagnosticID)}
}

func (l *Lifecycle) EnsureReady(ctx context.Context) error {
	return l.ensureReady(ctx, false)
}

// Active admission and activity registration share the same transition lock.
// Otherwise maintenance could claim the old idle revision after readiness was
// checked but before the gateway registered the request about to be proxied.
func (l *Lifecycle) EnsureActiveReady(ctx context.Context) error {
	return l.ensureReady(ctx, true)
}

func (l *Lifecycle) ensureReady(ctx context.Context, active bool) error {
	for {
		l.mu.Lock()
		if l.state.SafetySuspended {
			l.mu.Unlock()
			return errors.New("testing is suspended by local management")
		}
		if l.Store.Frozen() {
			l.mu.Unlock()
			return errors.New("maintenance is active")
		}
		if l.state.State == "FAULTED" {
			l.mu.Unlock()
			return errors.New("runtime is faulted; explicit retry required")
		}
		if !l.busy && (l.state.State == "DRAINING" || l.state.State == "STOPPING" || l.state.DrainID != "") {
			l.mu.Unlock()
			return errors.New("runtime control handoff is still pending")
		}
		if l.state.State == "READY" {
			if active {
				if err := l.markActiveLocked(); err != nil {
					l.mu.Unlock()
					return err
				}
			}
			l.mu.Unlock()
			return nil
		}
		if !l.busy {
			l.busy = true
			l.setPhaseLocked("STARTING", "inspecting")
			if e := l.persistLocked(); e != nil {
				l.busy = false
				l.mu.Unlock()
				return e
			}
			l.launchStartLocked()
		}
		ch := l.changed
		l.mu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ch:
		}
		l.mu.Lock()
		failed := !l.busy && l.state.State != "READY" && (l.state.Error != "" || l.state.State == "FAULTED" || l.state.SafetySuspended)
		message := l.state.Error
		l.mu.Unlock()
		if failed {
			return errors.New(message)
		}
	}
}

func (l *Lifecycle) launchStartLocked() {
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(l.Config.StartupSeconds)*time.Second)
	done := make(chan struct{})
	l.startCancel, l.startDone = cancel, done
	go func() {
		defer func() {
			cancel()
			l.mu.Lock()
			if l.startDone == done {
				l.startCancel, l.startDone = nil, nil
			}
			close(done)
			l.mu.Unlock()
		}()
		l.start(ctx)
	}()
}

func (l *Lifecycle) start(ctx context.Context) {
	started := time.Now()
	l.mu.Lock()
	l.attemptCounted = false
	if l.state.SafetySuspended || ctx.Err() != nil {
		l.busy = false
		l.setPhaseLocked("ABSENT", "suspended")
		_ = l.persistLocked()
		l.mu.Unlock()
		return
	}
	l.mu.Unlock()
	v, e := l.Manager.Inspect(ctx)
	if e != nil {
		l.failStart("container inspection failed")
		return
	}
	l.mu.Lock()
	if l.state.SafetySuspended {
		l.busy = false
		l.setPhaseLocked("ABSENT", "suspended")
		_ = l.persistLocked()
		l.mu.Unlock()
		return
	}
	expected := l.state.RuntimeID
	epoch := l.state.Epoch
	l.mu.Unlock()
	if v.Exists {
		if v.Epoch != epoch || (expected != "" && v.RuntimeID != expected) {
			l.failStart("existing runtime identity mismatch")
			return
		}
		if v.State != "running" {
			if e = l.Manager.Remove(ctx, RemoveRequest{v.ContainerID, v.RuntimeID, v.Epoch}); e != nil {
				l.failStart("cannot remove exited project runtime")
				return
			}
			v.Exists = false
		}
	}
	created := !v.Exists
	if created {
		var memorySnapshot hostMemorySnapshot
		var memoryErr error
		if memoryGuardEnabled(l.Config) {
			memorySnapshot, memoryErr = l.memoryMetrics()
		}
		l.mu.Lock()
		if l.state.SafetySuspended || ctx.Err() != nil {
			l.busy = false
			l.setPhaseLocked("ABSENT", "suspended")
			_ = l.persistLocked()
			l.mu.Unlock()
			return
		}
		if memoryReason := l.admissionMemoryReasonLocked(memorySnapshot, memoryErr); memoryReason != "" {
			l.blockMemoryAdmissionLocked(memoryReason)
			l.mu.Unlock()
			return
		}
		l.resetMemoryPressureLocked()
		if l.state.Attempts >= 3 {
			l.busy = false
			l.setPhaseLocked("FAULTED", "recovery_limit")
			l.state.Error = "three recovery attempts exhausted"
			_ = l.persistLocked()
			l.mu.Unlock()
			return
		}
		if l.state.RecoveryRound == "" {
			l.state.RecoveryRound = randomID()
		}
		l.state.Attempts++
		l.attemptCounted = true
		l.state.RecoveryRequired = true
		l.state.RuntimeID = randomID()
		l.state.ContainerID = ""
		l.state.IdleSince = time.Time{}
		l.state.IdleBlockers = []string{}
		l.state.DrainID = ""
		l.state.DrainRevision = 0
		l.state.DrainReason = ""
		l.state.Error = ""
		l.state.DiagnosticID = ""
		l.setPhaseLocked("STARTING", "creating")
		request := EnsureRequest{l.state.RuntimeID, l.state.Epoch}
		e = l.persistLocked()
		l.mu.Unlock()
		if e != nil {
			l.failStart("cannot persist recovery intent")
			return
		}
		createStarted := time.Now()
		v, e = l.Manager.Ensure(ctx, request)
		emitStartupTiming("container_create", request.RuntimeID, createStarted, true, e == nil)
		if e != nil {
			l.failStart("container creation failed")
			return
		}
	}
	l.mu.Lock()
	l.state.RuntimeID = v.RuntimeID
	l.state.ContainerID = v.ContainerID
	if l.state.SafetySuspended || ctx.Err() != nil {
		l.failStartLocked("runtime startup was cancelled")
		return
	}
	l.state.RecoveryRequired = true
	l.setPhaseLocked("RECOVERING", "reconciling")
	e = l.persistLocked()
	l.mu.Unlock()
	if e != nil {
		l.failStart("cannot persist runtime identity")
		return
	}
	for {
		var ready Ready
		callCtx, stop := context.WithTimeout(ctx, 3*time.Second)
		e = l.Runtime.Call(callCtx, "GET", "/health/ready", nil, &ready)
		stop()
		if e == nil && ready.Ready && ready.Reconciled && ready.RuntimeID == v.RuntimeID && ready.Epoch == epoch {
			l.mu.Lock()
			if l.state.SafetySuspended || ctx.Err() != nil {
				l.failStartLocked("runtime startup was cancelled")
				return
			}
			l.state.IntakeRevision = ready.IntakeRevision
			l.state.ReadyAt = time.Now()
			if l.state.LastActive.IsZero() || created {
				l.state.LastActive = l.state.ReadyAt
			}
			if created {
				l.state.IdleSince = time.Time{}
			}
			l.stableSince = time.Now()
			l.healthFailures = 0
			l.attemptCounted = l.state.Attempts > 0
			l.busy = false
			l.setPhaseLocked("READY", "ready")
			l.state.Error = ""
			l.state.DiagnosticID = ""
			e = l.persistLocked()
			if e != nil {
				l.failStartLocked("cannot persist ready state")
				return
			}
			l.mu.Unlock()
			emitStartupTiming("runtime_ready", v.RuntimeID, started, created, true)
			return
		}
		select {
		case <-ctx.Done():
			l.failStart("runtime startup or reconciliation timed out")
			return
		case <-time.After(400 * time.Millisecond):
		}
	}
}
func (l *Lifecycle) failStart(message string) {
	l.mu.Lock()
	l.failStartLocked(message)
}

// failObservedRuntime consumes the lock only if this health observation still
// belongs to the current READY generation. Slow Docker/HTTP results must never
// remove a replacement runtime or resurrect a normally removed instance.
func (l *Lifecycle) failObservedRuntime(observed RuntimeState, message string) {
	l.mu.Lock()
	if l.busy || !l.matchesSnapshotLocked(observed) {
		l.mu.Unlock()
		return
	}
	l.failStartLocked(message)
}

// Caller holds mu; this function releases it while performing exact cleanup.
func (l *Lifecycle) failStartLocked(message string) {
	l.busy = true
	if !l.attemptCounted {
		l.state.Attempts++
		l.attemptCounted = true
	}
	if l.state.RecoveryRound == "" {
		l.state.RecoveryRound = randomID()
	}
	expected, epoch := l.state.RuntimeID, l.state.Epoch
	l.setPhaseLocked("RECOVERING", "removing_failed_runtime")
	_ = l.persistLocked()
	l.mu.Unlock()
	cleanup, stop := context.WithTimeout(context.Background(), 20*time.Second)
	instance, inspectErr := l.Manager.Inspect(cleanup)
	cleanupFailed := inspectErr != nil
	if inspectErr == nil && instance.Exists {
		if instance.RuntimeID == expected && instance.Epoch == epoch {
			cleanupFailed = l.Manager.Remove(cleanup, RemoveRequest{instance.ContainerID, instance.RuntimeID, instance.Epoch}) != nil
		} else {
			cleanupFailed = true
		}
	}
	stop()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.busy = false
	l.state.Error = message
	l.state.DiagnosticID = randomID()
	l.state.RecoveryRequired = true
	l.state.NextRetry = time.Now().Add(time.Duration(2*(l.state.Attempts+1)) * time.Second)
	if cleanupFailed || l.state.Attempts >= 3 {
		l.setPhaseLocked("FAULTED", "recovery_limit")
	} else {
		l.state.RuntimeID = ""
		l.state.ContainerID = ""
		l.setPhaseLocked("ABSENT", "startup_failed")
	}
	_ = l.persistLocked()
	if l.state.State == "FAULTED" {
		_ = l.Store.RuntimeFaultNotice(l.state.RecoveryRound)
	}
}
func (l *Lifecycle) MarkActive() {
	l.mu.Lock()
	defer l.mu.Unlock()
	_ = l.markActiveLocked()
}

func (l *Lifecycle) markActiveLocked() error {
	l.state.LastActive = time.Now()
	l.state.ActivityRevision++
	l.state.IdleSince = time.Time{}
	return l.persistLocked()
}

func (l *Lifecycle) matchesSnapshotLocked(observed RuntimeState) bool {
	return l.state.State == observed.State && l.state.RuntimeID == observed.RuntimeID &&
		l.state.ContainerID == observed.ContainerID && l.state.Epoch == observed.Epoch &&
		l.state.DrainID == observed.DrainID && l.state.SafetySuspended == observed.SafetySuspended
}

func (l *Lifecycle) Tick(ctx context.Context) {
	s := l.Snapshot()
	if s.SafetySuspended {
		l.retrySafetySuspension(s)
		return
	}
	if s.State == "DRAINING" {
		if time.Now().After(s.NextRetry) {
			_ = l.stopObservedRuntime(ctx, false, s.DrainReason, &s)
		}
		return
	}
	if s.State == "ABSENT" && s.RecoveryRequired && time.Now().After(s.NextRetry) && !l.Store.Frozen() {
		l.mu.Lock()
		if !l.busy && l.matchesSnapshotLocked(s) && l.state.RecoveryRequired && !l.state.SafetySuspended &&
			l.state.DrainID == "" && l.state.Attempts < 3 && time.Now().After(l.state.NextRetry) && !l.Store.Frozen() {
			l.busy = true
			l.setPhaseLocked("STARTING", "automatic_recovery")
			_ = l.persistLocked()
			l.launchStartLocked()
		}
		l.mu.Unlock()
		return
	}
	if s.State != "READY" {
		return
	}
	if !l.runningMemorySafe(s) {
		return
	}
	inspectCtx, stop := context.WithTimeout(ctx, 4*time.Second)
	v, e := l.Manager.Inspect(inspectCtx)
	stop()
	if e != nil {
		return
	}
	if !v.Exists || v.RuntimeID != s.RuntimeID || v.State != "running" {
		l.failObservedRuntime(s, "runtime exited unexpectedly")
		return
	}
	l.mu.Lock()
	current := !l.busy && l.matchesSnapshotLocked(s)
	l.mu.Unlock()
	if !current {
		return
	}
	var a Activity
	activityCtx, stop := context.WithTimeout(ctx, 4*time.Second)
	e = l.Runtime.Call(activityCtx, "GET", "/internal/activity", nil, &a)
	stop()
	if e != nil || a.RuntimeID != s.RuntimeID {
		l.mu.Lock()
		if l.busy || !l.matchesSnapshotLocked(s) {
			l.mu.Unlock()
			return
		}
		l.healthFailures++
		n := l.healthFailures
		l.mu.Unlock()
		if n >= 3 {
			_ = l.stopObservedRuntime(ctx, true, "unhealthy_runtime", &s)
		}
		return
	}
	now := time.Now()
	l.mu.Lock()
	if l.busy || !l.matchesSnapshotLocked(s) {
		l.mu.Unlock()
		return
	}
	l.healthFailures = 0
	l.state.IntakeRevision = a.IntakeRevision
	l.state.IdleBlockers = []string{}
	if a.ExecutableTasks > 0 {
		l.state.IdleBlockers = append(l.state.IdleBlockers, "task")
	}
	if a.HumanControl {
		l.state.IdleBlockers = append(l.state.IdleBlockers, "human_control")
	}
	if !a.SafeIdle && len(l.state.IdleBlockers) == 0 {
		l.state.IdleBlockers = append(l.state.IdleBlockers, "active_operation")
	}
	if !a.SafeIdle || len(l.state.IdleBlockers) > 0 {
		l.state.IdleSince = time.Time{}
	} else {
		last := l.state.LastActive
		if t, e := time.Parse(time.RFC3339Nano, a.LastActivity); e == nil && t.After(last) && !t.After(now) {
			last = t
		}
		if a.IdleSeconds != nil && *a.IdleSeconds >= 0 {
			t := now.Add(-time.Duration(*a.IdleSeconds * float64(time.Second)))
			if t.After(last) {
				last = t
			}
		}
		if last.IsZero() {
			last = l.state.ReadyAt
		}
		l.state.IdleSince = last
	}
	idle := !l.state.IdleSince.IsZero() && now.Sub(l.state.IdleSince) >= time.Duration(l.Config.IdleSeconds)*time.Second
	if !l.stableSince.IsZero() && now.Sub(l.stableSince) >= 10*time.Minute && l.state.RecoveryRound != "" {
		l.state.Attempts = 0
		l.state.RecoveryRound = ""
	}
	_ = l.persistLocked()
	l.mu.Unlock()
	if idle {
		_ = l.stopObservedRuntime(ctx, false, "idle", &s)
	}
}

func (l *Lifecycle) Stop(ctx context.Context, force bool, reason string) error {
	return l.stopObservedRuntime(ctx, force, reason, nil)
}

func (l *Lifecycle) stopObservedRuntime(ctx context.Context, force bool, reason string, observed *RuntimeState) error {
	l.mu.Lock()
	if reason == "idle" && l.Store.Frozen() {
		l.mu.Unlock()
		return errors.New("maintenance owns the runtime handoff")
	}
	if observed != nil && (!l.matchesSnapshotLocked(*observed) ||
		(reason == "idle" && (!l.state.LastActive.Equal(observed.LastActive) || l.state.ActivityRevision != observed.ActivityRevision))) {
		l.mu.Unlock()
		return errors.New("runtime observation is no longer current")
	}
	if l.busy {
		l.mu.Unlock()
		return errors.New("lifecycle transition already in progress")
	}
	if l.state.RuntimeID == "" {
		l.mu.Unlock()
		return nil
	}
	l.busy = true
	s := l.state
	// Once a safety pause is set, releasing host memory must not depend on a
	// writable control database. Keep the exact identity binding below and
	// retain the pause/pending audit while retrying persistence on later ticks.
	emergency := force && s.SafetySuspended
	if !force && l.state.DrainID == "" {
		l.state.DrainID = newUUID()
		l.state.DrainRevision = s.IntakeRevision
		l.state.DrainReason = reason
	}
	drainID, drainRevision := l.state.DrainID, l.state.DrainRevision
	if l.state.DrainReason != "" && !force {
		reason = l.state.DrainReason
	}
	l.setPhaseLocked("DRAINING", "saving")
	e := l.persistLocked()
	if e != nil && !emergency {
		l.busy = false
		l.state = s
		l.signalLocked()
	}
	l.mu.Unlock()
	if e != nil && !emergency {
		return e
	}
	safe := false
	if !force {
		// The one-way gate makes a durably stored proof reusable after a gateway
		// crash or transient Docker failure. Never require a vanished API to
		// quiesce again after STOPPING was persisted.
		var stored string
		proofErr := l.Store.DB.QueryRow("SELECT proof FROM lifecycle_proofs WHERE runtime_id=? AND drain_id=?", s.RuntimeID, drainID).Scan(&stored)
		if proofErr == nil {
			var proof DrainProof
			if json.Unmarshal([]byte(stored), &proof) == nil {
				safe = validDrainProof(proof, s.RuntimeID, s.Epoch, drainID, drainRevision)
			}
		}
	}
	if !force && !safe {
		drainCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
		var proof DrainProof
		for {
			e = l.Runtime.Call(drainCtx, "POST", "/internal/quiesce", map[string]any{"drain_id": drainID, "expected_intake_revision": drainRevision, "runtime_id": s.RuntimeID, "deployment_epoch": s.Epoch}, &proof)
			safe = e == nil && validDrainProof(proof, s.RuntimeID, s.Epoch, drainID, drainRevision)
			if safe {
				break
			}
			// Only an explicit pre-gate active-work response can return READY.
			// A transport error/409 may have arrived after the one-way gate closed.
			var rejected *RuntimeHTTPError
			preGateResponse := e == nil || errors.As(e, &rejected) && rejected.Status == http.StatusConflict
			if preGateResponse && !proof.Safe && proof.Reason == "active_work" && proof.RuntimeID == s.RuntimeID && proof.DrainID == "" {
				cancel()
				l.mu.Lock()
				l.busy = false
				l.state.DrainID = ""
				l.state.DrainRevision = 0
				l.state.DrainReason = ""
				l.setPhaseLocked("READY", "busy")
				_ = l.persistLocked()
				l.mu.Unlock()
				return errors.New("runtime has executable work or its gate changed")
			}
			proof = DrainProof{}
			select {
			case <-drainCtx.Done():
				e = drainCtx.Err()
			case <-time.After(500 * time.Millisecond):
				continue
			}
			break
		}
		cancel()
		if safe {
			b, _ := json.Marshal(proof)
			_, e = l.Store.DB.Exec("INSERT OR REPLACE INTO lifecycle_proofs(runtime_id,drain_id,proof,created_at) VALUES(?,?,?,?)", s.RuntimeID, drainID, b, timestamp(time.Now()))
			if e != nil {
				safe = false
			}
		}
		if !safe {
			l.mu.Lock()
			l.busy = false
			l.state.RecoveryRequired = true
			l.state.Error = "runtime control handoff is incomplete; container retained"
			l.state.DiagnosticID = randomID()
			l.state.NextRetry = time.Now().Add(5 * time.Second)
			l.setPhaseLocked("DRAINING", "awaiting_handoff")
			persistErr := l.persistLocked()
			l.mu.Unlock()
			if persistErr != nil {
				return persistErr
			}
			return errors.New("runtime control handoff is incomplete; container retained")
		}
	}
	l.mu.Lock()
	l.setPhaseLocked("STOPPING", "removing")
	l.state.RecoveryRequired = true
	e = l.persistLocked()
	if e != nil && !emergency {
		l.busy = false
		l.setPhaseLocked("DRAINING", "awaiting_removal")
	}
	l.mu.Unlock()
	if e != nil && !emergency {
		return e
	}
	removeCtx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	v, inspectErr := l.Manager.Inspect(removeCtx)
	if inspectErr == nil && v.Exists {
		if v.RuntimeID != s.RuntimeID || v.Epoch != s.Epoch || (s.ContainerID != "" && v.ContainerID != s.ContainerID) {
			e = errors.New("refusing to remove mismatched runtime")
		} else {
			e = l.Manager.Remove(removeCtx, RemoveRequest{v.ContainerID, v.RuntimeID, v.Epoch})
		}
	} else {
		e = inspectErr
	}
	cancel()
	l.mu.Lock()
	defer l.mu.Unlock()
	l.busy = false
	if e != nil {
		l.state.Error = "runtime removal failed"
		l.state.DiagnosticID = randomID()
		if safe {
			l.state.NextRetry = time.Now().Add(5 * time.Second)
			l.setPhaseLocked("DRAINING", "removal_failed")
		} else {
			l.setPhaseLocked("FAULTED", "removal_failed")
		}
		_ = l.persistLocked()
		return e
	}
	l.state.ContainerID = ""
	l.state.RuntimeID = ""
	l.state.LastRemoved = time.Now()
	l.state.RemovalReason = reason
	l.state.IdleSince = time.Time{}
	l.state.LastActive = time.Time{}
	l.state.DrainID = ""
	l.state.DrainRevision = 0
	l.state.DrainReason = ""
	if safe {
		l.state.RecoveryRequired = false
		l.state.Attempts = 0
		l.state.RecoveryRound = ""
		l.state.Error = ""
		l.state.DiagnosticID = ""
	} else {
		l.state.RecoveryRequired = true
		l.state.NextRetry = time.Now().Add(3 * time.Second)
	}
	if !safe && l.state.Attempts >= 3 {
		l.setPhaseLocked("FAULTED", "recovery_limit")
	} else {
		l.setPhaseLocked("ABSENT", "sleeping")
	}
	_ = l.persistLocked()
	return nil
}

func validDrainProof(proof DrainProof, runtimeID, epoch, drainID string, revision int64) bool {
	return proof.Safe && proof.Epoch == epoch && proof.RuntimeID == runtimeID && proof.DrainID == drainID && proof.IntakeRevision > revision && proof.PendingOutbox == 0 && proof.PendingExternal == 0
}

func (l *Lifecycle) Suspend(reason string) error {
	return l.suspend(reason, nil)
}

func (l *Lifecycle) suspend(reason string, observed *RuntimeState) error {
	l.mu.Lock()
	if observed != nil && (l.busy || l.state.SafetySuspended || !l.matchesSnapshotLocked(*observed)) {
		l.mu.Unlock()
		return errors.New("runtime observation is no longer current")
	}
	previous := l.state
	l.state.SafetySuspended = true
	l.state.SuspensionReason = reason
	e := l.persistAuditLocked("safety.suspend", reason)
	if e != nil {
		l.state = previous
		l.signalLocked()
		l.mu.Unlock()
		return e
	}
	cancel, done := l.startCancel, l.startDone
	l.signalLocked()
	l.mu.Unlock()
	return l.finishSuspension(cancel, done)
}

func (l *Lifecycle) finishSuspension(cancel context.CancelFunc, done chan struct{}) error {
	if cancel != nil {
		cancel()
	}
	if done != nil {
		select {
		case <-done:
		case <-time.After(35 * time.Second):
			return errors.New("startup cancellation has not finished; safety suspension remains enabled")
		}
	}
	s := l.Snapshot()
	if !s.SafetySuspended {
		return errors.New("safety suspension was explicitly cleared")
	}
	if s.RuntimeID != "" {
		if e := l.stopObservedRuntime(context.Background(), true, "safety_suspended", &s); e != nil {
			return e
		}
	}
	verifyCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	instance, e := l.Manager.Inspect(verifyCtx)
	if e != nil {
		return e
	}
	if instance.Exists {
		return errors.New("runtime is still present; safety suspension remains enabled")
	}
	return nil
}
func (l *Lifecycle) Resume() error {
	l.mu.Lock()
	previous := l.state
	l.state.SafetySuspended = false
	l.state.SuspensionReason = ""
	l.state.MemoryGuardAuditPending = false
	e := l.persistAuditLocked("safety.resume", "local administrator")
	if e != nil {
		l.state = previous
	} else {
		l.resetMemoryPressureLocked()
	}
	l.signalLocked()
	l.mu.Unlock()
	return e
}
func (l *Lifecycle) Retry() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.busy || l.state.SafetySuspended || l.state.State == "DRAINING" || l.state.State == "STOPPING" || l.state.DrainID != "" {
		return errors.New("runtime is busy or safety suspended")
	}
	previous := l.state
	l.state.Attempts = 0
	l.state.RecoveryRound = randomID()
	l.state.Error = ""
	l.state.DiagnosticID = ""
	l.setPhaseLocked("ABSENT", "explicit_retry")
	if e := l.persistAuditLocked("runtime.retry", l.state.RecoveryRound); e != nil {
		l.state = previous
		l.signalLocked()
		return e
	}
	return nil
}

func (l *Lifecycle) persistAuditLocked(kind, subject string) error {
	b, e := json.Marshal(l.state)
	if e != nil {
		return e
	}
	tx, e := l.Store.DB.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	if _, e = tx.Exec("INSERT INTO settings(key,value) VALUES('lifecycle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(b)); e != nil {
		return e
	}
	if _, e = tx.Exec("INSERT INTO audit(kind,subject,created_at) VALUES(?,?,?)", kind, subject, timestamp(time.Now())); e != nil {
		return e
	}
	return tx.Commit()
}

func (l *Lifecycle) Run(ctx context.Context) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	l.Tick(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			l.Tick(ctx)
		}
	}
}
