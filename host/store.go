package host

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	_ "modernc.org/sqlite"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Store struct {
	DB      *sql.DB
	Secrets Secrets
	Dir     string
	lock    *os.File
}

func OpenStore(dir string) (*Store, error) {
	if e := os.MkdirAll(dir, 0700); e != nil {
		return nil, e
	}
	lock, e := acquireStoreLock(dir)
	if e != nil {
		return nil, e
	}
	sec, e := LoadSecrets(dir)
	if e != nil {
		releaseStoreLock(lock)
		return nil, e
	}
	p := filepath.Join(dir, "control.sqlite")
	db, e := sql.Open("sqlite", p)
	if e != nil {
		releaseStoreLock(lock)
		return nil, e
	}
	db.SetMaxOpenConns(1)
	s := &Store{DB: db, Secrets: sec, Dir: dir, lock: lock}
	fail := func(err error) (*Store, error) { db.Close(); releaseStoreLock(lock); return nil, err }
	var version string
	if e = db.QueryRow("SELECT sqlite_version()").Scan(&version); e != nil {
		return fail(e)
	}
	v := strings.Split(version, ".")
	if len(v) < 3 {
		return fail(errors.New("invalid SQLite version"))
	}
	major, _ := strconv.Atoi(v[0])
	minor, _ := strconv.Atoi(v[1])
	patch, _ := strconv.Atoi(v[2])
	if major < 3 || (major == 3 && (minor < 51 || (minor == 51 && patch < 3))) {
		return fail(fmt.Errorf("SQLite >=3.51.3 required; linked version %s", version))
	}
	for _, q := range []string{"PRAGMA journal_mode=WAL", "PRAGMA synchronous=FULL", "PRAGMA foreign_keys=ON", "PRAGMA busy_timeout=5000", schema} {
		if _, e = db.Exec(q); e != nil {
			return fail(e)
		}
	}
	if e = migrateMaintenance(db); e != nil {
		return fail(e)
	}
	if e = os.Chmod(p, 0600); e != nil {
		return fail(e)
	}
	if _, e = db.Exec("INSERT OR IGNORE INTO runtime(id,state,phase) VALUES(1,'ABSENT','sleeping')"); e != nil {
		return fail(e)
	}
	return s, nil
}

const schema = `
CREATE TABLE IF NOT EXISTS owner(id TEXT PRIMARY KEY, singleton INTEGER UNIQUE NOT NULL DEFAULT 1 CHECK(singleton=1), username TEXT NOT NULL UNIQUE,password TEXT NOT NULL,totp BLOB NOT NULL,last_totp_step INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS enrollment(id TEXT PRIMARY KEY, username TEXT NOT NULL,totp BLOB NOT NULL,expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_codes(hash TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owner(id));
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,owner_id TEXT NOT NULL REFERENCES owner(id),csrf TEXT NOT NULL,created_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,expires INTEGER NOT NULL,user_agent TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_limits(key TEXT PRIMARY KEY,count INTEGER NOT NULL,reset_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runtime(id INTEGER PRIMARY KEY CHECK(id=1),state TEXT NOT NULL,phase TEXT NOT NULL,runtime_id TEXT,container_id TEXT,recovery_required INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,recovery_round TEXT,error TEXT,diagnostic_id TEXT,stable_since INTEGER);
CREATE TABLE IF NOT EXISTS bridge_events(id TEXT PRIMARY KEY,kind TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,enabled INTEGER NOT NULL,cron TEXT NOT NULL,timezone TEXT NOT NULL,next_fire INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS wakes(task_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,resume_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload BLOB NOT NULL,due_at INTEGER NOT NULL,acked INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,category TEXT NOT NULL,created_at TEXT NOT NULL,read_at TEXT);
CREATE TABLE IF NOT EXISTS push_subscriptions(id TEXT PRIMARY KEY,endpoint_hash TEXT UNIQUE NOT NULL,subscription BLOB NOT NULL,created_at TEXT NOT NULL,disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS push_deliveries(notification_id TEXT NOT NULL REFERENCES notifications(id),subscription_id TEXT NOT NULL REFERENCES push_subscriptions(id),state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(notification_id,subscription_id));
CREATE TABLE IF NOT EXISTS maintenance(id TEXT PRIMARY KEY,stage TEXT NOT NULL,created_at TEXT NOT NULL,error TEXT,runtime_id TEXT NOT NULL DEFAULT '',container_id TEXT NOT NULL DEFAULT '',deployment_epoch TEXT NOT NULL DEFAULT '',cleanup_complete INTEGER NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS maintenance_one_active ON maintenance((1)) WHERE stage IN ('drain','freeze','releasing');
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,subject TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_receipts(event_id TEXT PRIMARY KEY,payload_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS wait_state(task_id TEXT PRIMARY KEY,generation INTEGER NOT NULL,connection_revision INTEGER NOT NULL,enabled INTEGER NOT NULL,resume_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lifecycle_proofs(runtime_id TEXT PRIMARY KEY,drain_id TEXT NOT NULL,proof BLOB NOT NULL,created_at TEXT NOT NULL);
`

func (s *Store) Initialized() bool {
	var n int
	return s.DB.QueryRow("SELECT count(*) FROM owner").Scan(&n) == nil && n == 1
}
func (s *Store) Close() error { e := s.DB.Close(); releaseStoreLock(s.lock); s.lock = nil; return e }
func (s *Store) Audit(kind, subject string) error {
	_, e := s.DB.Exec("INSERT INTO audit(kind,subject,created_at) VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))", kind, subject)
	return e
}
func (s *Store) Frozen() bool {
	var n int
	return s.DB.QueryRow("SELECT count(*) FROM maintenance WHERE stage IN ('drain','freeze','releasing')").Scan(&n) != nil || n > 0
}
func (s *Store) Snapshot(ctx context.Context, path string) error {
	_, e := s.DB.ExecContext(ctx, "VACUUM INTO ?", path)
	return e
}
func (s *Store) RuntimeFaultNotice(round string) error {
	tx, e := s.DB.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	id := "runtime_faulted_" + round
	if _, e = tx.Exec("INSERT OR IGNORE INTO notifications(id,task_id,category,created_at) VALUES(?,'','runtime_faulted',?)", id, timestamp(time.Now())); e != nil {
		return e
	}
	if _, e = tx.Exec("INSERT OR IGNORE INTO push_deliveries(notification_id,subscription_id) SELECT ?,id FROM push_subscriptions WHERE disabled=0", id); e != nil {
		return e
	}
	return tx.Commit()
}
