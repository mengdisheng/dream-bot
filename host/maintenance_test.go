package host

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"filippo.io/age"
)

type maintenanceRuntime struct {
	base               *fakeRuntime
	mu                 sync.Mutex
	marker             string
	drainReady         bool
	freezeError        bool
	freezeFalse        bool
	releaseFailures    int
	releaseFalse       bool
	stages             []string
	releaseGenerations []string
	releaseIDs         []string
}

func (f *maintenanceRuntime) serve(w http.ResponseWriter, req *http.Request) {
	if req.URL.Path != "/internal/maintenance/stage" {
		f.base.serve(w, req)
		return
	}
	var body struct {
		ID    string `json:"maintenance_id"`
		Stage string `json:"stage"`
	}
	if !decodeJSON(w, req, &body) {
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.stages = append(f.stages, body.Stage)
	switch body.Stage {
	case "drain":
		f.marker = body.ID + ":drain"
		writeJSON(w, 200, map[string]any{"drained": f.drainReady, "maintenance_id": body.ID})
	case "freeze":
		if f.freezeError {
			writeError(w, 409, "outbox_pending", "outbox has not drained", true)
			return
		}
		f.marker = body.ID + ":freeze"
		writeJSON(w, 200, map[string]any{"frozen": !f.freezeFalse, "snapshot_path": "/data/app/maintenance/" + body.ID + "/app.sqlite"})
	case "release":
		segments := strings.Split(req.Header.Get("X-Personal-Bot-Assertion"), ".")
		if len(segments) != 2 {
			writeError(w, 403, "assertion_missing", "missing assertion", false)
			return
		}
		payload, err := base64.RawURLEncoding.DecodeString(segments[0])
		var assertion struct {
			RuntimeID string `json:"runtime_id"`
			Epoch     string `json:"deployment_epoch"`
		}
		if err != nil || json.Unmarshal(payload, &assertion) != nil {
			writeError(w, 403, "assertion_invalid", "invalid assertion", false)
			return
		}
		instance, _ := f.base.manager.Inspect(req.Context())
		if assertion.RuntimeID != instance.RuntimeID || assertion.Epoch != instance.Epoch {
			writeError(w, 409, "stale_runtime", "wrong runtime", false)
			return
		}
		f.releaseGenerations = append(f.releaseGenerations, assertion.RuntimeID)
		f.releaseIDs = append(f.releaseIDs, body.ID)
		if f.releaseFailures > 0 {
			f.releaseFailures--
			writeError(w, 503, "release_unavailable", "transient release failure", true)
			return
		}
		if f.releaseFalse {
			writeJSON(w, 200, map[string]bool{"released": false})
			return
		}
		if f.marker != "" && !strings.HasPrefix(f.marker, body.ID+":") {
			writeError(w, 409, "wrong_maintenance", "wrong maintenance", false)
			return
		}
		f.marker = ""
		writeJSON(w, 200, map[string]bool{"released": true})
	}
}

func testMaintenanceGateway(t *testing.T) (*Gateway, *maintenanceRuntime, *fakeManager, *atomic.Int32) {
	t.Helper()
	l, m, base := testLifecycle(t)
	runtime := &maintenanceRuntime{base: base, drainReady: true}
	server := httptest.NewServer(http.HandlerFunc(runtime.serve))
	t.Cleanup(server.Close)
	l.Runtime.URL, l.Config.RuntimeURL = server.URL, server.URL
	key, err := age.GenerateX25519Identity()
	if err != nil {
		t.Fatal(err)
	}
	l.Config.BackupRecipient = key.Recipient().String()
	g, err := NewGateway(l.Config, l.Store, l)
	if err != nil {
		t.Fatal(err)
	}
	calls := &atomic.Int32{}
	g.maintenanceHelper.HTTP = &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
		calls.Add(1)
		body := `{"snapshot":true}`
		if req.URL.Path == "/v1/backup" {
			instance, _ := m.Inspect(req.Context())
			if instance.Exists {
				return nil, errors.New("archive attempted while runtime exists")
			}
			body = `{"id":"fixture.age","verified":true,"size":123}`
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})}
	ensureTest(t, l)
	return g, runtime, m, calls
}

func latestMaintenance(t *testing.T, s *Store) (string, string, int) {
	t.Helper()
	var id, stage string
	var complete int
	if err := s.DB.QueryRow("SELECT id,stage,cleanup_complete FROM maintenance ORDER BY created_at DESC,rowid DESC LIMIT 1").Scan(&id, &stage, &complete); err != nil {
		t.Fatal(err)
	}
	return id, stage, complete
}

func TestBackupFailedReleaseRemainsDurableAndRecoversAfterRestart(t *testing.T) {
	g, runtime, _, helpers := testMaintenanceGateway(t)
	runtime.freezeError, runtime.releaseFailures = true, 1
	if _, err := g.Backup(context.Background()); err == nil {
		t.Fatal("freeze failure was ignored")
	}
	id, stage, complete := latestMaintenance(t, g.Store)
	if stage != "releasing" || complete != 0 || !g.Store.Frozen() {
		t.Fatal("failed release opened durable admission")
	}
	if err := g.Lifecycle.EnsureReady(context.Background()); err == nil {
		t.Fatal("writes can enter while release is unconfirmed")
	}
	if helpers.Load() != 0 {
		t.Fatal("undrained backup reached archive helper")
	}
	// Recreate the lifecycle and gateway, retaining the real control database
	// and an unchanged runtime marker, as after a host gateway restart.
	l, err := NewLifecycle(g.Store, g.Lifecycle.Manager, NewRuntimeClient(g.Config.RuntimeURL, g.Store.Secrets), g.Config)
	if err != nil {
		t.Fatal(err)
	}
	restarted, err := NewGateway(g.Config, g.Store, l)
	if err != nil {
		t.Fatal(err)
	}
	if err = restarted.RecoverMaintenance(context.Background()); err != nil {
		t.Fatal(err)
	}
	id2, stage, complete := latestMaintenance(t, g.Store)
	if id2 != id || stage != "failed" || complete != 1 || g.Store.Frozen() {
		t.Fatal("restart did not finish the original release")
	}
	runtime.mu.Lock()
	marker, generations := runtime.marker, append([]string{}, runtime.releaseGenerations...)
	runtime.mu.Unlock()
	if marker != "" || len(generations) != 2 || generations[0] != generations[1] {
		t.Fatal("release did not clear the original bound generation")
	}
	ensureTest(t, l)
}

func TestBackupRequiresExplicitDrainFreezeAndReleaseAcknowledgments(t *testing.T) {
	for _, name := range []string{"drain_false", "freeze_false", "release_false"} {
		t.Run(name, func(t *testing.T) {
			g, runtime, _, helpers := testMaintenanceGateway(t)
			switch name {
			case "drain_false":
				runtime.drainReady = false
			case "freeze_false":
				runtime.freezeFalse = true
			case "release_false":
				runtime.freezeError, runtime.releaseFalse = true, true
			}
			if _, err := g.Backup(context.Background()); err == nil {
				t.Fatal("invalid stage acknowledgment was accepted")
			}
			_, stage, complete := latestMaintenance(t, g.Store)
			if helpers.Load() != 0 {
				t.Fatal("invalid stage reached archive helper")
			}
			runtime.mu.Lock()
			stages := append([]string{}, runtime.stages...)
			runtime.mu.Unlock()
			if name == "drain_false" && strings.Contains(strings.Join(stages, ","), "freeze") {
				t.Fatal("drained=false was followed by freeze")
			}
			if name == "release_false" {
				if stage != "releasing" || complete != 0 || !g.Store.Frozen() {
					t.Fatal("released=false opened maintenance gate")
				}
			} else if stage != "failed" || complete != 1 || g.Store.Frozen() {
				t.Fatal("acknowledged failure cleanup remained stuck")
			}
		})
	}
}

func TestBackupRecoveryRepairsLegacyFailedMarkerWithoutWaking(t *testing.T) {
	for _, exists := range []bool{true, false} {
		t.Run(map[bool]string{true: "original_exists", false: "original_absent"}[exists], func(t *testing.T) {
			g, runtime, m, _ := testMaintenanceGateway(t)
			id := newUUID()
			if _, err := g.Store.DB.Exec("INSERT INTO maintenance(id,stage,created_at) VALUES(?,'failed',?)", id, timestamp(time.Now())); err != nil {
				t.Fatal(err)
			}
			runtime.marker = id + ":drain"
			if !exists {
				m.crash()
			}
			m.mu.Lock()
			before := m.creates
			m.mu.Unlock()
			if err := g.RecoverMaintenance(context.Background()); err != nil {
				t.Fatal(err)
			}
			_, stage, complete := latestMaintenance(t, g.Store)
			if stage != "failed" || complete != 1 || g.Store.Frozen() {
				t.Fatal("legacy cleanup was not confirmed")
			}
			m.mu.Lock()
			after := m.creates
			m.mu.Unlock()
			if before != after {
				t.Fatal("recovery created a runtime")
			}
			runtime.mu.Lock()
			released := len(runtime.releaseGenerations)
			marker := runtime.marker
			runtime.mu.Unlock()
			if exists && (released != 1 || marker != "") || !exists && released != 0 {
				t.Fatal("wrong legacy release behavior")
			}
		})
	}
}

func TestBackupRecoveryDoesNotReleaseSuccessorGeneration(t *testing.T) {
	for _, changeEpoch := range []bool{false, true} {
		t.Run(map[bool]string{false: "new_runtime", true: "new_epoch"}[changeEpoch], func(t *testing.T) {
			g, runtime, m, _ := testMaintenanceGateway(t)
			r, err := g.claimBackup(context.Background(), nil)
			if err != nil {
				t.Fatal(err)
			}
			runtime.marker = "successor-maintenance:drain"
			m.mu.Lock()
			if changeEpoch {
				m.instance.Epoch = "successor-epoch"
			} else {
				m.instance.RuntimeID = newUUID()
				m.instance.ContainerID = strings.Repeat("d", 64)
			}
			m.mu.Unlock()
			if err = g.RecoverMaintenance(context.Background()); err != nil {
				t.Fatal(err)
			}
			id, stage, complete := latestMaintenance(t, g.Store)
			if id != r.ID || stage != "failed" || complete != 1 {
				t.Fatal("ended generation did not finish cleanup")
			}
			runtime.mu.Lock()
			defer runtime.mu.Unlock()
			if len(runtime.releaseGenerations) != 0 || runtime.marker != "successor-maintenance:drain" {
				t.Fatal("stale maintenance released its successor")
			}
		})
	}
}

func TestBackupRecoveryFindsOlderKnownLegacyMarker(t *testing.T) {
	for _, known := range []bool{true, false} {
		t.Run(map[bool]string{true: "known_older_uuid", false: "unknown_uuid_retained"}[known], func(t *testing.T) {
			g, runtime, _, _ := testMaintenanceGateway(t)
			older, latest := newUUID(), newUUID()
			if _, err := g.Store.DB.Exec("INSERT INTO maintenance(id,stage,created_at) VALUES(?,'failed',?)", older, timestamp(time.Now().Add(-time.Minute))); err != nil {
				t.Fatal(err)
			}
			if _, err := g.Store.DB.Exec("INSERT INTO maintenance(id,stage,created_at) VALUES(?,'failed',?)", latest, timestamp(time.Now())); err != nil {
				t.Fatal(err)
			}
			markerID := older
			if !known {
				markerID = newUUID()
			}
			runtime.marker = markerID + ":drain"
			err := g.RecoverMaintenance(context.Background())
			runtime.mu.Lock()
			marker := runtime.marker
			ids := append([]string{}, runtime.releaseIDs...)
			runtime.mu.Unlock()
			if known {
				if err != nil {
					t.Fatal(err)
				}
				if marker != "" || g.Store.Frozen() || strings.Join(ids, ",") != strings.Join([]string{latest, older, latest}, ",") {
					t.Fatal("older known marker did not recover through UUID-checked release", ids)
				}
				var pending int
				if err = g.Store.DB.QueryRow("SELECT count(*) FROM maintenance WHERE cleanup_complete=0").Scan(&pending); err != nil || pending != 0 {
					t.Fatal("legacy records were not both confirmed", err, pending)
				}
			} else if err == nil || marker != markerID+":drain" || !g.Store.Frozen() {
				t.Fatal("unknown marker was cleared or falsely acknowledged", err)
			}
		})
	}
}

func TestMaintenanceMigrationKeepsReleasingExclusive(t *testing.T) {
	s := testStore(t)
	for _, query := range []string{
		"DROP TABLE maintenance",
		"CREATE TABLE maintenance(id TEXT PRIMARY KEY,stage TEXT NOT NULL,created_at TEXT NOT NULL,error TEXT)",
		"CREATE UNIQUE INDEX maintenance_one_active ON maintenance((1)) WHERE stage IN ('drain','freeze')",
	} {
		if _, err := s.DB.Exec(query); err != nil {
			t.Fatal(err)
		}
	}
	if err := migrateMaintenance(s.DB); err != nil {
		t.Fatal(err)
	}
	if _, err := s.DB.Exec("INSERT INTO maintenance(id,stage,created_at) VALUES(?,'releasing',?)", newUUID(), timestamp(time.Now())); err != nil {
		t.Fatal(err)
	}
	if !s.Frozen() {
		t.Fatal("releasing record did not survive as a durable gate")
	}
	if _, err := s.DB.Exec("INSERT INTO maintenance(id,stage,created_at) VALUES(?,'drain',?)", newUUID(), timestamp(time.Now())); err == nil {
		t.Fatal("migration allowed a concurrent backup during release")
	}
	if err := migrateMaintenance(s.DB); err != nil {
		t.Fatal("schema migration is not restart-idempotent", err)
	}
}

func TestMaintenanceDrainAndReleaseKeepOutboxWritable(t *testing.T) {
	s := testStore(t)
	id := newUUID()
	if _, err := s.DB.Exec("INSERT INTO maintenance(id,stage,created_at) VALUES(?,'drain',?)", id, timestamp(time.Now())); err != nil {
		t.Fatal(err)
	}
	for _, stage := range []string{"drain", "releasing", "freeze"} {
		if _, err := s.DB.Exec("UPDATE maintenance SET stage=? WHERE id=?", stage, id); err != nil {
			t.Fatal(err)
		}
		event := notificationEvent(s, "outbox-during-"+stage)
		err := s.IngestEvent(event)
		if stage == "freeze" {
			if err == nil {
				t.Fatal("freeze admitted a new outbox write")
			}
		} else if err != nil {
			t.Fatal("pending maintenance blocked required outbox handoff", stage, err)
		}
	}
}

func TestConcurrentBackupUsesFreshDurableAdmission(t *testing.T) {
	g, runtime, _, helpers := testMaintenanceGateway(t)
	entered, release := make(chan struct{}), make(chan struct{})
	old := g.maintenanceHelper.HTTP.Transport
	g.maintenanceHelper.HTTP.Transport = roundTripFunc(func(req *http.Request) (*http.Response, error) {
		if req.URL.Path == "/v1/backup" {
			close(entered)
			<-release
		}
		return old.RoundTrip(req)
	})
	result := make(chan error, 1)
	go func() { _, err := g.Backup(context.Background()); result <- err }()
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("first backup did not reach archive")
	}
	// Independent gateway instance defeats the in-memory mutex deliberately;
	// the SQL gate must still reject it while the first backup is packaging.
	other, err := NewGateway(g.Config, g.Store, g.Lifecycle)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = other.Backup(context.Background()); err == nil {
		t.Fatal("durable gate admitted concurrent backup")
	}
	if err = g.RecoverMaintenance(context.Background()); err != nil {
		t.Fatal(err)
	}
	runtime.mu.Lock()
	releases := len(runtime.releaseGenerations)
	runtime.mu.Unlock()
	if releases != 0 {
		t.Fatal("recovery released an executing backup")
	}
	close(release)
	if err = <-result; err != nil {
		t.Fatal(err)
	}
	_, stage, complete := latestMaintenance(t, g.Store)
	if stage != "complete" || complete != 1 || helpers.Load() != 1 {
		t.Fatal("unexpected completed backup behavior")
	}
}

func TestDailyBackupRequiresTrueIdleAndRechecksNewActivity(t *testing.T) {
	g, _, _, _ := testMaintenanceGateway(t)
	now := time.Now()
	state := g.Lifecycle.Snapshot()
	state.LastActive = now.Add(-10 * time.Minute)
	activity := Activity{RuntimeID: state.RuntimeID, SafeIdle: true, LastActivity: timestamp(state.LastActive)}
	if !dailyBackupIdle(state, activity, now, 300) {
		t.Fatal("idle runtime should be eligible")
	}
	activity.SafeIdle = false
	if dailyBackupIdle(state, activity, now, 300) {
		t.Fatal("active work lease was ignored")
	}
	activity.SafeIdle = true
	seconds := 1.0
	activity.IdleSeconds = &seconds
	if dailyBackupIdle(state, activity, now, 300) {
		t.Fatal("recent file operation was ignored")
	}
	activity.IdleSeconds = nil
	state.LastActive = now
	if dailyBackupIdle(state, activity, now, 300) {
		t.Fatal("new owner activity was ignored")
	}
	observed := g.Lifecycle.Snapshot()
	g.Lifecycle.MarkActive()
	if _, err := g.claimBackup(context.Background(), &backupAdmission{Observed: observed, DailyDate: "2099-01-01"}); err == nil {
		t.Fatal("new active operation did not invalidate daily observation")
	}
	var last string
	if err := g.Store.DB.QueryRow("SELECT value FROM settings WHERE key='daily_backup_attempt'").Scan(&last); !errors.Is(err, sql.ErrNoRows) {
		t.Fatal("rejected admission consumed daily attempt")
	}
	if g.Store.Frozen() {
		t.Fatal("rejected daily backup left a gate")
	}
}

func TestActiveReadinessRegistersBeforeQueuedDailyAdmission(t *testing.T) {
	g, _, _, _ := testMaintenanceGateway(t)
	observed := g.Lifecycle.Snapshot()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	// Hold the sole DB connection so active admission stops inside its Frozen
	// check while owning l.mu. Queue the daily claimant behind that exact check.
	held, err := g.Store.DB.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	waits := g.Store.DB.Stats().WaitCount
	active := make(chan error, 1)
	go func() { active <- g.Lifecycle.EnsureActiveReady(ctx) }()
	deadline := time.Now().Add(time.Second)
	for g.Store.DB.Stats().WaitCount == waits && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if g.Store.DB.Stats().WaitCount == waits {
		t.Fatal("active request did not reach readiness check")
	}
	claimed := make(chan error, 1)
	go func() {
		_, err := g.claimBackup(ctx, &backupAdmission{Observed: observed, DailyDate: "2099-02-01"})
		claimed <- err
	}()
	if err = held.Close(); err != nil {
		t.Fatal(err)
	}
	if err = <-active; err != nil {
		t.Fatal(err)
	}
	if err = <-claimed; err == nil {
		t.Fatal("daily backup won between readiness check and activity registration")
	}
	state := g.Lifecycle.Snapshot()
	if state.ActivityRevision != observed.ActivityRevision+1 || g.Store.Frozen() {
		t.Fatal("active admission did not register exactly once before return")
	}
}

func TestActiveReadinessCountsCreatedAndCoalescedWaiters(t *testing.T) {
	l, m, runtime := testLifecycle(t)
	runtime.ready.Store(false)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	const count = 8
	results := make(chan error, count)
	for i := 0; i < count; i++ {
		go func() { results <- l.EnsureActiveReady(ctx) }()
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		m.mu.Lock()
		created := m.creates
		m.mu.Unlock()
		if created == 1 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	runtime.ready.Store(true)
	for i := 0; i < count; i++ {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	state := l.Snapshot()
	if state.ActivityRevision != count {
		t.Fatal("created/waiting requests skipped or duplicated activity registration", state.ActivityRevision)
	}
	var raw string
	if err := l.Store.DB.QueryRow("SELECT value FROM settings WHERE key='lifecycle'").Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var persisted RuntimeState
	if err := json.Unmarshal([]byte(raw), &persisted); err != nil {
		t.Fatal(err)
	}
	if persisted.ActivityRevision != count {
		t.Fatal("active readiness returned before activity was durable")
	}
	m.mu.Lock()
	created := m.creates
	m.mu.Unlock()
	if created != 1 {
		t.Fatal("active waiters created multiple runtimes", created)
	}
}
