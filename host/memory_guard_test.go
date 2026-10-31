package host

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func memoryFixture() map[string]string {
	return map[string]string{
		"/proc/meminfo":     "MemTotal: 4000000 kB\nMemAvailable: 3072000 kB\n",
		"/proc/self/cgroup": "0::/personal.slice/personal-bot.slice/personal-bot-gateway.service\n",
		"/sys/fs/cgroup/personal.slice/personal-bot.slice/memory.stat":   "anon 104857600\nshmem 20971520\nfile 9000000000\n",
		"/sys/fs/cgroup/personal.slice/personal-bot.slice/memory.events": "low 0\nhigh 0\nmax 2\noom 7\noom_kill 2\n",
	}
}

func fixtureReader(files map[string]string) memoryFSReader {
	return memoryFSReader{slice: "personal-bot.slice", readFile: func(name string) ([]byte, error) {
		value, ok := files[name]
		if !ok {
			return nil, errors.New("fixture metric missing")
		}
		return []byte(value), nil
	}}
}

func TestHostMemoryReaderUsesNestedAncestorAndNonreclaimableCredit(t *testing.T) {
	reader := fixtureReader(memoryFixture())
	snapshot, e := reader.Read()
	if e != nil {
		t.Fatal(e)
	}
	if snapshot.AvailableBytes != 3000<<20 || snapshot.ProjectResidentBytes != 120<<20 || snapshot.OOM != 7 || snapshot.OOMKills != 2 {
		t.Fatalf("nested hierarchy metrics or anon/shmem credit incorrect: %+v", snapshot)
	}
	// Huge file-cache metrics must not make this insufficient host admissible.
	l, m, _ := testLifecycle(t)
	enableMemoryGuard(l, reader)
	if l.EnsureReady(context.Background()) == nil || m.creates != 0 || !l.Snapshot().SafetySuspended {
		t.Fatal("file cache was credited twice or failed admission still created a runtime")
	}
}

func TestHostMemoryReaderRejectsMissingOrEscapingMetrics(t *testing.T) {
	for _, mode := range []string{"missing_available", "missing_shmem", "bad_value", "missing_oom", "missing_ancestor", "traversal", "unified_missing", "slice_escape"} {
		t.Run(mode, func(t *testing.T) {
			files := memoryFixture()
			switch mode {
			case "missing_available":
				files["/proc/meminfo"] = "MemFree: 9000000 kB\n"
			case "missing_shmem":
				files["/sys/fs/cgroup/personal.slice/personal-bot.slice/memory.stat"] = "anon 123\n"
			case "bad_value":
				files["/sys/fs/cgroup/personal.slice/personal-bot.slice/memory.stat"] = "anon -1\nshmem 0\n"
			case "missing_oom":
				files["/sys/fs/cgroup/personal.slice/personal-bot.slice/memory.events"] = "oom_kill 1\n"
			case "missing_ancestor":
				files["/proc/self/cgroup"] = "0::/system.slice/unrelated.service\n"
			case "traversal":
				files["/proc/self/cgroup"] = "0::/personal.slice/../personal-bot.slice/gateway.service\n"
			case "unified_missing":
				files["/proc/self/cgroup"] = "4:memory:/personal-bot.slice\n"
			}
			reader := fixtureReader(files)
			if mode == "slice_escape" {
				reader.slice = "../personal-bot.slice"
			}
			if _, e := reader.Read(); e == nil {
				t.Fatal("untrusted or incomplete metrics were accepted")
			}
		})
	}
}

func TestMemoryGuardProductionDefaultsAndExplicitDisabledConfig(t *testing.T) {
	name := filepath.Join(t.TempDir(), "host.json")
	if e := os.WriteFile(name, []byte(`{"origin":"https://localhost:18443","state_dir":"/tmp/test-control","static_dir":"/tmp/test-web"}`), 0600); e != nil {
		t.Fatal(e)
	}
	c, e := LoadConfig(name)
	if e != nil {
		t.Fatal(e)
	}
	if c.HostReserveBytes != 768<<20 || c.ProjectPeakBytes != 2432<<20 || c.ProjectSlice != "personal-bot.slice" || !memoryGuardEnabled(c) {
		t.Fatal("production defaults do not enable memory guard")
	}
	if memoryGuardEnabled(Config{}) || validateMemoryGuard(Config{}) != nil {
		t.Fatal("zero-value test configuration enabled production metric reads")
	}
	for _, slice := range []string{"../personal-bot.slice", "/personal-bot.slice", "a/b.slice", "x.service"} {
		c.ProjectSlice = slice
		if validateMemoryGuard(c) == nil {
			t.Fatal("nonfixed slice name accepted")
		}
	}
}

type fakeHostMemory struct {
	mu       sync.Mutex
	snapshot hostMemorySnapshot
	err      error
	calls    int
}

func (r *fakeHostMemory) Read() (hostMemorySnapshot, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	return r.snapshot, r.err
}

func (r *fakeHostMemory) update(snapshot hostMemorySnapshot, e error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.snapshot, r.err = snapshot, e
}

func enoughMemory() hostMemorySnapshot {
	return hostMemorySnapshot{AvailableBytes: 3300 << 20, ProjectResidentBytes: 100 << 20, OOM: 7, OOMKills: 2}
}

func enableMemoryGuard(l *Lifecycle, reader hostMemoryReader) {
	l.Config.HostReserveBytes, l.Config.ProjectPeakBytes, l.Config.ProjectSlice = 768<<20, 2432<<20, "personal-bot.slice"
	l.memoryReader = reader
}

func assertDurableMemorySuspension(t *testing.T, l *Lifecycle) {
	t.Helper()
	state := l.Snapshot()
	if !state.SafetySuspended || state.SuspensionReason == "" {
		t.Fatal("memory protection did not suspend with an explanation")
	}
	var encoded string
	if e := l.Store.DB.QueryRow("SELECT value FROM settings WHERE key='lifecycle'").Scan(&encoded); e != nil {
		t.Fatal(e)
	}
	var persisted RuntimeState
	if e := json.Unmarshal([]byte(encoded), &persisted); e != nil {
		t.Fatal(e)
	}
	if !persisted.SafetySuspended || persisted.SuspensionReason != state.SuspensionReason {
		t.Fatal("resource suspension was not durable")
	}
	if l.EnsureReady(context.Background()) == nil || l.Retry() == nil {
		t.Fatal("implicit recovery bypassed resource suspension")
	}
}

func TestMemoryAdmissionFailureIsDurableAndRequiresExplicitResume(t *testing.T) {
	for _, unreadable := range []bool{false, true} {
		t.Run(map[bool]string{false: "insufficient", true: "unreadable"}[unreadable], func(t *testing.T) {
			l, m, _ := testLifecycle(t)
			initial := enoughMemory()
			initial.AvailableBytes = 3000 << 20
			reader := &fakeHostMemory{snapshot: initial}
			if unreadable {
				reader.err = errors.New("test metric unavailable")
			}
			enableMemoryGuard(l, reader)
			if l.EnsureReady(context.Background()) == nil || m.creates != 0 || l.Snapshot().Attempts != 0 {
				t.Fatal("failed admission created a runtime or consumed recovery attempts")
			}
			assertDurableMemorySuspension(t, l)
			reader.update(enoughMemory(), nil)
			l.Tick(context.Background())
			if m.creates != 0 {
				t.Fatal("new capacity implicitly cleared resource suspension")
			}
			if e := l.Resume(); e != nil {
				t.Fatal(e)
			}
			ensureTest(t, l)
			if m.creates != 1 || l.Snapshot().SafetySuspended {
				t.Fatal("explicit resume could not admit a now-safe runtime")
			}
		})
	}
}

func TestMemoryPressureNeedsTwoReadingsAndTenSecondsThenStops(t *testing.T) {
	l, m, f := testLifecycle(t)
	reader := &fakeHostMemory{snapshot: enoughMemory()}
	enableMemoryGuard(l, reader)
	ensureTest(t, l)
	f.busy.Store(true)
	clock := time.Now()
	l.memoryClock = func() time.Time { return clock }
	low := enoughMemory()
	low.AvailableBytes = (768 << 20) - 1
	reader.update(low, nil)
	l.Tick(context.Background())
	if m.removes != 0 || l.Snapshot().SafetySuspended {
		t.Fatal("single low sample stopped the runtime")
	}
	clock = clock.Add(5 * time.Second)
	reader.update(enoughMemory(), nil)
	l.Tick(context.Background()) // A transient pressure episode must reset.
	reader.update(low, nil)
	clock = clock.Add(10 * time.Second)
	l.Tick(context.Background())
	if m.removes != 0 {
		t.Fatal("cleared pressure was counted as continuous")
	}
	clock = clock.Add(9 * time.Second)
	l.Tick(context.Background())
	if m.removes != 0 {
		t.Fatal("pressure shorter than ten seconds stopped the runtime")
	}
	clock = clock.Add(time.Second)
	l.Tick(context.Background())
	if m.removes != 1 || l.Snapshot().State != "ABSENT" || !l.Snapshot().RecoveryRequired {
		t.Fatal("sustained memory pressure did not stop exactly the runtime with a recovery marker")
	}
	assertDurableMemorySuspension(t, l)
	l.Tick(context.Background())
	if m.creates != 1 {
		t.Fatal("memory pressure caused an automatic wake loop")
	}
}

func TestHistoricalOOMIsBaselineButNewOOMOrReadFailureStops(t *testing.T) {
	for _, mode := range []string{"oom", "oom_kill", "reader_error"} {
		t.Run(mode, func(t *testing.T) {
			l, m, f := testLifecycle(t)
			reader := &fakeHostMemory{snapshot: enoughMemory()}
			enableMemoryGuard(l, reader)
			ensureTest(t, l) // Baseline already contains historical OOM counts.
			f.busy.Store(true)
			l.Tick(context.Background())
			if l.Snapshot().SafetySuspended || m.removes != 0 {
				t.Fatal("historical OOM was treated as new")
			}
			snapshot := enoughMemory()
			var readError error
			if mode == "oom" {
				snapshot.OOM++
			}
			if mode == "oom_kill" {
				snapshot.OOMKills++
			}
			if mode == "reader_error" {
				readError = errors.New("test metric read failure")
			}
			reader.update(snapshot, readError)
			l.Tick(context.Background())
			if m.removes != 1 || l.Snapshot().State != "ABSENT" {
				t.Fatal("new OOM or missing protection metrics did not stop the exact runtime")
			}
			assertDurableMemorySuspension(t, l)
			if mode == "reader_error" && !strings.Contains(l.Snapshot().SuspensionReason, "无法读取") {
				t.Fatal("metric failure has no readable reason")
			}
		})
	}
}

func TestDisabledMemoryGuardNeverReadsLinuxMetrics(t *testing.T) {
	l, m, _ := testLifecycle(t)
	reader := &fakeHostMemory{err: errors.New("disabled reader must not be invoked")}
	l.memoryReader = reader
	ensureTest(t, l)
	l.Tick(context.Background())
	reader.mu.Lock()
	calls := reader.calls
	reader.mu.Unlock()
	if calls != 0 || m.creates != 1 || l.Snapshot().SafetySuspended {
		t.Fatal("disabled guard affected ordinary cross-platform lifecycle tests")
	}
}

func TestMemoryTripStopsDuringStorageFailureAndPersistsAfterRecovery(t *testing.T) {
	for _, failAt := range []string{"settings", "audit"} {
		t.Run(failAt, func(t *testing.T) {
			l, m, f := testLifecycle(t)
			reader := &fakeHostMemory{snapshot: enoughMemory()}
			enableMemoryGuard(l, reader)
			ensureTest(t, l)
			f.busy.Store(true)
			trigger := "CREATE TRIGGER reject_memory BEFORE UPDATE ON settings WHEN NEW.key='lifecycle' BEGIN SELECT RAISE(ABORT,'test control state failure'); END"
			if failAt == "audit" {
				trigger = "CREATE TRIGGER reject_memory BEFORE INSERT ON audit WHEN NEW.kind='safety.memory_guard' BEGIN SELECT RAISE(ABORT,'test audit failure'); END"
			}
			if _, e := l.Store.DB.Exec(trigger); e != nil {
				t.Fatal(e)
			}
			oom := enoughMemory()
			oom.OOM++
			reader.update(oom, nil)
			l.Tick(context.Background())
			instance, _ := m.Inspect(context.Background())
			if instance.Exists || m.removes != 1 || !l.Snapshot().SafetySuspended || l.EnsureReady(context.Background()) == nil || l.Retry() == nil {
				t.Fatal("failed safety persistence left the pressure source running or allowed a wake")
			}
			if failAt == "audit" {
				assertDurableMemorySuspension(t, l)
				// The fallback pause and audit retry marker must survive restart.
				var e error
				l, e = NewLifecycle(l.Store, m, l.Runtime, l.Config)
				if e != nil {
					t.Fatal(e)
				}
			}
			if _, e := l.Store.DB.Exec("DROP TRIGGER reject_memory"); e != nil {
				t.Fatal(e)
			}
			// The OOM counter is unchanged: recovery must retry the pending
			// safety decision, rather than needing another OOM to trigger it.
			l.Tick(context.Background())
			assertDurableMemorySuspension(t, l)
			var events int
			if e := l.Store.DB.QueryRow("SELECT COUNT(*) FROM audit WHERE kind='safety.memory_guard'").Scan(&events); e != nil {
				t.Fatal(e)
			}
			if events != 1 || m.creates != 1 || m.removes != 1 {
				t.Fatal("storage recovery lost/duplicated the safety event or recreated the runtime")
			}
		})
	}
}

func TestMemorySuspensionRetriesExactRemovalAfterTransientHelperFailure(t *testing.T) {
	l, m, f := testLifecycle(t)
	reader := &fakeHostMemory{snapshot: enoughMemory()}
	enableMemoryGuard(l, reader)
	ensureTest(t, l)
	f.busy.Store(true)
	m.removeFailures = 1
	oom := enoughMemory()
	oom.OOMKills++
	reader.update(oom, nil)
	l.Tick(context.Background())
	instance, _ := m.Inspect(context.Background())
	if !instance.Exists || l.Snapshot().State != "FAULTED" {
		t.Fatal("test did not retain the paused runtime after its transient cleanup failure")
	}
	assertDurableMemorySuspension(t, l)
	// Simulate gateway restart while the exact cleanup remains unfinished.
	reloaded, e := NewLifecycle(l.Store, m, l.Runtime, l.Config)
	if e != nil {
		t.Fatal(e)
	}
	reloaded.Tick(context.Background())
	instance, _ = m.Inspect(context.Background())
	if instance.Exists || m.removes != 1 || m.creates != 1 || reloaded.Snapshot().State != "ABSENT" {
		t.Fatal("paused Tick failed to retry only the existing runtime cleanup")
	}
	assertDurableMemorySuspension(t, reloaded)
	for i := 0; i < 3; i++ {
		reloaded.Tick(context.Background())
	}
	if m.creates != 1 || m.removes != 1 {
		t.Fatal("paused cleanup turned into a create/delete loop")
	}
}

func TestMemorySuspensionRefusesReplacementContainerIdentity(t *testing.T) {
	l, m, f := testLifecycle(t)
	reader := &fakeHostMemory{snapshot: enoughMemory()}
	enableMemoryGuard(l, reader)
	ensureTest(t, l)
	f.busy.Store(true)
	m.removeFailures = 1
	oom := enoughMemory()
	oom.OOMKills++
	reader.update(oom, nil)
	l.Tick(context.Background())
	m.mu.Lock()
	m.instance.ContainerID = strings.Repeat("b", 64)
	replacement := m.instance
	m.mu.Unlock()
	l.Tick(context.Background())
	instance, _ := m.Inspect(context.Background())
	if instance != replacement || m.removes != 0 || m.creates != 1 {
		t.Fatal("suspended cleanup deleted a container outside the recorded exact binding")
	}
	assertDurableMemorySuspension(t, l)
}

func TestFreshRuntimeDoesNotInheritPreviousLowMemoryEpisode(t *testing.T) {
	l, m, f := testLifecycle(t)
	reader := &fakeHostMemory{snapshot: enoughMemory()}
	enableMemoryGuard(l, reader)
	ensureTest(t, l)
	f.busy.Store(true)
	clock := time.Now()
	l.memoryClock = func() time.Time { return clock }
	low := enoughMemory()
	low.AvailableBytes = (768 << 20) - 1
	reader.update(low, nil)
	l.Tick(context.Background())
	if e := l.Stop(context.Background(), false, "idle"); e != nil {
		t.Fatal(e)
	}
	reader.update(enoughMemory(), nil)
	ensureTest(t, l)
	clock = clock.Add(30 * time.Second)
	reader.update(low, nil)
	l.Tick(context.Background())
	if l.Snapshot().SafetySuspended || m.removes != 1 || m.creates != 2 {
		t.Fatal("fresh runtime inherited the earlier generation's low-memory samples")
	}
	clock = clock.Add(10 * time.Second)
	l.Tick(context.Background())
	if !l.Snapshot().SafetySuspended || m.removes != 2 {
		t.Fatal("new generation's own sustained pressure did not stop it")
	}
}

type blockedHostMemory struct {
	base    *fakeHostMemory
	entered chan struct{}
	release chan struct{}
	mu      sync.Mutex
	block   bool
}

func (r *blockedHostMemory) Read() (hostMemorySnapshot, error) {
	snapshot, e := r.base.Read()
	r.mu.Lock()
	block := r.block
	r.block = false
	r.mu.Unlock()
	if block {
		close(r.entered)
		<-r.release
	}
	return snapshot, e
}

func TestOldMemoryObservationCannotConsumeNewOOMOrPauseAReplacement(t *testing.T) {
	l, m, f := testLifecycle(t)
	base := &fakeHostMemory{snapshot: enoughMemory()}
	reader := &blockedHostMemory{base: base, entered: make(chan struct{}), release: make(chan struct{})}
	enableMemoryGuard(l, reader)
	ensureTest(t, l)
	f.busy.Store(true)
	oom := enoughMemory()
	oom.OOM++
	base.update(oom, nil)
	reader.mu.Lock()
	reader.block = true
	reader.mu.Unlock()
	finished := make(chan struct{})
	go func() { l.Tick(context.Background()); close(finished) }()
	<-reader.entered
	if e := l.Stop(context.Background(), false, "idle"); e != nil {
		t.Fatal(e)
	}
	// B's separate admission does not see the delayed A observation yet.
	base.update(enoughMemory(), nil)
	ensureTest(t, l)
	close(reader.release)
	<-finished
	if l.Snapshot().SafetySuspended || m.creates != 2 || m.removes != 1 {
		t.Fatal("delayed A observation paused or removed healthy replacement B")
	}
	// The stale read must not advance the global OOM baseline: B now sees
	// that same counter increase and must still trip its own protection.
	base.update(oom, nil)
	l.Tick(context.Background())
	if !l.Snapshot().SafetySuspended || m.removes != 2 {
		t.Fatal("stale observation swallowed the project OOM before B could act")
	}
	assertDurableMemorySuspension(t, l)
}
