package host

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/robfig/cron/v3"
	"net/http"
	"time"
)

type BridgeEvent struct {
	ID        string          `json:"event_id"`
	Kind      string          `json:"kind"`
	Payload   json.RawMessage `json:"payload"`
	RuntimeID string          `json:"runtime_id"`
	Epoch     string          `json:"deployment_epoch"`
}

var cronParser = cron.NewParser(cron.Minute | cron.Hour | cron.Dom | cron.Month | cron.Dow)

func parseSchedule(expr, zone string) (cron.Schedule, error) {
	loc, e := time.LoadLocation(zone)
	if e != nil {
		return nil, errors.New("invalid IANA timezone")
	}
	v, e := cronParser.Parse(expr)
	if e != nil {
		return nil, errors.New("invalid five-field cron")
	}
	spec, ok := v.(*cron.SpecSchedule)
	if !ok {
		return nil, errors.New("unsupported schedule")
	}
	spec.Location = loc
	if spec.Next(time.Now()).IsZero() {
		return nil, errors.New("schedule has no future occurrence")
	}
	return spec, nil
}

func (s *Store) IngestEvent(v BridgeEvent) error {
	if len(v.ID) < 1 || len(v.ID) > 160 {
		return errors.New("invalid event ID")
	}
	var payload any
	if e := json.Unmarshal(v.Payload, &payload); e != nil {
		return e
	}
	canonical, e := json.Marshal(map[string]any{"kind": v.Kind, "payload": payload, "deployment_epoch": v.Epoch})
	if e != nil {
		return e
	}
	hash := digest(string(canonical))
	tx, e := s.DB.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	var previous string
	e = tx.QueryRow("SELECT payload_digest FROM event_receipts WHERE event_id=?", v.ID).Scan(&previous)
	if e == nil {
		if previous != hash {
			return errors.New("event ID content conflict")
		}
		return tx.Commit()
	}
	if !errors.Is(e, sql.ErrNoRows) {
		return e
	}
	var frozen int
	if e = tx.QueryRow("SELECT count(*) FROM maintenance WHERE stage='freeze'").Scan(&frozen); e != nil {
		return e
	}
	if frozen > 0 {
		return errors.New("maintenance writes are frozen")
	}
	switch v.Kind {
	case "schedule.upsert", "schedule.disable":
		var p struct {
			ID       string `json:"schedule_id"`
			Revision int64  `json:"revision"`
			Enabled  bool   `json:"enabled"`
			Cron     string `json:"cron"`
			Timezone string `json:"timezone"`
			Next     string `json:"next_fire_at"`
		}
		if e = json.Unmarshal(v.Payload, &p); e != nil {
			return e
		}
		if p.ID == "" || p.Revision < 1 {
			return errors.New("invalid schedule revision")
		}
		if v.Kind == "schedule.disable" {
			p.Enabled = false
		}
		if p.Timezone == "" {
			p.Timezone = "Asia/Shanghai"
		}
		next := int64(0)
		if p.Enabled {
			sch, err := parseSchedule(p.Cron, p.Timezone)
			if err != nil {
				return err
			}
			t, err := time.Parse(time.RFC3339Nano, p.Next)
			if err != nil {
				t = sch.Next(time.Now())
			}
			next = t.Unix()
		}
		_, e = tx.Exec("INSERT INTO schedules(id,revision,enabled,cron,timezone,next_fire) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled,cron=excluded.cron,timezone=excluded.timezone,next_fire=excluded.next_fire WHERE excluded.revision>schedules.revision", p.ID, p.Revision, p.Enabled, p.Cron, p.Timezone, next)
		if e == nil {
			_, e = tx.Exec("UPDATE dispatch SET acked=1 WHERE kind='schedule' AND json_extract(payload,'$.schedule_id')=? AND json_extract(payload,'$.revision')<?", p.ID, p.Revision)
		}
	case "model.wait.upsert":
		var p struct {
			Task               string  `json:"task_id"`
			Generation         int64   `json:"wait_generation"`
			ConnectionRevision int64   `json:"connection_revision"`
			Enabled            bool    `json:"enabled"`
			Resume             *string `json:"resume_at"`
		}
		if e = json.Unmarshal(v.Payload, &p); e != nil {
			return e
		}
		if p.Task == "" || p.Generation < 1 {
			return errors.New("invalid model wait")
		}
		resume := int64(0)
		if p.Enabled {
			if p.Resume == nil {
				return errors.New("enabled model wait requires a trusted time")
			}
			t, err := time.Parse(time.RFC3339Nano, *p.Resume)
			if err != nil {
				return errors.New("invalid model resume time")
			}
			resume = t.Unix()
		}
		_, e = tx.Exec("INSERT INTO wait_state(task_id,generation,connection_revision,enabled,resume_at) VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET generation=excluded.generation,connection_revision=excluded.connection_revision,enabled=excluded.enabled,resume_at=excluded.resume_at WHERE excluded.generation>wait_state.generation", p.Task, p.Generation, p.ConnectionRevision, p.Enabled, resume)
		if e == nil {
			_, e = tx.Exec("UPDATE dispatch SET acked=1 WHERE kind='model.wake' AND json_extract(payload,'$.task_id')=? AND json_extract(payload,'$.wait_generation')<?", p.Task, p.Generation)
		}
	case "notification":
		var p struct {
			ID       string `json:"notification_id"`
			Task     string `json:"task_id"`
			Category string `json:"category"`
		}
		if e = json.Unmarshal(v.Payload, &p); e != nil {
			return e
		}
		if p.ID == "" || p.Task == "" {
			return errors.New("invalid notification")
		}
		allowed := map[string]bool{"completed": true, "failed": true, "approval": true, "human": true, "succeeded": true, "needs_review": true, "task.completed": true, "task.failed": true, "waiting_approval": true, "waiting_human": true}
		if !allowed[p.Category] {
			return errors.New("invalid notification category")
		}
		_, e = tx.Exec("INSERT OR IGNORE INTO notifications(id,task_id,category,created_at) VALUES(?,?,?,?)", p.ID, p.Task, p.Category, timestamp(time.Now()))
		if e == nil {
			_, e = tx.Exec("INSERT OR IGNORE INTO push_deliveries(notification_id,subscription_id) SELECT ?,id FROM push_subscriptions WHERE disabled=0", p.ID)
		}
	default:
		return errors.New("unsupported control event")
	}
	if e != nil {
		return e
	}
	if _, e = tx.Exec("INSERT INTO event_receipts(event_id,payload_digest) VALUES(?,?)", v.ID, hash); e != nil {
		return e
	}
	return tx.Commit()
}

func (l *Lifecycle) BridgeHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		uid, ok := peerUID(r.Context())
		if !ok || uid != l.Config.APIPeerUID {
			writeError(w, 403, "peer_forbidden", "API peer credential required", false)
			return
		}
		if r.Method != "POST" || r.URL.Path != "/internal/events" {
			writeError(w, 404, "not_found", "unknown bridge route", false)
			return
		}
		var v BridgeEvent
		if !decodeJSON(w, r, &v) {
			return
		}
		state := l.Snapshot()
		if v.RuntimeID != state.RuntimeID || v.RuntimeID == "" || v.Epoch != l.Store.Secrets.Epoch {
			writeError(w, 409, "stale_runtime", "runtime identity is no longer current", false)
			return
		}
		if e := l.Store.IngestEvent(v); e != nil {
			writeError(w, 409, "event_rejected", e.Error(), true)
			return
		}
		writeJSON(w, 200, map[string]any{"accepted": true, "event_id": v.ID})
	})
}

func isFirstFold(t time.Time) bool {
	start, _ := t.ZoneBounds()
	if start.IsZero() {
		return true
	}
	_, current := t.Zone()
	_, previous := start.Add(-time.Nanosecond).Zone()
	if previous <= current {
		return true
	}
	earlier := t.Add(-time.Duration(previous-current) * time.Second)
	return earlier.Format("2006-01-02 15:04") != t.Format("2006-01-02 15:04")
}
func nextOccurrence(s cron.Schedule, after time.Time) time.Time {
	for i := 0; i < 150; i++ {
		n := s.Next(after)
		if n.IsZero() {
			return n
		}
		local := n
		if spec, ok := s.(*cron.SpecSchedule); ok {
			local = n.In(spec.Location)
		}
		if isFirstFold(local) {
			return n
		}
		after = n
	}
	return time.Time{}
}
func latestOccurrence(s cron.Schedule, lower, now time.Time) time.Time {
	spec, ok := s.(*cron.SpecSchedule)
	if !ok {
		return lower
	}
	t := now.UTC().Truncate(time.Minute)
	for i := 0; i < 527040*5 && !t.Before(lower); i++ {
		local := t.In(spec.Location)
		minute, hour, day, month, dow := local.Minute(), local.Hour(), local.Day(), int(local.Month()), int(local.Weekday())
		domOK := spec.Dom&(1<<uint(day)) != 0
		dowOK := spec.Dow&(1<<uint(dow)) != 0
		days := domOK || dowOK
		if spec.Dom&(1<<63) != 0 || spec.Dow&(1<<63) != 0 {
			days = domOK && dowOK
		}
		if spec.Minute&(1<<uint(minute)) != 0 && spec.Hour&(1<<uint(hour)) != 0 && spec.Month&(1<<uint(month)) != 0 && days && isFirstFold(local) {
			return t
		}
		t = t.Add(-time.Minute)
	}
	return lower
}

func (s *Store) EnqueueDue(now time.Time) error {
	tx, e := s.DB.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	var n int
	if e = tx.QueryRow("SELECT count(*) FROM maintenance WHERE stage IN ('drain','freeze')").Scan(&n); e != nil {
		return e
	}
	if n > 0 {
		return nil
	}
	rows, e := tx.Query("SELECT id,revision,cron,timezone,next_fire FROM schedules WHERE enabled=1 AND next_fire<=?", now.Unix())
	if e != nil {
		return e
	}
	type due struct {
		id         string
		rev        int64
		expr, zone string
		next       int64
	}
	var items []due
	for rows.Next() {
		var v due
		if e = rows.Scan(&v.id, &v.rev, &v.expr, &v.zone, &v.next); e != nil {
			rows.Close()
			return e
		}
		items = append(items, v)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return e
	}
	for _, v := range items {
		sch, err := parseSchedule(v.expr, v.zone)
		if err != nil {
			return err
		}
		fire := latestOccurrence(sch, time.Unix(v.next, 0), now)
		next := nextOccurrence(sch, now)
		if next.IsZero() {
			return errors.New("cannot determine next schedule occurrence")
		}
		id := "schedule_" + digest(s.Secrets.Epoch+"|"+v.id+"|"+fmt.Sprint(v.rev)+"|"+timestamp(fire))
		p, _ := json.Marshal(map[string]any{"schedule_id": v.id, "revision": v.rev, "scheduled_at": timestamp(fire)})
		if _, e = tx.Exec("UPDATE dispatch SET acked=1 WHERE kind='schedule' AND acked=0 AND json_extract(payload,'$.schedule_id')=?", v.id); e != nil {
			return e
		}
		if _, e = tx.Exec("INSERT OR IGNORE INTO dispatch(id,kind,payload,due_at) VALUES(?,'schedule',?,?)", id, p, fire.Unix()); e != nil {
			return e
		}
		if _, e = tx.Exec("UPDATE schedules SET next_fire=? WHERE id=? AND revision=?", next.Unix(), v.id, v.rev); e != nil {
			return e
		}
	}
	rows, e = tx.Query("SELECT task_id,generation,connection_revision,resume_at FROM wait_state WHERE enabled=1 AND resume_at<=?", now.Unix())
	if e != nil {
		return e
	}
	type wait struct {
		id           string
		gen, rev, at int64
	}
	var waits []wait
	for rows.Next() {
		var v wait
		if e = rows.Scan(&v.id, &v.gen, &v.rev, &v.at); e != nil {
			rows.Close()
			return e
		}
		waits = append(waits, v)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return e
	}
	for _, v := range waits {
		id := "wake_" + digest(s.Secrets.Epoch+"|"+v.id+"|"+fmt.Sprint(v.gen))
		p, _ := json.Marshal(map[string]any{"task_id": v.id, "wait_generation": v.gen, "connection_revision": v.rev})
		if _, e = tx.Exec("INSERT OR IGNORE INTO dispatch(id,kind,payload,due_at) VALUES(?,'model.wake',?,?)", id, p, v.at); e != nil {
			return e
		}
		if _, e = tx.Exec("UPDATE wait_state SET enabled=0 WHERE task_id=? AND generation=?", v.id, v.gen); e != nil {
			return e
		}
	}
	return tx.Commit()
}

func (l *Lifecycle) DispatchDue(ctx context.Context) error {
	s := l.Snapshot()
	if s.SafetySuspended || s.State == "FAULTED" || l.Store.Frozen() {
		return nil
	}
	if e := l.Store.EnqueueDue(time.Now()); e != nil {
		return e
	}
	rows, e := l.Store.DB.Query("SELECT id,kind,payload,attempts FROM dispatch WHERE acked=0 AND next_attempt<=? ORDER BY due_at LIMIT 20", time.Now().Unix())
	if e != nil {
		return e
	}
	type event struct {
		id, kind string
		payload  []byte
		attempts int
	}
	var items []event
	for rows.Next() {
		var v event
		if e = rows.Scan(&v.id, &v.kind, &v.payload, &v.attempts); e != nil {
			rows.Close()
			return e
		}
		items = append(items, v)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return e
	}
	if len(items) == 0 {
		return nil
	}
	if e = l.EnsureReady(ctx); e != nil {
		return e
	}
	for _, v := range items {
		if l.Snapshot().SafetySuspended || l.Store.Frozen() {
			return nil
		}
		var pending int
		if e = l.Store.DB.QueryRow("SELECT count(*) FROM dispatch WHERE id=? AND acked=0", v.id).Scan(&pending); e != nil {
			return e
		}
		if pending == 0 {
			continue
		}
		var p any
		if e = json.Unmarshal(v.payload, &p); e != nil {
			return e
		}
		var ack struct {
			Accepted bool   `json:"accepted"`
			ID       string `json:"event_id"`
		}
		e = l.Runtime.Call(ctx, "POST", "/internal/dispatch", map[string]any{"event_id": v.id, "kind": v.kind, "payload": p}, &ack)
		if e == nil && ack.Accepted && ack.ID == v.id {
			_, e = l.Store.DB.Exec("UPDATE dispatch SET acked=1 WHERE id=?", v.id)
		} else {
			delay := time.Duration(1<<min(v.attempts, 8)) * time.Second
			if delay > 5*time.Minute {
				delay = 5 * time.Minute
			}
			_, e = l.Store.DB.Exec("UPDATE dispatch SET attempts=attempts+1,next_attempt=? WHERE id=?", time.Now().Add(delay).Unix(), v.id)
		}
		if e != nil {
			return e
		}
	}
	return nil
}
