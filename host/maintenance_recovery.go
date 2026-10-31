package host

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"time"
)

type maintenanceRecord struct {
	ID, RuntimeID, ContainerID, Epoch string
}

type backupAdmission struct {
	Observed  RuntimeState
	DailyDate string
}

func migrateMaintenance(db *sql.DB) error {
	rows, err := db.Query("PRAGMA table_info(maintenance)")
	if err != nil {
		return err
	}
	columns := make(map[string]bool)
	for rows.Next() {
		var index, notNull, primary int
		var name, kind string
		var fallback any
		if err = rows.Scan(&index, &name, &kind, &notNull, &fallback, &primary); err != nil {
			rows.Close()
			return err
		}
		columns[name] = true
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, column := range []struct{ name, definition string }{
		{"runtime_id", "TEXT NOT NULL DEFAULT ''"},
		{"container_id", "TEXT NOT NULL DEFAULT ''"},
		{"deployment_epoch", "TEXT NOT NULL DEFAULT ''"},
		{"cleanup_complete", "INTEGER NOT NULL DEFAULT 0"},
	} {
		if !columns[column.name] {
			if _, err = tx.Exec("ALTER TABLE maintenance ADD COLUMN " + column.name + " " + column.definition); err != nil {
				return err
			}
		}
	}
	if _, err = tx.Exec("DROP INDEX IF EXISTS maintenance_one_active"); err != nil {
		return err
	}
	if _, err = tx.Exec("CREATE UNIQUE INDEX maintenance_one_active ON maintenance((1)) WHERE stage IN ('drain','freeze','releasing')"); err != nil {
		return err
	}
	return tx.Commit()
}

// Persist the gate while holding lifecycle's transition lock. An idle remover or
// concurrent wake cannot pass between the fresh generation check and admission.
func (g *Gateway) claimBackup(ctx context.Context, admission *backupAdmission) (maintenanceRecord, error) {
	l := g.Lifecycle
	l.mu.Lock()
	defer l.mu.Unlock()
	r := maintenanceRecord{ID: newUUID(), RuntimeID: l.state.RuntimeID, ContainerID: l.state.ContainerID, Epoch: l.state.Epoch}
	if l.busy || (l.state.State != "READY" && l.state.State != "ABSENT") || l.state.DrainID != "" {
		return r, errors.New("runtime transition prevents maintenance admission")
	}
	if admission != nil && (l.state.SafetySuspended || !l.matchesSnapshotLocked(admission.Observed) || !l.state.LastActive.Equal(admission.Observed.LastActive) || l.state.ActivityRevision != admission.Observed.ActivityRevision) {
		return r, errors.New("automatic backup observation is no longer current")
	}
	tx, err := g.Store.DB.BeginTx(ctx, nil)
	if err != nil {
		return r, err
	}
	defer tx.Rollback()
	if admission != nil {
		var last string
		err = tx.QueryRow("SELECT value FROM settings WHERE key='daily_backup_attempt'").Scan(&last)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return r, err
		}
		if last == admission.DailyDate {
			return r, errors.New("daily backup already attempted")
		}
		if _, err = tx.Exec("INSERT INTO settings(key,value) VALUES('daily_backup_attempt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", admission.DailyDate); err != nil {
			return r, err
		}
	}
	if _, err = tx.Exec("INSERT INTO maintenance(id,stage,created_at,runtime_id,container_id,deployment_epoch) VALUES(?,'drain',?,?,?,?)", r.ID, timestamp(time.Now()), r.RuntimeID, r.ContainerID, r.Epoch); err != nil {
		return r, errors.New("maintenance already active or admission could not be persisted")
	}
	return r, tx.Commit()
}

// RecoverMaintenance never creates a runtime. Active records survive process
// failure, including the old implementation's failed-before-release ordering.
func (g *Gateway) RecoverMaintenance(ctx context.Context) error {
	if !g.backupMu.TryLock() {
		return nil
	}
	defer g.backupMu.Unlock()
	var r maintenanceRecord
	err := g.Store.DB.QueryRow("SELECT id,runtime_id,container_id,deployment_epoch FROM maintenance WHERE stage IN ('drain','freeze','releasing') LIMIT 1").Scan(&r.ID, &r.RuntimeID, &r.ContainerID, &r.Epoch)
	if errors.Is(err, sql.ErrNoRows) {
		var stage string
		var complete int
		err = g.Store.DB.QueryRow("SELECT id,runtime_id,container_id,deployment_epoch,stage,cleanup_complete FROM maintenance ORDER BY created_at DESC,rowid DESC LIMIT 1").Scan(&r.ID, &r.RuntimeID, &r.ContainerID, &r.Epoch, &stage, &complete)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if stage != "failed" || complete != 0 {
			return nil
		}
	} else if err != nil {
		return err
	}
	if _, err = g.Store.DB.Exec("UPDATE maintenance SET stage='releasing',error='maintenance release pending' WHERE id=?", r.ID); err != nil {
		return err
	}
	return g.releaseMaintenance(ctx, r)
}

func (g *Gateway) releaseMaintenance(ctx context.Context, r maintenanceRecord) error {
	instance, err := g.Lifecycle.Manager.Inspect(ctx)
	if err != nil {
		return fmt.Errorf("maintenance release requires verified runtime inspection: %w", err)
	}
	if instance.Exists && r.Epoch == "" {
		// Upgrade repair: older rows did not record generation identity. Only
		// the helper's project-scoped instance in this deployment may be bound.
		if instance.Epoch != g.Store.Secrets.Epoch || instance.RuntimeID == "" || instance.ContainerID == "" {
			return errors.New("legacy maintenance runtime identity could not be verified")
		}
		r.RuntimeID, r.ContainerID, r.Epoch = instance.RuntimeID, instance.ContainerID, instance.Epoch
		if _, err = g.Store.DB.Exec("UPDATE maintenance SET runtime_id=?,container_id=?,deployment_epoch=? WHERE id=?", r.RuntimeID, r.ContainerID, r.Epoch, r.ID); err != nil {
			return err
		}
	}
	if instance.Exists && instance.RuntimeID == r.RuntimeID && instance.ContainerID == r.ContainerID && instance.Epoch == r.Epoch {
		if r.Epoch != g.Store.Secrets.Epoch {
			return errors.New("maintenance release deployment epoch mismatch")
		}
		err = g.releaseRuntimeMaintenance(ctx, r)
		var conflict *RuntimeHTTPError
		if errors.As(err, &conflict) && conflict.Status == http.StatusConflict {
			// The previous version could admit B after failing to release A.
			// Only known, unconfirmed legacy UUIDs are candidates; the runtime
			// remains authoritative about which UUID actually owns its marker.
			if repaired, repairErr := g.releaseLegacyConflict(ctx, r); repairErr != nil {
				return repairErr
			} else if repaired {
				err = g.releaseRuntimeMaintenance(ctx, r)
			}
		}
		if err != nil {
			return fmt.Errorf("maintenance release remains pending: %w", err)
		}
	}
	// A different exact project identity proves this physical generation ended;
	// never send its release to a successor or reopen a one-way intake gate.
	_, err = g.Store.DB.Exec("UPDATE maintenance SET stage='failed',cleanup_complete=1,error='maintenance did not finish; cleanup confirmed' WHERE id=?", r.ID)
	return err
}

func (g *Gateway) releaseRuntimeMaintenance(ctx context.Context, r maintenanceRecord) error {
	var response struct {
		Released bool `json:"released"`
	}
	if err := g.Lifecycle.Runtime.callForRuntime(ctx, "POST", "/internal/maintenance/stage", map[string]string{"maintenance_id": r.ID, "stage": "release"}, &response, r.RuntimeID); err != nil {
		return err
	}
	if !response.Released {
		return errors.New("maintenance release was not acknowledged")
	}
	return nil
}

func (g *Gateway) releaseLegacyConflict(ctx context.Context, current maintenanceRecord) (bool, error) {
	rows, err := g.Store.DB.QueryContext(ctx, "SELECT id FROM maintenance WHERE stage='failed' AND cleanup_complete=0 AND deployment_epoch='' AND id<>? ORDER BY created_at,rowid", current.ID)
	if err != nil {
		return false, err
	}
	var candidates []string
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return false, err
		}
		candidates = append(candidates, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return false, err
	}
	for _, id := range candidates {
		if !maintenanceIDPattern.MatchString(id) {
			continue
		}
		candidate := current
		candidate.ID = id
		if err = g.releaseRuntimeMaintenance(ctx, candidate); err != nil {
			var conflict *RuntimeHTTPError
			if errors.As(err, &conflict) && conflict.Status == http.StatusConflict {
				continue
			}
			return false, err
		}
		if _, err = g.Store.DB.ExecContext(ctx, "UPDATE maintenance SET runtime_id=?,container_id=?,deployment_epoch=?,cleanup_complete=1,error='legacy maintenance release confirmed' WHERE id=? AND stage='failed' AND cleanup_complete=0 AND deployment_epoch=''", current.RuntimeID, current.ContainerID, current.Epoch, id); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}
