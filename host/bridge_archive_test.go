package host

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"filippo.io/age"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func notificationEvent(s *Store, id string) BridgeEvent {
	return BridgeEvent{ID: id, Kind: "notification", Epoch: s.Secrets.Epoch, RuntimeID: randomID(), Payload: json.RawMessage(`{"notification_id":"notice1","task_id":"task1","category":"completed"}`)}
}
func TestOutboxACKReceiptSurvivesLostResponseAndRejectsChangedContent(t *testing.T) {
	s := testStore(t)
	event := notificationEvent(s, "event1")
	if e := s.IngestEvent(event); e != nil {
		t.Fatal(e)
	}
	event.RuntimeID = randomID()
	if e := s.IngestEvent(event); e != nil {
		t.Fatal("same durable event could not replay after runtime rebuild", e)
	}
	var n int
	if e := s.DB.QueryRow("SELECT count(*) FROM notifications").Scan(&n); e != nil || n != 1 {
		t.Fatal("duplicate business effect", e, n)
	}
	event.Payload = json.RawMessage(`{"notification_id":"notice2","task_id":"task1","category":"completed"}`)
	if s.IngestEvent(event) == nil {
		t.Fatal("same event ID with changed payload was acknowledged")
	}
	if e := s.DB.QueryRow("SELECT count(*) FROM notifications").Scan(&n); e != nil || n != 1 {
		t.Fatal("conflicting event changed business state")
	}
}
func TestNewerWaitTombstoneCancelsPendingWake(t *testing.T) {
	s := testStore(t)
	payload, _ := json.Marshal(map[string]any{"task_id": "t1", "wait_generation": 2, "connection_revision": 3, "enabled": true, "resume_at": timestamp(time.Now().Add(-time.Minute))})
	event := BridgeEvent{ID: "wait2", Kind: "model.wait.upsert", Epoch: s.Secrets.Epoch, Payload: payload}
	if e := s.IngestEvent(event); e != nil {
		t.Fatal(e)
	}
	if e := s.EnqueueDue(time.Now()); e != nil {
		t.Fatal(e)
	}
	var n int
	_ = s.DB.QueryRow("SELECT count(*) FROM dispatch WHERE acked=0").Scan(&n)
	if n != 1 {
		t.Fatal("missing pending wake")
	}
	event.ID = "cancel3"
	event.Payload = json.RawMessage(`{"task_id":"t1","wait_generation":3,"connection_revision":4,"enabled":false,"resume_at":null}`)
	if e := s.IngestEvent(event); e != nil {
		t.Fatal(e)
	}
	_ = s.DB.QueryRow("SELECT count(*) FROM dispatch WHERE acked=0").Scan(&n)
	if n != 0 {
		t.Fatal("cancelled wait still has a deliverable wake")
	}
	event.ID = "late-old"
	event.Payload = payload
	if e := s.IngestEvent(event); e != nil {
		t.Fatal(e)
	}
	if e := s.EnqueueDue(time.Now()); e != nil {
		t.Fatal(e)
	}
	_ = s.DB.QueryRow("SELECT count(*) FROM dispatch WHERE acked=0").Scan(&n)
	if n != 0 {
		t.Fatal("late old registration bypassed tombstone")
	}
}
func TestScheduleCatchupOnlyLatestAndDisableTombstone(t *testing.T) {
	s := testStore(t)
	now := time.Date(2026, 10, 31, 15, 12, 30, 0, time.UTC)
	p, _ := json.Marshal(map[string]any{"schedule_id": "daily", "revision": 1, "enabled": true, "cron": "* * * * *", "timezone": "Asia/Shanghai", "next_fire_at": timestamp(now.Add(-48 * time.Hour))})
	e := s.IngestEvent(BridgeEvent{ID: "schedule1", Kind: "schedule.upsert", Epoch: s.Secrets.Epoch, Payload: p})
	if e != nil {
		t.Fatal(e)
	}
	if e = s.EnqueueDue(now); e != nil {
		t.Fatal(e)
	}
	var n int
	var body []byte
	if e = s.DB.QueryRow("SELECT count(*) FROM dispatch WHERE acked=0").Scan(&n); e != nil || n != 1 {
		t.Fatal("catchup piled up occurrences", e, n)
	}
	if e = s.DB.QueryRow("SELECT payload FROM dispatch WHERE acked=0").Scan(&body); e != nil {
		t.Fatal(e)
	}
	var occurrence struct {
		Scheduled string `json:"scheduled_at"`
	}
	_ = json.Unmarshal(body, &occurrence)
	if occurrence.Scheduled != timestamp(now.Truncate(time.Minute)) {
		t.Fatal("not the latest missed occurrence", occurrence.Scheduled)
	}
	disabled := BridgeEvent{ID: "disable2", Kind: "schedule.disable", Epoch: s.Secrets.Epoch, Payload: json.RawMessage(`{"schedule_id":"daily","revision":2,"enabled":false}`)}
	if e = s.IngestEvent(disabled); e != nil {
		t.Fatal(e)
	}
	if e = s.IngestEvent(BridgeEvent{ID: "late1", Kind: "schedule.upsert", Epoch: s.Secrets.Epoch, Payload: p}); e != nil {
		t.Fatal(e)
	}
	var enabled bool
	if e = s.DB.QueryRow("SELECT enabled FROM schedules WHERE id='daily'").Scan(&enabled); e != nil || enabled {
		t.Fatal("late registration revived a disabled schedule")
	}
}
func TestDSTSkipsNonexistentAndRepeatedLocalTimes(t *testing.T) {
	sch, e := parseSchedule("0 1 * * *", "America/New_York")
	if e != nil {
		t.Fatal(e)
	}
	first := time.Date(2026, 11, 1, 5, 0, 0, 0, time.UTC)
	if n := nextOccurrence(sch, time.Date(2026, 11, 1, 4, 59, 0, 0, time.UTC)); !n.Equal(first) {
		t.Fatal("did not select first DST fold", n)
	}
	n := nextOccurrence(sch, first)
	if n.UTC().Format(time.RFC3339) != "2026-11-02T06:00:00Z" {
		t.Fatal("repeated local time executed twice", n)
	}
	sch, e = parseSchedule("30 2 * * *", "America/New_York")
	if e != nil {
		t.Fatal(e)
	}
	n = nextOccurrence(sch, time.Date(2026, 3, 8, 5, 0, 0, 0, time.UTC))
	if n.UTC().Format(time.RFC3339) != "2026-03-09T06:30:00Z" {
		t.Fatal("nonexistent local time was not skipped", n)
	}
}

func makeEncryptedArchive(t *testing.T, entries map[string][]byte, extra *tar.Header, wrongDigest bool) (string, string) {
	t.Helper()
	key, e := age.GenerateX25519Identity()
	if e != nil {
		t.Fatal(e)
	}
	archive := filepath.Join(t.TempDir(), "backup.age")
	f, e := os.Create(archive)
	if e != nil {
		t.Fatal(e)
	}
	enc, e := age.Encrypt(f, key.Recipient())
	if e != nil {
		t.Fatal(e)
	}
	tw := tar.NewWriter(enc)
	manifest := ArchiveManifest{Format: 1, MaintenanceID: newUUID(), CreatedAt: timestamp(time.Now()), Image: "bot@sha256:" + strings.Repeat("a", 64), Files: []ArchiveEntry{}}
	for name, b := range entries {
		if e = tw.WriteHeader(&tar.Header{Name: name, Mode: 04777, Typeflag: tar.TypeReg, Size: int64(len(b))}); e != nil {
			t.Fatal(e)
		}
		if _, e = tw.Write(b); e != nil {
			t.Fatal(e)
		}
		hash := sha256.Sum256(b)
		manifest.Files = append(manifest.Files, ArchiveEntry{name, int64(len(b)), hex.EncodeToString(hash[:])})
	}
	if extra != nil {
		if e = tw.WriteHeader(extra); e != nil {
			t.Fatal(e)
		}
	}
	if wrongDigest && len(manifest.Files) > 0 {
		manifest.Files[0].SHA256 = strings.Repeat("0", 64)
	}
	b, _ := json.Marshal(manifest)
	if e = tw.WriteHeader(&tar.Header{Name: "manifest.json", Typeflag: tar.TypeReg, Mode: 0600, Size: int64(len(b))}); e != nil {
		t.Fatal(e)
	}
	_, _ = tw.Write(b)
	if e = tw.Close(); e != nil {
		t.Fatal(e)
	}
	if e = enc.Close(); e != nil {
		t.Fatal(e)
	}
	if e = f.Close(); e != nil {
		t.Fatal(e)
	}
	return archive, key.String()
}
func minimalRecoveryFiles() map[string][]byte {
	return map[string][]byte{"control/master.key": make([]byte, 32), "control/assertion.key": []byte("key-validation-is-a-later-restore-step"), "control/deployment_epoch": []byte("old-epoch-for-manifest-test"), "control/control.sqlite": []byte("db-test-placeholder"), "app/app.sqlite": []byte("app-test-placeholder"), "files/a.txt": []byte("artifact")}
}
func TestArchiveManifestAndTraversalProtection(t *testing.T) {
	archive, key := makeEncryptedArchive(t, minimalRecoveryFiles(), nil, false)
	stage := t.TempDir()
	if _, e := verifyArchive(context.Background(), archive, key, stage); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(filepath.Join(stage, "files", "a.txt"))
	if e != nil || string(b) != "artifact" {
		t.Fatal("valid archive not staged", e)
	}
	badNames := []string{"../../outside", "/etc/passwd", "files/../control/other", "files\\escape", "C:/escape", "control/unexpected"}
	for _, name := range badNames {
		t.Run(name, func(t *testing.T) {
			entries := minimalRecoveryFiles()
			entries[name] = []byte("escape")
			a, k := makeEncryptedArchive(t, entries, nil, false)
			if _, e := verifyArchive(context.Background(), a, k, t.TempDir()); e == nil {
				t.Fatal("unsafe path accepted", name)
			}
		})
	}
	for _, kind := range []byte{tar.TypeSymlink, tar.TypeLink, tar.TypeChar, tar.TypeFifo} {
		a, k := makeEncryptedArchive(t, minimalRecoveryFiles(), &tar.Header{Name: "files/link", Typeflag: kind, Linkname: "/etc/passwd"}, false)
		if _, e := verifyArchive(context.Background(), a, k, t.TempDir()); e == nil {
			t.Fatal("nonregular archive entry accepted", kind)
		}
	}
	a, k := makeEncryptedArchive(t, minimalRecoveryFiles(), nil, true)
	if _, e := verifyArchive(context.Background(), a, k, t.TempDir()); e == nil {
		t.Fatal("manifest digest mismatch accepted")
	}
}
