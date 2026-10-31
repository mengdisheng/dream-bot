package host

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type startupLogBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (b *startupLogBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.b.Write(p)
}

func captureStartupLogs(t *testing.T) *startupLogBuffer {
	t.Helper()
	b := &startupLogBuffer{}
	writer, flags, prefix := log.Writer(), log.Flags(), log.Prefix()
	log.SetOutput(b)
	log.SetFlags(0)
	log.SetPrefix("")
	t.Cleanup(func() {
		log.SetOutput(writer)
		log.SetFlags(flags)
		log.SetPrefix(prefix)
	})
	return b
}

func (b *startupLogBuffer) records(t *testing.T) []map[string]any {
	t.Helper()
	b.mu.Lock()
	defer b.mu.Unlock()
	var records []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(b.b.String()), "\n") {
		if line == "" {
			continue
		}
		var record map[string]any
		if err := json.Unmarshal([]byte(line), &record); err != nil {
			t.Fatalf("startup log is not structured JSON: %v", err)
		}
		if len(record) != 7 || record["kind"] != "performance" {
			t.Fatalf("unexpected startup telemetry fields: %+v", record)
		}
		seconds, ok := record["seconds"].(float64)
		if !ok || seconds < 0 || math.IsNaN(seconds) || math.IsInf(seconds, 0) {
			t.Fatalf("invalid startup duration: %+v", record)
		}
		records = append(records, record)
	}
	return records
}

func waitStartupDone(t *testing.T, l *Lifecycle) {
	t.Helper()
	l.mu.Lock()
	done := l.startDone
	l.mu.Unlock()
	if done != nil {
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Fatal("startup telemetry did not settle")
		}
	}
}

func TestStartupTelemetrySeparatesCreationFromAdoption(t *testing.T) {
	for _, adopted := range []bool{false, true} {
		name := "created"
		if adopted {
			name = "adopted"
		}
		t.Run(name, func(t *testing.T) {
			logs := captureStartupLogs(t)
			l, m, _ := testLifecycle(t)
			if adopted {
				m.instance = Instance{Exists: true, ContainerID: strings.Repeat("a", 64), RuntimeID: "existing-runtime", Epoch: l.Store.Secrets.Epoch, State: "running"}
			}
			ensureTest(t, l)
			waitStartupDone(t, l)
			records := logs.records(t)
			wantCount := 2
			if adopted {
				wantCount = 1
			}
			if len(records) != wantCount {
				t.Fatalf("expected %d startup measurements, got %+v", wantCount, records)
			}
			for _, record := range records {
				if record["runtime_id"] != l.Snapshot().RuntimeID || record["created"] != !adopted || record["adopted"] != adopted || record["success"] != true {
					t.Fatalf("startup correlation or adoption flags changed: %+v", record)
				}
			}
			ready := records[len(records)-1]
			if ready["stage"] != "runtime_ready" {
				t.Fatalf("missing readiness measurement: %+v", ready)
			}
			if !adopted && (records[0]["stage"] != "container_create" || ready["seconds"].(float64) < records[0]["seconds"].(float64)) {
				t.Fatalf("total startup did not include creation: %+v", records)
			}
			if adopted && (m.creates != 0 || l.Snapshot().Attempts != 0) {
				t.Fatal("timing instrumentation changed adoption or recovery counts")
			}
			// Already-ready callers do not manufacture another startup sample.
			ensureTest(t, l)
			if len(logs.records(t)) != wantCount {
				t.Fatal("ready request emitted a new startup measurement")
			}
		})
	}
}

type failingStartupManager struct{ *fakeManager }

func (m failingStartupManager) Ensure(context.Context, EnsureRequest) (Instance, error) {
	return Instance{}, errors.New("private helper failure text must not be logged")
}

func TestStartupTelemetryNeverReportsUnverifiedReady(t *testing.T) {
	for _, scenario := range []string{"create_failure", "existing_identity_mismatch", "ready_identity_mismatch"} {
		t.Run(scenario, func(t *testing.T) {
			logs := captureStartupLogs(t)
			l, m, _ := testLifecycle(t)
			switch scenario {
			case "create_failure":
				l.Manager = failingStartupManager{m}
			case "existing_identity_mismatch":
				m.instance = Instance{Exists: true, RuntimeID: "existing-runtime", Epoch: "wrong-epoch", State: "running"}
			case "ready_identity_mismatch":
				m.instance = Instance{Exists: true, RuntimeID: "existing-runtime", Epoch: l.Store.Secrets.Epoch, State: "running"}
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					writeJSON(w, http.StatusOK, Ready{Ready: true, Reconciled: true, RuntimeID: "wrong-runtime", Epoch: l.Store.Secrets.Epoch})
				}))
				defer server.Close()
				l.Runtime.URL = server.URL
			}
			ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
			defer cancel()
			l.start(ctx)
			if l.Snapshot().State == "READY" {
				t.Fatal("unverified startup became ready")
			}
			records := logs.records(t)
			for _, record := range records {
				if record["stage"] == "runtime_ready" {
					t.Fatalf("failure emitted readiness telemetry: %+v", record)
				}
			}
			if scenario == "create_failure" && (len(records) != 1 || records[0]["success"] != false) {
				t.Fatalf("failed create did not retain its bounded timing: %+v", records)
			}
			if scenario != "create_failure" && len(records) != 0 {
				t.Fatalf("adoption failure emitted a creation measurement: %+v", records)
			}
		})
	}
}
