package host

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"filippo.io/age"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

var maintenanceIDPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
var backupIDPattern = regexp.MustCompile(`^[0-9]{8}T[0-9]{6}Z-[0-9a-f-]{36}\.age$`)

func validBackupID(id string) bool { return backupIDPattern.MatchString(id) }

type BackupInfo struct {
	ID        string `json:"id"`
	CreatedAt string `json:"created_at"`
	Size      int64  `json:"size"`
	Verified  bool   `json:"verified"`
	Image     string `json:"image,omitempty"`
}

func ListBackups(dir string) ([]BackupInfo, error) {
	items := []BackupInfo{}
	entries, e := os.ReadDir(dir)
	if e != nil {
		return items, e
	}
	for _, v := range entries {
		if !validBackupID(v.Name()) || v.Type()&os.ModeSymlink != 0 {
			continue
		}
		st, e := v.Info()
		if e != nil || !st.Mode().IsRegular() {
			continue
		}
		items = append(items, BackupInfo{ID: v.Name(), CreatedAt: timestamp(st.ModTime()), Size: st.Size(), Verified: true})
	}
	return items, nil
}
func (g *Gateway) MaintenanceStatus() map[string]any {
	recipient := g.Config.BackupRecipient
	_ = g.Store.DB.QueryRow("SELECT value FROM settings WHERE key='backup_recipient'").Scan(&recipient)
	stage := "idle"
	_ = g.Store.DB.QueryRow("SELECT stage FROM maintenance ORDER BY created_at DESC,rowid DESC LIMIT 1").Scan(&stage)
	return map[string]any{"stage": stage, "active": g.Store.Frozen(), "backup_configured": recipient != "", "recipient_public": recipient, "local_commands": []string{"backup", "restore", "upgrade", "rollback"}}
}
func (g *Gateway) Backup(ctx context.Context) (BackupInfo, error) {
	return g.backup(ctx, nil)
}

func (g *Gateway) backup(ctx context.Context, admission *backupAdmission) (result BackupInfo, err error) {
	var empty BackupInfo
	// This prevents the background recovery worker from releasing an operation
	// which is still executing. Admission itself remains a durable SQL gate.
	if !g.backupMu.TryLock() {
		return empty, errors.New("maintenance already active")
	}
	defer g.backupMu.Unlock()
	recipient := g.Config.BackupRecipient
	_ = g.Store.DB.QueryRow("SELECT value FROM settings WHERE key='backup_recipient'").Scan(&recipient)
	if _, e := age.ParseX25519Recipient(recipient); e != nil {
		return empty, errors.New("configure an age public recipient before backup")
	}
	record, e := g.claimBackup(ctx, admission)
	if e != nil {
		return empty, e
	}
	id := record.ID
	defer func() {
		if err == nil {
			return
		}
		// Never open admission before release is acknowledged. A timeout can
		// mean the runtime committed drain/freeze but its response was lost.
		if _, e := g.Store.DB.Exec("UPDATE maintenance SET stage='releasing',error='maintenance did not finish' WHERE id=?", id); e != nil {
			err = errors.Join(err, errors.New("maintenance cleanup remains pending"))
			return
		}
		release, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if e := g.releaseMaintenance(release, record); e != nil {
			err = errors.Join(err, e)
		}
	}()
	instance, inspectErr := g.Lifecycle.Manager.Inspect(ctx)
	if inspectErr != nil {
		return empty, inspectErr
	}
	online := instance.Exists
	if online {
		if record.RuntimeID == "" || instance.RuntimeID != record.RuntimeID || instance.ContainerID != record.ContainerID || instance.Epoch != record.Epoch || instance.State != "running" {
			return empty, errors.New("runtime changed before backup admission")
		}
	}
	if online {
		for _, stage := range []string{"drain", "freeze"} {
			var response struct {
				Drained       bool   `json:"drained"`
				MaintenanceID string `json:"maintenance_id"`
				Frozen        bool   `json:"frozen"`
				Snapshot      string `json:"snapshot_path"`
			}
			if e := g.Lifecycle.Runtime.callForRuntime(ctx, "POST", "/internal/maintenance/stage", map[string]string{"maintenance_id": id, "stage": stage}, &response, record.RuntimeID); e != nil {
				return empty, e
			}
			if stage == "drain" && (!response.Drained || response.MaintenanceID != id) {
				return empty, errors.New("runtime has not drained for backup")
			}
			if stage == "freeze" {
				expected := "/data/app/maintenance/" + id + "/app.sqlite"
				if !response.Frozen || response.Snapshot != expected {
					return empty, errors.New("runtime did not produce the fixed maintenance snapshot")
				}
			}
		}
	} else {
		helper := g.maintenanceHelper
		if e := helper.call(ctx, "/v1/offline-snapshot", map[string]string{"maintenance_id": id}, &struct {
			Snapshot bool `json:"snapshot"`
		}{}); e != nil {
			return empty, e
		}
	}
	if _, e := g.Store.DB.Exec("UPDATE maintenance SET stage='freeze' WHERE id=?", id); e != nil {
		return empty, e
	}
	snapDir := filepath.Join(g.Config.StateDir, "maintenance", id)
	if e := os.MkdirAll(snapDir, 0700); e != nil {
		return empty, e
	}
	if e := g.Store.Snapshot(ctx, filepath.Join(snapDir, "control.sqlite")); e != nil {
		return empty, e
	}
	if online {
		var activity Activity
		if e := g.Lifecycle.Runtime.callForRuntime(ctx, "GET", "/internal/activity", nil, &activity, record.RuntimeID); e != nil {
			return empty, e
		}
		if activity.RuntimeID != record.RuntimeID {
			return empty, errors.New("runtime activity identity changed during backup")
		}
		g.Lifecycle.mu.Lock()
		if g.Lifecycle.state.RuntimeID != record.RuntimeID || g.Lifecycle.state.Epoch != record.Epoch {
			g.Lifecycle.mu.Unlock()
			return empty, errors.New("runtime generation changed during backup")
		}
		g.Lifecycle.state.IntakeRevision = activity.IntakeRevision
		g.Lifecycle.mu.Unlock()
		if e := g.Lifecycle.Stop(ctx, false, "backup"); e != nil {
			return empty, e
		}
		state := g.Lifecycle.Snapshot()
		if state.State != "ABSENT" || state.RecoveryRequired {
			return empty, errors.New("backup requires a verified clean runtime removal")
		}
	}
	helper := g.maintenanceHelper
	if e := helper.call(ctx, "/v1/backup", map[string]string{"maintenance_id": id, "recipient": recipient}, &result); e != nil {
		return empty, e
	}
	if _, e := g.Store.DB.Exec("UPDATE maintenance SET stage='complete',cleanup_complete=1 WHERE id=?", id); e != nil {
		return empty, e
	}
	return result, nil
}

type ArchiveEntry struct {
	Path   string `json:"path"`
	Size   int64  `json:"size"`
	SHA256 string `json:"sha256"`
}
type ArchiveManifest struct {
	Format        int            `json:"format"`
	MaintenanceID string         `json:"maintenance_id"`
	CreatedAt     string         `json:"created_at"`
	Image         string         `json:"image"`
	Files         []ArchiveEntry `json:"files"`
}
type BackupRequest struct {
	MaintenanceID string `json:"maintenance_id"`
	Recipient     string `json:"recipient"`
}
type RestoreRequest struct {
	ID               string `json:"id"`
	Identity         string `json:"identity"`
	PreviousBackupID string `json:"previous_backup_id,omitempty"`
	Confirm          bool   `json:"confirm"`
}

func (d *DockerManager) BackupArchive(ctx context.Context, r BackupRequest) (BackupInfo, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	var empty BackupInfo
	if !maintenanceIDPattern.MatchString(r.MaintenanceID) {
		return empty, errors.New("invalid maintenance ID")
	}
	recipient, e := age.ParseX25519Recipient(r.Recipient)
	if e != nil {
		return empty, errors.New("invalid backup public recipient")
	}
	v, e := d.inspect(ctx)
	if e != nil || v.Exists {
		return empty, errors.New("runtime must be absent before archive packaging")
	}
	root := d.Config.DataRoot
	controlSnapshot := filepath.Join(root, "control", "maintenance", r.MaintenanceID, "control.sqlite")
	appSnapshot := filepath.Join(root, "app", "maintenance", r.MaintenanceID, "app.sqlite")
	for _, p := range []string{controlSnapshot, appSnapshot} {
		st, e := os.Lstat(p)
		if e != nil || !st.Mode().IsRegular() {
			return empty, errors.New("required fixed maintenance snapshot is missing")
		}
	}
	dir := filepath.Join(root, "backups")
	if e = os.MkdirAll(dir, 0750); e != nil {
		return empty, e
	}
	if e = os.Chmod(dir, 0750); e != nil {
		return empty, e
	}
	_ = os.Chown(dir, 0, int(d.Config.GatewayGID))
	id := time.Now().UTC().Format("20060102T150405Z") + "-" + newUUID() + ".age"
	tmp, e := os.CreateTemp(dir, ".archive-*.partial")
	if e != nil {
		return empty, e
	}
	tmpPath := tmp.Name()
	complete := false
	defer func() {
		tmp.Close()
		if !complete {
			_ = os.Remove(tmpPath)
		}
	}()
	if e = tmp.Chmod(0600); e != nil {
		return empty, e
	}
	enc, e := age.Encrypt(tmp, recipient)
	if e != nil {
		return empty, e
	}
	tw := tar.NewWriter(enc)
	manifest := ArchiveManifest{Format: 1, MaintenanceID: r.MaintenanceID, CreatedAt: timestamp(time.Now()), Image: d.Config.Image, Files: []ArchiveEntry{}}
	appendFile := func(logical, physical string) error {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		st, e := os.Lstat(physical)
		if e != nil {
			return e
		}
		if !st.Mode().IsRegular() {
			return errors.New("backup rejects links and nonregular files")
		}
		available, total, spaceErr := diskSpace(root)
		reserve := max(uint64(1<<30), total/10)
		if spaceErr != nil || available < uint64(st.Size())+reserve {
			return errors.New("insufficient disk reserve for encrypted backup")
		}
		f, e := os.Open(physical)
		if e != nil {
			return e
		}
		defer f.Close()
		if e = tw.WriteHeader(&tar.Header{Name: logical, Mode: 0600, Size: st.Size(), Typeflag: tar.TypeReg, ModTime: st.ModTime()}); e != nil {
			return e
		}
		h := sha256.New()
		n, e := io.Copy(io.MultiWriter(tw, h), io.LimitReader(f, st.Size()+1))
		if e != nil {
			return e
		}
		if n != st.Size() {
			return errors.New("file changed during frozen backup")
		}
		manifest.Files = append(manifest.Files, ArchiveEntry{logical, n, hex.EncodeToString(h.Sum(nil))})
		return nil
	}
	for _, name := range []string{"master.key", "assertion.key", "deployment_epoch"} {
		if e = appendFile("control/"+name, filepath.Join(root, "control", name)); e != nil {
			return empty, e
		}
	}
	if e = appendFile("control/control.sqlite", controlSnapshot); e != nil {
		return empty, e
	}
	if e = appendFile("app/app.sqlite", appSnapshot); e != nil {
		return empty, e
	}
	for _, area := range []string{"app", "files", "browser"} {
		base := filepath.Join(root, area)
		e = filepath.WalkDir(base, func(p string, entry fs.DirEntry, walkErr error) error {
			if walkErr != nil {
				return walkErr
			}
			relative, err := filepath.Rel(base, p)
			if err != nil {
				return err
			}
			if relative == "." {
				return nil
			}
			name := entry.Name()
			if entry.IsDir() {
				if area == "app" && relative == "maintenance" {
					return filepath.SkipDir
				}
				if area == "browser" {
					switch name {
					case "Cache", "Code Cache", "GPUCache", "GrShaderCache", "DawnGraphiteCache", "ShaderCache", "Crashpad":
						return filepath.SkipDir
					}
				}
				return nil
			}
			if area == "app" && (name == "app.sqlite" || strings.HasPrefix(name, "app.sqlite-") || name == ".restore-required.json") {
				return nil
			}
			if area == "browser" && (strings.HasPrefix(name, "Singleton") || name == "DevToolsActivePort") {
				return nil
			}
			return appendFile(area+"/"+filepath.ToSlash(relative), p)
		})
		if e != nil {
			return empty, e
		}
	}
	b, e := json.Marshal(manifest)
	if e != nil {
		return empty, e
	}
	if e = tw.WriteHeader(&tar.Header{Name: "manifest.json", Mode: 0600, Size: int64(len(b)), Typeflag: tar.TypeReg}); e != nil {
		return empty, e
	}
	if _, e = tw.Write(b); e != nil {
		return empty, e
	}
	if e = tw.Close(); e != nil {
		return empty, e
	}
	if e = enc.Close(); e != nil {
		return empty, e
	}
	if e = tmp.Sync(); e != nil {
		return empty, e
	}
	if e = tmp.Close(); e != nil {
		return empty, e
	}
	if e = os.Chown(tmpPath, 0, int(d.Config.GatewayGID)); e != nil {
		return empty, e
	}
	if e = os.Chmod(tmpPath, 0640); e != nil {
		return empty, e
	}
	destination := filepath.Join(dir, id)
	if e = os.Rename(tmpPath, destination); e != nil {
		return empty, e
	}
	if e = syncDirectory(dir); e != nil {
		return empty, e
	}
	complete = true
	st, e := os.Stat(destination)
	if e != nil {
		return empty, e
	}
	result := BackupInfo{ID: id, CreatedAt: manifest.CreatedAt, Size: st.Size(), Verified: true, Image: manifest.Image}
	index, _ := json.Marshal(result)
	_ = os.WriteFile(destination+".json", index, 0640)
	_ = os.Chown(destination+".json", 0, int(d.Config.GatewayGID))
	completed, listErr := ListBackups(dir)
	if listErr != nil {
		return empty, listErr
	}
	if len(completed) > 7 {
		sort.Slice(completed, func(i, j int) bool { return completed[i].ID < completed[j].ID })
		for _, old := range completed[:len(completed)-7] {
			if e = os.Remove(filepath.Join(dir, old.ID)); e != nil {
				return empty, e
			}
			if e = os.Remove(filepath.Join(dir, old.ID+".json")); e != nil && !os.IsNotExist(e) {
				return empty, e
			}
		}
		if e = syncDirectory(dir); e != nil {
			return empty, e
		}
	}
	return result, nil
}

func archivePathAllowed(name string) bool {
	if name == "" || strings.ContainsAny(name, "\\\x00") || strings.HasPrefix(name, "/") || path.Clean(name) != name || strings.Contains(name, ":") {
		return false
	}
	if name == "manifest.json" {
		return true
	}
	if strings.HasPrefix(name, "control/") {
		switch strings.TrimPrefix(name, "control/") {
		case "master.key", "assertion.key", "deployment_epoch", "control.sqlite":
			return true
		}
		return false
	}
	return strings.HasPrefix(name, "app/") || strings.HasPrefix(name, "files/") || strings.HasPrefix(name, "browser/")
}
func verifyArchive(ctx context.Context, archive, identity, stage string) (ArchiveManifest, error) {
	var manifest ArchiveManifest
	key, e := age.ParseX25519Identity(strings.TrimSpace(identity))
	if e != nil {
		return manifest, errors.New("invalid age recovery identity")
	}
	f, e := os.Open(archive)
	if e != nil {
		return manifest, e
	}
	defer f.Close()
	reader, e := age.Decrypt(f, key)
	if e != nil {
		return manifest, errors.New("archive decryption failed")
	}
	tr := tar.NewReader(reader)
	seen := map[string]ArchiveEntry{}
	total := int64(0)
	manifestSeen := false
	for {
		select {
		case <-ctx.Done():
			return manifest, ctx.Err()
		default:
		}
		h, e := tr.Next()
		if e == io.EOF {
			break
		}
		if e != nil {
			return manifest, e
		}
		if !archivePathAllowed(h.Name) || h.Typeflag != tar.TypeReg || h.Size < 0 || h.Size > 2<<30 || len(seen) > 200000 {
			return manifest, errors.New("unsafe archive entry")
		}
		if _, ok := seen[h.Name]; ok {
			return manifest, errors.New("duplicate archive path")
		}
		available, fsTotal, spaceErr := diskSpace(stage)
		reserve := max(uint64(1<<30), fsTotal/10)
		if spaceErr != nil || available < uint64(h.Size)+reserve {
			return manifest, errors.New("insufficient disk reserve for restore staging")
		}
		total += h.Size
		if total > 20<<30 {
			return manifest, errors.New("archive exceeds restore byte limit")
		}
		if h.Name == "manifest.json" {
			if manifestSeen || h.Size > 4<<20 {
				return manifest, errors.New("invalid manifest")
			}
			manifestSeen = true
			b, e := io.ReadAll(io.LimitReader(tr, 4<<20))
			if e != nil {
				return manifest, e
			}
			if e = json.Unmarshal(b, &manifest); e != nil {
				return manifest, e
			}
			continue
		}
		destination := filepath.Join(stage, filepath.FromSlash(h.Name))
		if e = os.MkdirAll(filepath.Dir(destination), 0700); e != nil {
			return manifest, e
		}
		out, e := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if e != nil {
			return manifest, e
		}
		hash := sha256.New()
		n, copyErr := io.Copy(io.MultiWriter(out, hash), tr)
		syncErr := out.Sync()
		closeErr := out.Close()
		if copyErr != nil || syncErr != nil || closeErr != nil || n != h.Size {
			return manifest, errors.New("incomplete restore entry")
		}
		seen[h.Name] = ArchiveEntry{h.Name, n, hex.EncodeToString(hash.Sum(nil))}
	}
	remaining, drainErr := io.Copy(io.Discard, io.LimitReader(reader, (1<<20)+1))
	if drainErr != nil || remaining > 1<<20 {
		return manifest, errors.New("archive authentication or trailing data invalid")
	}
	if !manifestSeen || manifest.Format != 1 || !maintenanceIDPattern.MatchString(manifest.MaintenanceID) || len(manifest.Files) != len(seen) || !immutableImagePattern.MatchString(manifest.Image) {
		return manifest, errors.New("incomplete archive manifest")
	}
	for _, entry := range manifest.Files {
		actual, ok := seen[entry.Path]
		if !ok || actual != entry {
			return manifest, errors.New("archive digest mismatch")
		}
		delete(seen, entry.Path)
	}
	for _, required := range []string{"control/master.key", "control/assertion.key", "control/deployment_epoch", "control/control.sqlite", "app/app.sqlite"} {
		if _, e = os.Stat(filepath.Join(stage, filepath.FromSlash(required))); e != nil {
			return manifest, errors.New("required recovery file missing")
		}
	}
	return manifest, nil
}
func syncDirectory(path string) error {
	f, e := os.Open(path)
	if e != nil {
		return e
	}
	defer f.Close()
	return f.Sync()
}
func safeRemoveStage(root, stage string) error {
	base, e := filepath.Abs(root)
	if e != nil {
		return e
	}
	target, e := filepath.Abs(stage)
	if e != nil {
		return e
	}
	if filepath.Dir(target) != base || !strings.HasPrefix(filepath.Base(target), ".restore-") {
		return errors.New("refusing out-of-root cleanup")
	}
	return os.RemoveAll(target)
}
func (d *DockerManager) RestorePreview(ctx context.Context, r RestoreRequest) (ArchiveManifest, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	var empty ArchiveManifest
	if !validBackupID(r.ID) {
		return empty, errors.New("invalid backup ID")
	}
	if instance, e := d.inspect(ctx); e != nil || instance.Exists {
		return empty, errors.New("runtime must be absent for restore verification")
	}
	stage, e := os.MkdirTemp(d.Config.DataRoot, ".restore-preview-")
	if e != nil {
		return empty, e
	}
	defer safeRemoveStage(d.Config.DataRoot, stage)
	return verifyArchive(ctx, filepath.Join(d.Config.DataRoot, "backups", r.ID), r.Identity, stage)
}
func (d *DockerManager) RestoreArchive(ctx context.Context, r RestoreRequest) (map[string]any, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !r.Confirm {
		return nil, errors.New("restore requires explicit --confirm after preview")
	}
	if !validBackupID(r.ID) || !validBackupID(r.PreviousBackupID) || r.ID == r.PreviousBackupID {
		return nil, errors.New("a separate pre-restore backup is required")
	}
	if st, e := os.Stat(filepath.Join(d.Config.DataRoot, "backups", r.PreviousBackupID)); e != nil || !st.Mode().IsRegular() || st.Size() == 0 {
		return nil, errors.New("pre-restore backup is missing")
	}
	instance, e := d.inspect(ctx)
	if e != nil || instance.Exists {
		return nil, errors.New("runtime must be removed before restore")
	}
	controlLock, e := acquireStoreLock(filepath.Join(d.Config.DataRoot, "control"))
	if e != nil {
		return nil, errors.New("gateway must be stopped before restore")
	}
	defer releaseStoreLock(controlLock)
	stage, e := os.MkdirTemp(d.Config.DataRoot, ".restore-apply-")
	if e != nil {
		return nil, e
	}
	journalPath := filepath.Join(d.Config.DataRoot, "restore-in-progress.json")
	defer func() {
		if _, e := os.Stat(journalPath); os.IsNotExist(e) {
			_ = safeRemoveStage(d.Config.DataRoot, stage)
		}
	}()
	manifest, e := verifyArchive(ctx, filepath.Join(d.Config.DataRoot, "backups", r.ID), r.Identity, stage)
	if e != nil {
		return nil, e
	}
	epoch := randomID()
	if e = os.WriteFile(filepath.Join(stage, "control", "deployment_epoch"), []byte(epoch), 0600); e != nil {
		return nil, e
	}
	s, e := OpenStore(filepath.Join(stage, "control"))
	if e != nil {
		return nil, e
	}
	reset := RuntimeState{State: "ABSENT", Phase: "restored_review_required", Epoch: epoch, PhaseStarted: time.Now(), SafetySuspended: true, SuspensionReason: "restored backup requires owner review", IdleBlockers: []string{}}
	b, _ := json.Marshal(reset)
	tx, e := s.DB.Begin()
	if e != nil {
		s.Close()
		return nil, e
	}
	for _, q := range []string{"DELETE FROM sessions", "DELETE FROM enrollment", "DELETE FROM dispatch", "DELETE FROM wait_state", "DELETE FROM wakes", "DELETE FROM push_deliveries", "DELETE FROM maintenance", "UPDATE schedules SET enabled=0"} {
		if _, e = tx.Exec(q); e != nil {
			tx.Rollback()
			s.Close()
			return nil, e
		}
	}
	_, e = tx.Exec("INSERT INTO settings(key,value) VALUES('lifecycle',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(b))
	if e == nil {
		e = tx.Commit()
	} else {
		tx.Rollback()
	}
	if e == nil {
		_, e = s.DB.Exec("PRAGMA wal_checkpoint(TRUNCATE)")
	}
	s.Close()
	if e != nil {
		return nil, e
	}
	marker, _ := json.Marshal(map[string]string{"deployment_epoch": epoch, "backup_created_at": manifest.CreatedAt, "maintenance_id": manifest.MaintenanceID})
	if e = os.WriteFile(filepath.Join(stage, "app", ".restore-required.json"), marker, 0600); e != nil {
		return nil, e
	}
	for _, area := range []string{"control", "app", "files", "browser"} {
		p := filepath.Join(stage, area)
		if e = os.MkdirAll(p, 0700); e != nil {
			return nil, e
		}
		uid := 31001
		if area == "control" {
			uid = int(d.Config.GatewayUID)
		}
		if area == "browser" {
			uid = 31003
		}
		if e = filepath.WalkDir(p, func(path string, v fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if v.Type()&os.ModeSymlink != 0 {
				return errors.New("restore staging contains a link")
			}
			mode := os.FileMode(0600)
			if v.IsDir() {
				mode = 0700
			}
			if e := os.Chmod(path, mode); e != nil {
				return e
			}
			return os.Chown(path, uid, uid)
		}); e != nil {
			return nil, e
		}
	}
	rollbackRoot := filepath.Join(stage, "previous")
	if e = os.Mkdir(rollbackRoot, 0700); e != nil {
		return nil, e
	}
	for _, area := range []string{"control", "app", "files", "browser"} {
		st, err := os.Lstat(filepath.Join(d.Config.DataRoot, area))
		if err != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("current restore roots must be real directories")
		}
	}
	journal, _ := json.Marshal(map[string]string{"stage": filepath.Base(stage), "deployment_epoch": epoch})
	if e = privateFile(journalPath, journal); e != nil {
		return nil, e
	}
	if e = syncDirectory(d.Config.DataRoot); e != nil {
		return nil, e
	}
	rollbackOnError := func(cause error) (map[string]any, error) {
		if rollbackErr := RecoverRestoreJournal(d.Config.DataRoot); rollbackErr != nil {
			return nil, errors.New("restore interrupted; rollback journal retained, gateway must stay stopped")
		}
		return nil, cause
	}
	for _, area := range []string{"control", "app", "files", "browser"} {
		destination := filepath.Join(d.Config.DataRoot, area)
		previous := filepath.Join(rollbackRoot, area)
		if _, statErr := os.Lstat(destination); statErr == nil {
			if e = os.Rename(destination, previous); e != nil {
				return rollbackOnError(e)
			}
		}
		if e = os.Rename(filepath.Join(stage, area), destination); e != nil {
			return rollbackOnError(e)
		}
		if e = syncDirectory(d.Config.DataRoot); e != nil {
			return rollbackOnError(e)
		}
	}
	if e = syncDirectory(d.Config.DataRoot); e != nil {
		return rollbackOnError(e)
	}
	if e = os.Remove(journalPath); e != nil {
		return rollbackOnError(e)
	}
	if e = syncDirectory(d.Config.DataRoot); e != nil {
		return nil, e
	}
	return map[string]any{"restored": true, "required_image": manifest.Image, "deployment_epoch": epoch, "safety_suspended": true, "notice": "未完成任务需核对；排程已暂停；记忆和删除状态回到备份时间。"}, nil
}

// RecoverRestoreJournal rolls back an interrupted multi-directory replacement.
// Only fixed children of a validated root-owned staging directory are touched.
func RecoverRestoreJournal(root string) error {
	p := filepath.Join(root, "restore-in-progress.json")
	b, e := os.ReadFile(p)
	if os.IsNotExist(e) {
		return nil
	}
	if e != nil {
		return e
	}
	var journal struct {
		Stage string `json:"stage"`
	}
	if e = json.Unmarshal(b, &journal); e != nil {
		return e
	}
	if filepath.Base(journal.Stage) != journal.Stage || !strings.HasPrefix(journal.Stage, ".restore-apply-") {
		return errors.New("invalid restore recovery staging path")
	}
	stage := filepath.Join(root, journal.Stage)
	st, e := os.Lstat(stage)
	if e != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
		return errors.New("restore staging is missing or unsafe")
	}
	for _, area := range []string{"browser", "files", "app", "control"} {
		previous := filepath.Join(stage, "previous", area)
		if st, e = os.Lstat(previous); os.IsNotExist(e) {
			continue
		} else if e != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
			return errors.New("unsafe restore previous directory")
		}
		target := filepath.Join(root, area)
		if _, e = os.Lstat(target); e == nil {
			if e = os.Rename(target, filepath.Join(stage, "interrupted-"+area)); e != nil {
				return e
			}
		} else if !os.IsNotExist(e) {
			return e
		}
		if e = os.Rename(previous, target); e != nil {
			return e
		}
		if e = syncDirectory(root); e != nil {
			return e
		}
	}
	if e = os.Remove(p); e != nil {
		return e
	}
	if e = syncDirectory(root); e != nil {
		return e
	}
	return safeRemoveStage(root, stage)
}

func (d *DockerManager) OfflineSnapshot(ctx context.Context, id string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !maintenanceIDPattern.MatchString(id) {
		return errors.New("invalid maintenance ID")
	}
	if v, e := d.inspect(ctx); e != nil || v.Exists {
		return errors.New("offline application snapshot requires no runtime")
	}
	app := filepath.Join(d.Config.DataRoot, "app")
	if e := validateOfflineSnapshotRoot(d.Config.DataRoot); e != nil {
		return e
	}
	if e := prepareOfflineSnapshotDirectory(app, false); e != nil {
		return e
	}
	parent := filepath.Join(app, "maintenance")
	if e := prepareOfflineSnapshotDirectory(parent, true); e != nil {
		return e
	}
	snapshotDir := filepath.Join(parent, id)
	if e := prepareOfflineSnapshotDirectory(snapshotDir, true); e != nil {
		return e
	}
	if e := prepareOfflineSnapshotFile(filepath.Join(app, "app.sqlite")); e != nil {
		return e
	}
	for _, suffix := range []string{"-wal", "-shm"} {
		p := filepath.Join(app, "app.sqlite"+suffix)
		if _, e := os.Lstat(p); os.IsNotExist(e) {
			continue
		} else if e != nil {
			return e
		}
		if e := prepareOfflineSnapshotFile(p); e != nil {
			return e
		}
	}
	if _, e := os.Lstat(filepath.Join(snapshotDir, "app.sqlite")); !os.IsNotExist(e) {
		return errors.New("offline snapshot destination must not already exist")
	}
	db, e := sql.Open("sqlite", filepath.Join(app, "app.sqlite"))
	if e != nil {
		return e
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	var check string
	if e = db.QueryRowContext(ctx, "PRAGMA integrity_check").Scan(&check); e != nil || check != "ok" {
		return errors.New("application database integrity check failed")
	}
	_, e = db.ExecContext(ctx, "VACUUM INTO ?", filepath.Join(snapshotDir, "app.sqlite"))
	if e != nil {
		return e
	}
	if e = db.Close(); e != nil {
		return e
	}
	if e = prepareOfflineSnapshotFile(filepath.Join(snapshotDir, "app.sqlite")); e != nil {
		return e
	}
	// SQLite may leave existing WAL/SHM files behind. Do not leave a root-only
	// sidecar which prevents the API role from reopening its own database.
	for _, suffix := range []string{"", "-wal", "-shm"} {
		p := filepath.Join(app, "app.sqlite"+suffix)
		if _, e = os.Lstat(p); os.IsNotExist(e) && suffix != "" {
			continue
		} else if e != nil {
			return e
		}
		if e = prepareOfflineSnapshotFile(p); e != nil {
			return e
		}
	}
	return nil
}
