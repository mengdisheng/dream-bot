package host

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func testStore(t *testing.T) *Store {
	t.Helper()
	s, e := OpenStore(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

type fakeManager struct {
	mu               sync.Mutex
	instance         Instance
	creates, removes int
	removeFailures   int
}

func (m *fakeManager) Inspect(context.Context) (Instance, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.instance, nil
}
func (m *fakeManager) Ensure(_ context.Context, r EnsureRequest) (Instance, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.instance.Exists {
		if m.instance.RuntimeID != r.RuntimeID {
			return Instance{}, fmt.Errorf("duplicate runtime")
		}
		return m.instance, nil
	}
	m.creates++
	m.instance = Instance{Exists: true, ContainerID: fmt.Sprintf("%064x", m.creates), RuntimeID: r.RuntimeID, Epoch: r.Epoch, State: "running"}
	return m.instance, nil
}
func (m *fakeManager) Remove(_ context.Context, r RemoveRequest) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.instance.Exists {
		return nil
	}
	if m.instance.ContainerID != r.ContainerID || m.instance.RuntimeID != r.RuntimeID || m.instance.Epoch != r.Epoch {
		return fmt.Errorf("identity mismatch")
	}
	if m.removeFailures > 0 {
		m.removeFailures--
		return fmt.Errorf("test transient removal failure")
	}
	m.removes++
	m.instance = Instance{}
	return nil
}
func (m *fakeManager) crash() { m.mu.Lock(); defer m.mu.Unlock(); m.instance = Instance{} }

type fakeRuntime struct {
	manager          *fakeManager
	ready            atomic.Bool
	busy             atomic.Bool
	drainBusy        atomic.Bool
	drainPending     atomic.Bool
	drainExternal    atomic.Bool
	drainConflict    atomic.Bool
	drainMu          sync.Mutex
	drains           []drainRequest
	dispatches       atomic.Int32
	activityRequests atomic.Int32
}

type drainRequest struct {
	DrainID   string `json:"drain_id"`
	Revision  int64  `json:"expected_intake_revision"`
	RuntimeID string `json:"runtime_id"`
	Epoch     string `json:"deployment_epoch"`
}

func (f *fakeRuntime) serve(w http.ResponseWriter, r *http.Request) {
	v, _ := f.manager.Inspect(r.Context())
	switch r.URL.Path {
	case "/health/ready":
		writeJSON(w, 200, Ready{Ready: f.ready.Load(), Reconciled: f.ready.Load(), RuntimeID: v.RuntimeID, Epoch: v.Epoch, IntakeRevision: 1})
	case "/internal/activity":
		f.activityRequests.Add(1)
		count := 0
		if f.busy.Load() {
			count = 1
		}
		writeJSON(w, 200, Activity{RuntimeID: v.RuntimeID, ExecutableTasks: count, SafeIdle: !f.busy.Load(), LastActivity: timestamp(time.Now().Add(-time.Hour)), IntakeRevision: 1})
	case "/internal/quiesce":
		var in drainRequest
		if !decodeJSON(w, r, &in) {
			return
		}
		f.drainMu.Lock()
		f.drains = append(f.drains, in)
		f.drainMu.Unlock()
		if f.drainBusy.Load() {
			writeJSON(w, 409, DrainProof{Safe: false, RuntimeID: v.RuntimeID, Reason: "active_work"})
			return
		}
		if f.drainConflict.Load() {
			writeError(w, 409, "drain_conflict", "another one-way gate is already closed", true)
			return
		}
		proof := DrainProof{Safe: true, Epoch: v.Epoch, RuntimeID: v.RuntimeID, DrainID: in.DrainID, IntakeRevision: in.Revision + 1}
		if f.drainPending.Load() {
			proof.Safe, proof.PendingOutbox = false, 1
			writeJSON(w, 503, proof)
			return
		}
		if f.drainExternal.Load() {
			// A claimed safe flag cannot override contradictory live-action evidence.
			proof.PendingExternal = 1
		}
		writeJSON(w, 200, proof)
	case "/internal/dispatch":
		var in struct {
			ID      string `json:"event_id"`
			Kind    string `json:"kind"`
			Payload any    `json:"payload"`
		}
		if !decodeJSON(w, r, &in) {
			return
		}
		f.dispatches.Add(1)
		writeJSON(w, 200, map[string]any{"accepted": true, "event_id": in.ID})
	default:
		writeJSON(w, 200, map[string]bool{"ok": true})
	}
}
func testLifecycle(t *testing.T) (*Lifecycle, *fakeManager, *fakeRuntime) {
	t.Helper()
	s := testStore(t)
	m := &fakeManager{}
	f := &fakeRuntime{manager: m}
	f.ready.Store(true)
	server := httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(server.Close)
	c := Config{StateDir: s.Dir, RuntimeURL: server.URL, Origin: "https://bot.example.invalid", IdleSeconds: 300, StartupSeconds: 1, APIPeerUID: 31001}
	l, e := NewLifecycle(s, m, NewRuntimeClient(server.URL, s.Secrets), c)
	if e != nil {
		t.Fatal(e)
	}
	return l, m, f
}
func ensureTest(t *testing.T, l *Lifecycle) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	if e := l.EnsureReady(ctx); e != nil {
		t.Fatal(e)
	}
}

func TestConcurrentWakeAndVerifiedIdleRemoval(t *testing.T) {
	l, m, _ := testLifecycle(t)
	var wg sync.WaitGroup
	errs := make(chan error, 20)
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
			defer cancel()
			errs <- l.EnsureReady(ctx)
		}()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	if m.creates != 1 {
		t.Fatalf("created %d instances", m.creates)
	}
	l.mu.Lock()
	l.state.LastActive = time.Now().Add(-301 * time.Second)
	l.mu.Unlock()
	l.Tick(context.Background())
	s := l.Snapshot()
	if s.State != "ABSENT" || s.RecoveryRequired || m.removes != 1 || s.Attempts != 0 {
		t.Fatalf("unclean idle reclaim: %+v removes=%d", s, m.removes)
	}
	var proofs int
	if e := l.Store.DB.QueryRow("SELECT count(*) FROM lifecycle_proofs").Scan(&proofs); e != nil || proofs != 1 {
		t.Fatal("proof was not durably retained", e)
	}
}
func TestReadyThenCrashThreeTimesDoesNotResetRecovery(t *testing.T) {
	l, m, _ := testLifecycle(t)
	for i := 1; i <= 3; i++ {
		ensureTest(t, l)
		if l.Snapshot().Attempts != i {
			t.Fatalf("attempt counter reset at ready: %+v", l.Snapshot())
		}
		m.crash()
		l.Tick(context.Background())
	}
	s := l.Snapshot()
	if s.State != "FAULTED" || m.creates != 3 {
		t.Fatalf("recovery did not stop: %+v creates=%d", s, m.creates)
	}
	if l.EnsureReady(context.Background()) == nil {
		t.Fatal("faulted runtime accepted an implicit retry")
	}
	reloaded, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	if reloaded.Snapshot().Attempts != 3 || reloaded.Snapshot().State != "FAULTED" {
		t.Fatal("restart lost recovery round")
	}
	if e = l.Retry(); e != nil {
		t.Fatal(e)
	}
	ensureTest(t, l)
	if m.creates != 4 {
		t.Fatal("explicit audited retry did not permit the next round")
	}
}
func TestFailedReadinessRemovesOldExecutorAndStopsAfterThree(t *testing.T) {
	l, m, f := testLifecycle(t)
	f.ready.Store(false)
	for i := 1; i <= 3; i++ {
		ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		e := l.EnsureReady(ctx)
		cancel()
		if e == nil {
			t.Fatal("unready runtime was accepted")
		}
		if l.Snapshot().Attempts != i {
			t.Fatalf("attempt not durable: %+v", l.Snapshot())
		}
		v, _ := m.Inspect(context.Background())
		if v.Exists {
			t.Fatal("unready old runtime survived before a retry")
		}
	}
	if l.Snapshot().State != "FAULTED" || m.creates != 3 {
		t.Fatalf("unbounded startup failures: %+v", l.Snapshot())
	}
}
func TestAcceptedWorkWinsAgainstIdleDrain(t *testing.T) {
	l, m, f := testLifecycle(t)
	ensureTest(t, l)
	f.drainBusy.Store(true)
	l.mu.Lock()
	l.state.LastActive = time.Now().Add(-301 * time.Second)
	l.mu.Unlock()
	l.Tick(context.Background())
	if m.removes != 0 || l.Snapshot().State != "READY" {
		t.Fatal("quiesce rejection killed accepted work")
	}
}
func TestSafetySuspensionSurvivesRestartAndBlocksDueWake(t *testing.T) {
	l, m, _ := testLifecycle(t)
	if e := l.Suspend("business regression"); e != nil {
		t.Fatal(e)
	}
	l2, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	if l2.EnsureReady(context.Background()) == nil {
		t.Fatal("suspended runtime was created")
	}
	if e = l2.DispatchDue(context.Background()); e != nil {
		t.Fatal(e)
	}
	if m.creates != 0 {
		t.Fatal("scheduler bypassed suspension")
	}
	if l2.Retry() == nil {
		t.Fatal("retry must not override safety suspension")
	}
}
func TestGatewayRestartAdoptsSameRuntimeAndIdleAge(t *testing.T) {
	l, m, _ := testLifecycle(t)
	ensureTest(t, l)
	l.mu.Lock()
	old := time.Now().Add(-4 * time.Minute)
	l.state.LastActive = old
	l.state.IdleSince = old
	if e := l.persistLocked(); e != nil {
		t.Fatal(e)
	}
	l.mu.Unlock()
	l2, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	l2.mu.Lock()
	l2.state.State = "ABSENT"
	l2.mu.Unlock()
	ensureTest(t, l2)
	if m.creates != 1 || l2.Snapshot().Attempts != 1 || !l2.Snapshot().LastActive.Equal(old) || !l2.Snapshot().IdleSince.Equal(old) {
		t.Fatalf("adoption reset identity/idle age: %+v", l2.Snapshot())
	}
}

func TestUnsafeDrainRetainsRuntimeAndResumesSameGateAfterRestart(t *testing.T) {
	l, m, f := testLifecycle(t)
	ensureTest(t, l)
	original := l.Snapshot()
	f.drainPending.Store(true)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	e := l.Stop(ctx, false, "idle")
	cancel()
	state := l.Snapshot()
	if e == nil || m.removes != 0 || state.State != "DRAINING" || state.DrainID == "" || state.DrainRevision != 1 || !state.RecoveryRequired || state.Attempts != original.Attempts {
		t.Fatalf("unfinished durable handoff was discarded: %+v removes=%d err=%v", state, m.removes, e)
	}
	if l.EnsureReady(context.Background()) == nil || l.Retry() == nil || m.creates != 1 {
		t.Fatal("one-way drain was reopened by an active wake or retry")
	}
	reloaded, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	if reloaded.Snapshot().DrainID != state.DrainID || reloaded.Snapshot().DrainRevision != state.DrainRevision {
		t.Fatal("drain binding was not persisted")
	}
	f.drainPending.Store(false)
	ctx, cancel = context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() { reloaded.Run(ctx); close(finished) }()
	deadline := time.Now().Add(2 * time.Second)
	for reloaded.Snapshot().State != "ABSENT" && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-finished
	if reloaded.Snapshot().State != "ABSENT" || reloaded.Snapshot().RecoveryRequired || m.removes != 1 || m.creates != 1 {
		t.Fatalf("handoff completion did not allow one verified removal: %+v", reloaded.Snapshot())
	}
	f.drainMu.Lock()
	defer f.drainMu.Unlock()
	if len(f.drains) < 2 {
		t.Fatal("drain was never resumed")
	}
	for _, request := range f.drains {
		if request.DrainID != state.DrainID || request.Revision != state.DrainRevision || request.RuntimeID != original.RuntimeID {
			t.Fatal("drain identity changed across retry or restart")
		}
	}
}

func TestNormalDrainRejectsConflictingOrLiveActionEvidence(t *testing.T) {
	for _, mode := range []string{"conflict", "external"} {
		t.Run(mode, func(t *testing.T) {
			l, m, f := testLifecycle(t)
			ensureTest(t, l)
			f.drainConflict.Store(mode == "conflict")
			f.drainExternal.Store(mode == "external")
			ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
			defer cancel()
			if e := l.Stop(ctx, false, "idle"); e == nil {
				t.Fatal("incomplete/conflicting proof was accepted")
			}
			if m.removes != 0 || l.Snapshot().State != "DRAINING" {
				t.Fatalf("ambiguous gate reopened or removed: %+v", l.Snapshot())
			}
		})
	}
}

func TestDrainProofMustBeDurablyStoredBeforeRemoval(t *testing.T) {
	l, m, _ := testLifecycle(t)
	ensureTest(t, l)
	_, e := l.Store.DB.Exec("CREATE TRIGGER reject_drain_proof BEFORE INSERT ON lifecycle_proofs BEGIN SELECT RAISE(ABORT,'test proof write rejected'); END")
	if e != nil {
		t.Fatal(e)
	}
	if l.Stop(context.Background(), false, "idle") == nil || m.removes != 0 || l.Snapshot().State != "DRAINING" {
		t.Fatal("container removed without durable handoff proof")
	}
}

func TestDrainIntentWriteFailureDoesNotDispatchOrStickBusy(t *testing.T) {
	l, m, f := testLifecycle(t)
	ensureTest(t, l)
	_, e := l.Store.DB.Exec("CREATE TRIGGER reject_drain_intent BEFORE UPDATE ON settings WHEN NEW.key='lifecycle' BEGIN SELECT RAISE(ABORT,'test intent write rejected'); END")
	if e != nil {
		t.Fatal(e)
	}
	if l.Stop(context.Background(), false, "idle") == nil || l.Snapshot().State != "READY" || m.removes != 0 {
		t.Fatal("undurable drain intent changed runtime state")
	}
	l.mu.Lock()
	busy := l.busy
	l.mu.Unlock()
	f.drainMu.Lock()
	dispatched := len(f.drains)
	f.drainMu.Unlock()
	if busy || dispatched != 0 {
		t.Fatal("undurable drain intent dispatched or left lifecycle locked")
	}
}

func TestForcedRemovalRetainsRecoveryMarkerWithoutProof(t *testing.T) {
	l, m, f := testLifecycle(t)
	ensureTest(t, l)
	f.drainPending.Store(true)
	attempts := l.Snapshot().Attempts
	if e := l.Stop(context.Background(), true, "unhealthy_runtime"); e != nil {
		t.Fatal(e)
	}
	state := l.Snapshot()
	if m.removes != 1 || !state.RecoveryRequired || state.Attempts != attempts || state.State != "ABSENT" {
		t.Fatalf("forced recovery did not retain uncertainty: %+v", state)
	}
}

func TestNewRuntimeDoesNotReuseFailedRemovalIdleDeadline(t *testing.T) {
	l, m, _ := testLifecycle(t)
	ensureTest(t, l)
	oldRuntime := l.Snapshot().RuntimeID
	m.crash()
	l.mu.Lock()
	l.state.State = "FAULTED"
	l.state.IdleSince = time.Now().Add(-10 * time.Minute)
	l.state.LastActive = l.state.IdleSince
	l.state.IdleBlockers = []string{"stale_blocker"}
	if e := l.persistLocked(); e != nil {
		t.Fatal(e)
	}
	l.mu.Unlock()
	if e := l.Retry(); e != nil {
		t.Fatal(e)
	}
	started := time.Now()
	ensureTest(t, l)
	state := l.Snapshot()
	if state.RuntimeID == oldRuntime || !state.IdleSince.IsZero() || state.LastActive.Before(started) || len(state.IdleBlockers) != 0 {
		t.Fatalf("new runtime inherited an old idle deadline: %+v", state)
	}
	l.Tick(context.Background())
	if m.removes != 0 || l.Snapshot().State != "READY" {
		t.Fatal("new runtime was immediately reclaimed using old activity")
	}
}

func TestVerifiedDrainRemovalFailureCannotAdoptClosedRuntime(t *testing.T) {
	l, m, _ := testLifecycle(t)
	ensureTest(t, l)
	m.mu.Lock()
	m.removeFailures = 1
	m.mu.Unlock()
	if l.Stop(context.Background(), false, "idle") == nil {
		t.Fatal("test removal failure was not reported")
	}
	state := l.Snapshot()
	if state.State != "DRAINING" || state.Phase != "removal_failed" || state.DrainID == "" || m.removes != 0 {
		t.Fatalf("failed removal lost the one-way gate: %+v", state)
	}
	if l.Retry() == nil || l.EnsureReady(context.Background()) == nil {
		t.Fatal("failed deletion re-adopted a closed runtime")
	}
	// Proof was saved before the failed Docker call. A subsequent attempt must
	// finish exact removal even if the already quiesced API has gone away.
	l.Runtime.URL = "http://127.0.0.1:1"
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if e := l.Stop(ctx, false, "idle"); e != nil {
		t.Fatal("durable handoff proof was not reused", e)
	}
	if m.removes != 1 || l.Snapshot().State != "ABSENT" || l.Snapshot().RecoveryRequired {
		t.Fatal("verified removal did not complete after transient failure")
	}
}

func TestGatewayRestartCompletesPersistedStoppingAfterContainerDisappears(t *testing.T) {
	l, m, _ := testLifecycle(t)
	ensureTest(t, l)
	l.mu.Lock()
	l.state.DrainID = newUUID()
	l.state.DrainRevision = l.state.IntakeRevision
	l.state.DrainReason = "idle"
	l.state.State = "STOPPING"
	proof := DrainProof{Safe: true, Epoch: l.state.Epoch, RuntimeID: l.state.RuntimeID,
		DrainID: l.state.DrainID, IntakeRevision: l.state.DrainRevision + 1}
	encoded, e := json.Marshal(proof)
	if e == nil {
		_, e = l.Store.DB.Exec("INSERT INTO lifecycle_proofs(runtime_id,drain_id,proof,created_at) VALUES(?,?,?,?)", proof.RuntimeID, proof.DrainID, string(encoded), timestamp(time.Now()))
	}
	if e == nil {
		e = l.persistLocked()
	}
	l.mu.Unlock()
	if e != nil {
		t.Fatal(e)
	}
	m.crash() // Docker removed it before the final ABSENT write committed.
	l.Runtime.URL = "http://127.0.0.1:1"
	reloaded, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	if reloaded.EnsureReady(context.Background()) == nil || m.creates != 1 {
		t.Fatal("HTTP startup raced Run and adopted a STOPPING runtime")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	finished := make(chan struct{})
	go func() { reloaded.Run(ctx); close(finished) }()
	deadline := time.Now().Add(500 * time.Millisecond)
	for reloaded.Snapshot().State != "ABSENT" && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-finished
	state := reloaded.Snapshot()
	if state.State != "ABSENT" || state.RecoveryRequired || state.DrainID != "" || state.LastRemoved.IsZero() || m.creates != 1 {
		t.Fatalf("persisted STOPPING required a vanished API or lost proof: %+v", state)
	}
}

type blockedInspectManager struct {
	*fakeManager
	once    atomic.Bool
	entered chan struct{}
	release chan struct{}
}

func (m *blockedInspectManager) Inspect(ctx context.Context) (Instance, error) {
	if m.once.CompareAndSwap(true, false) {
		close(m.entered)
		select {
		case <-m.release:
		case <-ctx.Done():
			return Instance{}, ctx.Err()
		}
	}
	return m.fakeManager.Inspect(ctx)
}

func TestLateTickCannotRemoveReplacementOrResurrectCleanAbsence(t *testing.T) {
	for _, replacement := range []bool{false, true} {
		t.Run(fmt.Sprint("replacement=", replacement), func(t *testing.T) {
			l, base, f := testLifecycle(t)
			ensureTest(t, l)
			manager := &blockedInspectManager{fakeManager: base, entered: make(chan struct{}), release: make(chan struct{})}
			manager.once.Store(true)
			l.Manager = manager
			finished := make(chan struct{})
			go func() { l.Tick(context.Background()); close(finished) }()
			<-manager.entered
			if e := l.Stop(context.Background(), false, "local_management"); e != nil {
				t.Fatal(e)
			}
			var newRuntime string
			if replacement {
				ensureTest(t, l)
				newRuntime = l.Snapshot().RuntimeID
				f.busy.Store(true) // Work accepted by the new generation must survive.
			}
			close(manager.release)
			<-finished
			state := l.Snapshot()
			if base.removes != 1 {
				t.Fatal("old health result removed the replacement runtime")
			}
			if replacement {
				if state.State != "READY" || state.RuntimeID != newRuntime || base.creates != 2 {
					t.Fatalf("old health result corrupted the new generation: %+v", state)
				}
			} else {
				if state.State != "ABSENT" || state.RecoveryRequired || state.Attempts != 0 {
					t.Fatalf("old health result resurrected clean removal: %+v", state)
				}
				l.Tick(context.Background())
				if base.creates != 1 {
					t.Fatal("passive health polling woke a cleanly removed runtime")
				}
			}
		})
	}
}

func TestLateRecoveryTickCannotOverrideCommittedFault(t *testing.T) {
	l, m, _ := testLifecycle(t)
	l.mu.Lock()
	l.state.RecoveryRequired = true
	l.state.Attempts = 1
	if e := l.persistLocked(); e != nil {
		t.Fatal(e)
	}
	l.mu.Unlock()
	// Frozen's query waits for this connection after Tick has taken its ABSENT
	// snapshot. Another transition commits FAULTED while holding the lifecycle
	// lock, before that old recovery observation can acquire the lock again.
	tx, e := l.Store.DB.Begin()
	if e != nil {
		t.Fatal(e)
	}
	waits := l.Store.DB.Stats().WaitCount
	finished := make(chan struct{})
	go func() { l.Tick(context.Background()); close(finished) }()
	deadline := time.Now().Add(time.Second)
	for l.Store.DB.Stats().WaitCount <= waits && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if l.Store.DB.Stats().WaitCount <= waits {
		tx.Rollback()
		t.Fatal("Tick did not reach the blocked query")
	}
	committed := make(chan error, 1)
	go func() {
		l.mu.Lock()
		l.setPhaseLocked("FAULTED", "removal_failed")
		e := l.persistLocked()
		l.mu.Unlock()
		committed <- e
	}()
	for l.Store.DB.Stats().WaitCount <= waits+1 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if e = tx.Rollback(); e != nil {
		t.Fatal(e)
	}
	if e = <-committed; e != nil {
		t.Fatal(e)
	}
	<-finished
	if l.Snapshot().State != "FAULTED" || m.creates != 0 {
		t.Fatal("stale ABSENT recovery observation overrode the committed fault")
	}
}

type blockedEnsureManager struct {
	*fakeManager
	entered      chan struct{}
	release      chan struct{}
	ignoreCancel bool
}

func (m *blockedEnsureManager) Ensure(ctx context.Context, request EnsureRequest) (Instance, error) {
	close(m.entered)
	if m.ignoreCancel {
		<-m.release
	} else {
		select {
		case <-ctx.Done():
			return Instance{}, ctx.Err()
		case <-m.release:
		}
	}
	return m.fakeManager.Ensure(ctx, request)
}

func TestSafetySuspendWaitsForInflightCreateAndCleanup(t *testing.T) {
	for _, ignoreCancel := range []bool{false, true} {
		t.Run(fmt.Sprint("late-create=", ignoreCancel), func(t *testing.T) {
			l, base, _ := testLifecycle(t)
			manager := &blockedEnsureManager{fakeManager: base, entered: make(chan struct{}), release: make(chan struct{}), ignoreCancel: ignoreCancel}
			l.Manager = manager
			started := make(chan error, 1)
			go func() { started <- l.EnsureReady(context.Background()) }()
			<-manager.entered
			suspended := make(chan error, 1)
			go func() { suspended <- l.Suspend("acceptance cancellation") }()
			if ignoreCancel {
				select {
				case <-suspended:
					t.Fatal("suspend returned before the inflight create finished")
				case <-time.After(20 * time.Millisecond):
				}
				close(manager.release)
			}
			if e := <-suspended; e != nil {
				t.Fatal(e)
			}
			if e := <-started; e == nil {
				t.Fatal("suspended startup was published READY")
			}
			instance, _ := base.Inspect(context.Background())
			if instance.Exists || !l.Snapshot().SafetySuspended || l.EnsureReady(context.Background()) == nil || l.Retry() == nil {
				t.Fatal("successful suspension allowed a runtime or wake")
			}
			if ignoreCancel && (base.creates != 1 || base.removes != 1) {
				t.Fatal("late successful create was not removed before suspension returned")
			}
			if !ignoreCancel && base.creates != 0 {
				t.Fatal("cancelled startup still created a container")
			}
		})
	}
}

func TestFailedResumeOrRetryCannotChangeCommittedSafetyState(t *testing.T) {
	for _, operation := range []string{"resume", "retry"} {
		for _, failAt := range []string{"settings", "audit"} {
			t.Run(operation+"/"+failAt, func(t *testing.T) {
				l, m, _ := testLifecycle(t)
				kind := "runtime.retry"
				if operation == "resume" {
					if e := l.Suspend("test durable suspension"); e != nil {
						t.Fatal(e)
					}
					kind = "safety.resume"
				} else {
					l.mu.Lock()
					l.state.State, l.state.Attempts, l.state.RecoveryRound = "FAULTED", 3, randomID()
					e := l.persistLocked()
					l.mu.Unlock()
					if e != nil {
						t.Fatal(e)
					}
				}
				previous := l.Snapshot()
				trigger := "CREATE TRIGGER reject_admin BEFORE UPDATE ON settings WHEN NEW.key='lifecycle' BEGIN SELECT RAISE(ABORT,'test state write failure'); END"
				if failAt == "audit" {
					trigger = "CREATE TRIGGER reject_admin BEFORE INSERT ON audit WHEN NEW.kind='" + kind + "' BEGIN SELECT RAISE(ABORT,'test audit write failure'); END"
				}
				if _, e := l.Store.DB.Exec(trigger); e != nil {
					t.Fatal(e)
				}
				var e error
				if operation == "resume" {
					e = l.Resume()
				} else {
					e = l.Retry()
				}
				if e == nil {
					t.Fatal("injected persistence failure was ignored")
				}
				current := l.Snapshot()
				if current.SafetySuspended != previous.SafetySuspended || current.State != previous.State || current.Attempts != previous.Attempts || current.RecoveryRound != previous.RecoveryRound {
					t.Fatal("failed admin command changed the in-memory safety/recovery state")
				}
				var encoded string
				if e = l.Store.DB.QueryRow("SELECT value FROM settings WHERE key='lifecycle'").Scan(&encoded); e != nil {
					t.Fatal(e)
				}
				var persisted RuntimeState
				if e = json.Unmarshal([]byte(encoded), &persisted); e != nil {
					t.Fatal(e)
				}
				if persisted.SafetySuspended != previous.SafetySuspended || persisted.State != previous.State || persisted.Attempts != previous.Attempts || persisted.RecoveryRound != previous.RecoveryRound {
					t.Fatal("state and audit did not roll back together")
				}
				if l.EnsureReady(context.Background()) == nil || m.creates != 0 {
					t.Fatal("failed admin command permitted a new startup")
				}
			})
		}
	}
}

func TestRunCannotReapplyStartupReconciliationAfterNewWake(t *testing.T) {
	l, m, f := testLifecycle(t)
	ensureTest(t, l)
	reloaded, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	if reloaded.Snapshot().State != "ABSENT" || !reloaded.Snapshot().RecoveryRequired {
		t.Fatal("persisted phase was not reconciled before serving")
	}
	ensureTest(t, reloaded) // Public wake wins scheduling before Run begins.
	current := reloaded.Snapshot()
	f.busy.Store(true)
	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	go func() { reloaded.Run(ctx); close(finished) }()
	deadline := time.Now().Add(time.Second)
	for f.activityRequests.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	cancel()
	<-finished
	state := reloaded.Snapshot()
	if f.activityRequests.Load() == 0 || state.State != "READY" || state.RuntimeID != current.RuntimeID || !state.ReadyAt.Equal(current.ReadyAt) || m.creates != 1 || m.removes != 0 {
		t.Fatal("Run applied an old startup phase over a newly ready generation")
	}
}
