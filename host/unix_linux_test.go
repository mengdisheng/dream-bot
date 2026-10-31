//go:build linux

package host

import (
	"context"
	"database/sql"
	"encoding/json"
	"filippo.io/age"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestOfflineSnapshotOwnershipAllowsRealAPIUID(t *testing.T) {
	if os.Getenv("PERSONAL_BOT_TEST_API_SNAPSHOT") == "1" {
		if os.Getuid() != 31001 || os.Getgid() != 31000 {
			t.Fatal("child did not drop to the actual API role")
		}
		root := os.Getenv("PERSONAL_BOT_TEST_API_ROOT")
		folder := filepath.Join(root, "app", "maintenance", newUUID())
		if err := os.Mkdir(folder, 0700); err != nil {
			t.Fatal("API role cannot create next online snapshot directory", err)
		}
		db, err := sql.Open("sqlite", filepath.Join(root, "app", "app.sqlite"))
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
		if _, err = db.Exec("VACUUM INTO ?", filepath.Join(folder, "app.sqlite")); err != nil {
			t.Fatal("API role cannot write next online snapshot", err)
		}
		var value string
		if err = db.QueryRow("SELECT value FROM fixture").Scan(&value); err != nil || value != "offline-then-online" {
			t.Fatal("API role cannot reopen original data", err)
		}
		return
	}
	if os.Getuid() != 0 {
		t.Skip("requires root to test real API UID/GID transition")
	}
	root, err := os.MkdirTemp("", "personal-bot-offline-role-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	if err = os.Chmod(root, 0711); err != nil {
		t.Fatal(err)
	}
	app := filepath.Join(root, "app")
	if err = os.Mkdir(app, 0700); err != nil {
		t.Fatal(err)
	}
	parent := filepath.Join(app, "maintenance")
	if err = os.Mkdir(parent, 0700); err != nil {
		t.Fatal(err)
	}
	// Model the old root:root 0700 parent produced by offline snapshots.
	db, err := sql.Open("sqlite", filepath.Join(app, "app.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec("CREATE TABLE fixture(value TEXT);INSERT INTO fixture VALUES('offline-then-online')"); err != nil {
		t.Fatal(err)
	}
	if err = db.Close(); err != nil {
		t.Fatal(err)
	}
	d := &DockerManager{Config: HelperConfig{DataRoot: root}, HTTP: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("[]")), Header: make(http.Header)}, nil
	})}}
	id := newUUID()
	if err = d.OfflineSnapshot(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{app, parent, filepath.Join(parent, id), filepath.Join(parent, id, "app.sqlite"), filepath.Join(app, "app.sqlite")} {
		info, err := os.Lstat(path)
		if err != nil {
			t.Fatal(err)
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != 31001 || stat.Gid != 31000 {
			t.Fatal("offline snapshot left incorrect ownership", path)
		}
		mode := os.FileMode(0600)
		if info.IsDir() {
			mode = 0700
		}
		if info.Mode().Perm() != mode {
			t.Fatal("incorrect snapshot permissions", path, info.Mode())
		}
	}
	// The go test build directory may be private to dev/root. Copy this exact
	// test binary into the isolated traversable fixture before dropping IDs.
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	source, err := os.Open(exe)
	if err != nil {
		t.Fatal(err)
	}
	defer source.Close()
	child := filepath.Join(root, "api-ownership.test")
	target, err := os.OpenFile(child, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0755)
	if err != nil {
		t.Fatal(err)
	}
	_, copyErr := io.Copy(target, source)
	closeErr := target.Close()
	if copyErr != nil || closeErr != nil {
		t.Fatal("copy test binary", copyErr, closeErr)
	}
	if err = os.Chmod(child, 0755); err != nil {
		t.Fatal(err)
	}
	command := exec.Command(child, "-test.run=^TestOfflineSnapshotOwnershipAllowsRealAPIUID$", "-test.v")
	command.Env = []string{"PERSONAL_BOT_TEST_API_SNAPSHOT=1", "PERSONAL_BOT_TEST_API_ROOT=" + root}
	command.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 31001, Gid: 31000, Groups: []uint32{}}}
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("real API role failed: %v\n%s", err, output)
	}
}

func TestOfflineSnapshotRejectsUnsafePaths(t *testing.T) {
	if os.Getuid() != 0 {
		t.Skip("requires root for role ownership validation")
	}
	for _, kind := range []string{"parent_link", "parent_wrong_uid", "database_link", "database_hardlink"} {
		t.Run(kind, func(t *testing.T) {
			root := t.TempDir()
			app := filepath.Join(root, "app")
			if err := os.Mkdir(app, 0700); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(app, "app.sqlite")
			db, err := sql.Open("sqlite", path)
			if err != nil {
				t.Fatal(err)
			}
			if _, err = db.Exec("CREATE TABLE fixture(value TEXT)"); err != nil {
				t.Fatal(err)
			}
			db.Close()
			switch kind {
			case "parent_link":
				if err = os.Symlink(t.TempDir(), filepath.Join(app, "maintenance")); err != nil {
					t.Fatal(err)
				}
			case "parent_wrong_uid":
				p := filepath.Join(app, "maintenance")
				if err = os.Mkdir(p, 0700); err != nil {
					t.Fatal(err)
				}
				if err = os.Chown(p, 31002, 31000); err != nil {
					t.Fatal(err)
				}
			case "database_link":
				outside := filepath.Join(root, "outside.sqlite")
				if err = os.Rename(path, outside); err != nil {
					t.Fatal(err)
				}
				if err = os.Symlink(outside, path); err != nil {
					t.Fatal(err)
				}
			case "database_hardlink":
				if err = os.Link(path, filepath.Join(root, "hardlink.sqlite")); err != nil {
					t.Fatal(err)
				}
			}
			d := &DockerManager{Config: HelperConfig{DataRoot: root}, HTTP: &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("[]")), Header: make(http.Header)}, nil
			})}}
			if err = d.OfflineSnapshot(context.Background(), newUUID()); err == nil {
				t.Fatal("unsafe offline snapshot path was accepted")
			}
		})
	}
}

func TestRealUnixPeerAndStableDirectoryAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	socket := filepath.Join(dir, "bridge.sock")
	before, e := os.Stat(dir)
	if e != nil {
		t.Fatal(e)
	}
	inode := before.Sys().(*syscall.Stat_t).Ino
	serve := func() (func(), *http.Client) {
		listener, e := listenUnix(socket)
		if e != nil {
			t.Fatal(e)
		}
		server := unixServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			uid, ok := peerUID(r.Context())
			if !ok || uid != uint32(os.Getuid()) {
				writeError(w, 403, "peer_forbidden", "actual credential failed", false)
				return
			}
			writeJSON(w, 200, map[string]any{"uid": uid})
		}))
		go server.Serve(listener)
		return func() { server.Close(); listener.Close() }, unixClient(socket, 2*time.Second)
	}
	for i := 0; i < 2; i++ {
		close, client := serve()
		request, e := http.NewRequest("GET", "http://bridge/check", nil)
		if e != nil {
			t.Fatal(e)
		}
		request.Header.Set("X-UID", "99999")
		response, e := client.Do(request)
		if e != nil {
			t.Fatal(e)
		}
		if response.StatusCode != 200 {
			t.Fatal("real peer credential failed")
		}
		response.Body.Close()
		close()
		after, e := os.Stat(dir)
		if e != nil || after.Sys().(*syscall.Stat_t).Ino != inode {
			t.Fatal("socket restart replaced mounted parent directory")
		}
	}
	uid := uint32(os.Getuid() + 1)
	close, _ := serve()
	defer close()
	client := unixPeerClient(socket, 2*time.Second, &uid)
	if _, e = client.Get("http://bridge/check"); e == nil {
		t.Fatal("client trusted a server with the wrong peer UID")
	}
}
func TestControlDatabaseCannotHaveTwoOnlineOwners(t *testing.T) {
	s := testStore(t)
	if second, e := OpenStore(s.Dir); e == nil {
		second.Close()
		t.Fatal("second process-style owner opened the live control database")
	}
}
func TestInterruptedRestoreRollsBackAllInstalledRoots(t *testing.T) {
	root := t.TempDir()
	stage := filepath.Join(root, ".restore-apply-test")
	if e := os.MkdirAll(filepath.Join(stage, "previous"), 0700); e != nil {
		t.Fatal(e)
	}
	for _, area := range []string{"control", "app", "files", "browser"} {
		if e := os.MkdirAll(filepath.Join(root, area), 0700); e != nil {
			t.Fatal(e)
		}
		if e := os.WriteFile(filepath.Join(root, area, "value"), []byte("old-"+area), 0600); e != nil {
			t.Fatal(e)
		}
	}
	for _, area := range []string{"control", "app"} {
		if e := os.Rename(filepath.Join(root, area), filepath.Join(stage, "previous", area)); e != nil {
			t.Fatal(e)
		}
		if e := os.MkdirAll(filepath.Join(root, area), 0700); e != nil {
			t.Fatal(e)
		}
		if e := os.WriteFile(filepath.Join(root, area, "value"), []byte("new-"+area), 0600); e != nil {
			t.Fatal(e)
		}
	}
	journal, _ := json.Marshal(map[string]string{"stage": filepath.Base(stage)})
	if e := os.WriteFile(filepath.Join(root, "restore-in-progress.json"), journal, 0600); e != nil {
		t.Fatal(e)
	}
	if e := RecoverRestoreJournal(root); e != nil {
		t.Fatal(e)
	}
	for _, area := range []string{"control", "app", "files", "browser"} {
		b, e := os.ReadFile(filepath.Join(root, area, "value"))
		if e != nil || string(b) != "old-"+area {
			t.Fatal("interrupted restore left mixed generations", area, e)
		}
	}
	if _, e := os.Stat(stage); !os.IsNotExist(e) {
		t.Fatal("successful recovery left private staging")
	}
}

func TestRealEncryptedBackupAndOfflineRestore(t *testing.T) {
	if os.Getuid() != 0 {
		t.Skip("root-only ownership/maintenance integration")
	}
	root := t.TempDir()
	for _, area := range []string{"app", "files", "browser"} {
		if e := os.MkdirAll(filepath.Join(root, area), 0700); e != nil {
			t.Fatal(e)
		}
	}
	s, e := OpenStore(filepath.Join(root, "control"))
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	owner, _ := setupTestOwner(t, s)
	if _, _, e = s.NewSession(owner, "old-device"); e != nil {
		t.Fatal(e)
	}
	if _, e = s.DB.Exec("INSERT INTO schedules(id,revision,enabled,cron,timezone,next_fire) VALUES('schedule1',1,1,'0 9 * * *','Asia/Shanghai',1)"); e != nil {
		t.Fatal(e)
	}
	key, e := age.GenerateX25519Identity()
	if e != nil {
		t.Fatal(e)
	}
	d := &DockerManager{Config: HelperConfig{DataRoot: root, GatewayUID: uint32(os.Getuid()), Image: "sha256:" + strings.Repeat("a", 64)}, HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("[]")), Header: make(http.Header)}, nil
	})}}
	app, e := sql.Open("sqlite", filepath.Join(root, "app", "app.sqlite"))
	if e != nil {
		t.Fatal(e)
	}
	if _, e = app.Exec("CREATE TABLE fixture(v TEXT);INSERT INTO fixture VALUES('old')"); e != nil {
		t.Fatal(e)
	}
	app.Close()
	if e = os.WriteFile(filepath.Join(root, "files", "artifact"), []byte("old-file"), 0600); e != nil {
		t.Fatal(e)
	}
	if e = os.WriteFile(filepath.Join(root, "browser", "Preferences"), []byte(`{"profile":"fixture"}`), 0600); e != nil {
		t.Fatal(e)
	}
	backup := func() BackupInfo {
		id := newUUID()
		dir := filepath.Join(root, "control", "maintenance", id)
		if e := os.MkdirAll(dir, 0700); e != nil {
			t.Fatal(e)
		}
		if e := s.Snapshot(context.Background(), filepath.Join(dir, "control.sqlite")); e != nil {
			t.Fatal(e)
		}
		if e := d.OfflineSnapshot(context.Background(), id); e != nil {
			t.Fatal(e)
		}
		info, e := d.BackupArchive(context.Background(), BackupRequest{MaintenanceID: id, Recipient: key.Recipient().String()})
		if e != nil {
			t.Fatal(e)
		}
		return info
	}
	original := backup()
	oldEpoch := s.Secrets.Epoch
	if e = os.WriteFile(filepath.Join(root, "files", "artifact"), []byte("new-file"), 0600); e != nil {
		t.Fatal(e)
	}
	current := backup()
	if original.ID == current.ID {
		t.Fatal("backup IDs collided")
	}
	preview, e := d.RestorePreview(context.Background(), RestoreRequest{ID: original.ID, Identity: key.String()})
	if e != nil || len(preview.Files) == 0 {
		t.Fatal("encrypted preview failed", e)
	}
	if _, e = d.RestoreArchive(context.Background(), RestoreRequest{ID: original.ID, Identity: key.String(), PreviousBackupID: current.ID, Confirm: true}); e == nil {
		t.Fatal("restore overwrote a live gateway control DB")
	}
	if e = s.Close(); e != nil {
		t.Fatal(e)
	}
	restored, e := d.RestoreArchive(context.Background(), RestoreRequest{ID: original.ID, Identity: key.String(), PreviousBackupID: current.ID, Confirm: true})
	if e != nil {
		t.Fatal(e)
	}
	if restored["deployment_epoch"] == oldEpoch || restored["safety_suspended"] != true {
		t.Fatal("restore did not invalidate old capabilities")
	}
	after, e := OpenStore(filepath.Join(root, "control"))
	if e != nil {
		t.Fatal(e)
	}
	defer after.Close()
	var sessions, enabled int
	if e = after.DB.QueryRow("SELECT count(*) FROM sessions").Scan(&sessions); e != nil || sessions != 0 {
		t.Fatal("old sessions survived restore", e)
	}
	if e = after.DB.QueryRow("SELECT count(*) FROM schedules WHERE enabled=1").Scan(&enabled); e != nil || enabled != 0 {
		t.Fatal("restored schedules auto-enabled", e)
	}
	b, e := os.ReadFile(filepath.Join(root, "files", "artifact"))
	if e != nil || string(b) != "old-file" {
		t.Fatal("restore did not restore prior artifact", e)
	}
	if _, e = os.Stat(filepath.Join(root, "app", ".restore-required.json")); e != nil {
		t.Fatal("runtime reconciliation marker missing")
	}
	if _, e = os.Stat(filepath.Join(root, "restore-in-progress.json")); !os.IsNotExist(e) {
		t.Fatal("completed restore journal remains")
	}
	st, e := os.Stat(filepath.Join(root, "browser"))
	if e != nil || st.Sys().(*syscall.Stat_t).Uid != 31003 {
		t.Fatal("browser profile ownership not restored")
	}
}
