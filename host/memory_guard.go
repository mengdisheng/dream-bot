package host

import (
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Only anon and shmem are credited: adding memory.current would count
// reclaimable file cache twice because MemAvailable already includes it.
type hostMemorySnapshot struct {
	AvailableBytes       int64
	ProjectResidentBytes int64
	OOM                  uint64
	OOMKills             uint64
}

type hostMemoryReader interface {
	Read() (hostMemorySnapshot, error)
}

type memoryFSReader struct {
	slice    string
	readFile func(string) ([]byte, error)
}

var memorySliceName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:-]*\.slice$`)

func memoryGuardEnabled(c Config) bool {
	return c.HostReserveBytes != 0 || c.ProjectPeakBytes != 0 || c.ProjectSlice != ""
}

func validateMemoryGuard(c Config) error {
	if !memoryGuardEnabled(c) {
		return nil
	}
	if c.HostReserveBytes <= 0 || c.ProjectPeakBytes <= 0 || c.ProjectPeakBytes > math.MaxInt64-c.HostReserveBytes || len(c.ProjectSlice) > 255 || !memorySliceName.MatchString(c.ProjectSlice) {
		return errors.New("memory guard requires positive host_reserve_bytes/project_peak_bytes and a fixed slice unit name")
	}
	return nil
}

func readMemoryFile(name string) ([]byte, error) {
	if runtime.GOOS != "linux" {
		return nil, errors.New("host memory guard requires Linux cgroup v2")
	}
	f, e := os.Open(name)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, 256*1024+1))
	if e != nil {
		return nil, e
	}
	if len(b) > 256*1024 {
		return nil, errors.New("memory metric file exceeds limit")
	}
	return b, nil
}

func metricValues(data []byte) (map[string]uint64, error) {
	values := make(map[string]uint64)
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 {
			return nil, errors.New("invalid cgroup memory metric")
		}
		if _, exists := values[fields[0]]; exists {
			return nil, errors.New("duplicate cgroup memory metric")
		}
		number, e := strconv.ParseUint(fields[1], 10, 64)
		if e != nil {
			return nil, errors.New("invalid cgroup memory value")
		}
		values[fields[0]] = number
	}
	return values, nil
}

func (r memoryFSReader) Read() (hostMemorySnapshot, error) {
	var snapshot hostMemorySnapshot
	read := r.readFile
	if read == nil {
		read = readMemoryFile
	}
	data, e := read("/proc/meminfo")
	if e != nil {
		return snapshot, e
	}
	found := false
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 || fields[0] != "MemAvailable:" {
			continue
		}
		if found || len(fields) != 3 || fields[2] != "kB" {
			return snapshot, errors.New("invalid MemAvailable metric")
		}
		number, err := strconv.ParseUint(fields[1], 10, 63)
		if err != nil || number > math.MaxInt64/1024 {
			return snapshot, errors.New("invalid MemAvailable value")
		}
		snapshot.AvailableBytes, found = int64(number)*1024, true
	}
	if !found {
		return snapshot, errors.New("MemAvailable metric missing")
	}
	data, e = read("/proc/self/cgroup")
	if e != nil {
		return snapshot, e
	}
	var group string
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if !strings.HasPrefix(line, "0::") {
			continue
		}
		if group != "" {
			return snapshot, errors.New("ambiguous unified cgroup hierarchy")
		}
		group = strings.TrimPrefix(line, "0::")
	}
	if group == "" || !strings.HasPrefix(group, "/") || path.Clean(group) != group || !memorySliceName.MatchString(r.slice) {
		return snapshot, errors.New("invalid unified cgroup hierarchy")
	}
	parts := strings.Split(strings.TrimPrefix(group, "/"), "/")
	index := -1
	for i, part := range parts {
		if part == r.slice {
			if index != -1 {
				return snapshot, errors.New("ambiguous project cgroup ancestor")
			}
			index = i
		}
	}
	if index < 0 {
		return snapshot, errors.New("project slice is not a gateway cgroup ancestor")
	}
	base := "/sys/fs/cgroup/" + strings.Join(parts[:index+1], "/")
	data, e = read(base + "/memory.stat")
	if e != nil {
		return snapshot, e
	}
	values, e := metricValues(data)
	if e != nil {
		return snapshot, e
	}
	anon, hasAnon := values["anon"]
	shmem, hasShmem := values["shmem"]
	if !hasAnon || !hasShmem || anon > math.MaxInt64 || shmem > math.MaxInt64-anon {
		return snapshot, errors.New("project anon/shmem metrics missing or invalid")
	}
	snapshot.ProjectResidentBytes = int64(anon + shmem)
	data, e = read(base + "/memory.events")
	if e != nil {
		return snapshot, e
	}
	values, e = metricValues(data)
	if e != nil {
		return snapshot, e
	}
	var hasOOM, hasKills bool
	snapshot.OOM, hasOOM = values["oom"]
	snapshot.OOMKills, hasKills = values["oom_kill"]
	if !hasOOM || !hasKills {
		return snapshot, errors.New("project OOM metrics missing")
	}
	return snapshot, nil
}

func (l *Lifecycle) memoryMetrics() (hostMemorySnapshot, error) {
	reader := l.memoryReader
	if reader == nil {
		reader = memoryFSReader{slice: l.Config.ProjectSlice}
	}
	return reader.Read()
}

// Called with mu held. The first observation is a baseline, never a new OOM.
func (l *Lifecycle) observeOOMLocked(snapshot hostMemorySnapshot) bool {
	increased := l.memoryBaseline && (snapshot.OOM > l.lastOOM || snapshot.OOMKills > l.lastOOMKills)
	l.memoryBaseline, l.lastOOM, l.lastOOMKills = true, snapshot.OOM, snapshot.OOMKills
	return increased
}

// The caller owns the start transition and holds mu, so accepting an OOM
// observation and blocking admission cannot race another runtime generation.
func (l *Lifecycle) admissionMemoryReasonLocked(snapshot hostMemorySnapshot, e error) string {
	if !memoryGuardEnabled(l.Config) {
		return ""
	}
	if e != nil {
		return "无法读取主机内存保护指标，Bot 已安全暂停；修复后请由管理员恢复"
	}
	newOOM := l.observeOOMLocked(snapshot)
	if newOOM {
		return "项目发生新的 OOM，Bot 已安全暂停；检查资源后请由管理员恢复"
	}
	required := l.Config.ProjectPeakBytes + l.Config.HostReserveBytes
	if snapshot.ProjectResidentBytes < required && snapshot.AvailableBytes < required-snapshot.ProjectResidentBytes {
		return "主机内存无法满足项目峰值预算和保留余量，Bot 已安全暂停；释放资源后请由管理员恢复"
	}
	return ""
}

// The create path owns the start transition. It cannot call Suspend, which
// waits for that same start goroutine; no runtime has been created here.
func (l *Lifecycle) blockMemoryAdmissionLocked(reason string) {
	l.busy = false
	l.state.SafetySuspended, l.state.SuspensionReason = true, reason
	l.state.RuntimeID, l.state.ContainerID = "", ""
	l.state.IdleSince = time.Time{}
	l.state.Error, l.state.DiagnosticID = reason, randomID()
	l.setPhaseLocked("ABSENT", "memory_blocked")
	if e := l.persistMemorySuspensionLocked(); e != nil {
		// Failed persistence never grants permission to create. Retain the
		// in-memory suspension too; a restart must repeat admission checks.
		l.state.Error = "无法持久保存内存安全暂停；Bot 保持关闭，请修复控制存储"
		l.setPhaseLocked("FAULTED", "memory_suspension_failed")
	}
}

func (l *Lifecycle) resetMemoryPressureLocked() {
	l.lowMemorySamples, l.lowMemorySince = 0, time.Time{}
}

// Memory trips fail closed even if the control database is temporarily
// unavailable. An audit failure must not roll back a safety decision. Saving
// the pause separately preserves it across restart when only audit is broken;
// a later paused Tick retries the complete transaction before doing anything
// except cleanup. If all storage writes fail, keep the in-memory pause too.
func (l *Lifecycle) persistMemorySuspensionLocked() error {
	l.state.MemoryGuardAuditPending = false
	if e := l.persistAuditLocked("safety.memory_guard", l.state.SuspensionReason); e == nil {
		return nil
	}
	l.state.MemoryGuardAuditPending = true
	return l.persistLocked()
}

func (l *Lifecycle) retrySafetySuspension(observed RuntimeState) {
	l.mu.Lock()
	if l.busy || !l.state.SafetySuspended || !l.matchesSnapshotLocked(observed) {
		l.mu.Unlock()
		return
	}
	if l.state.MemoryGuardAuditPending {
		_ = l.persistMemorySuspensionLocked()
	}
	if l.state.RuntimeID == "" {
		l.mu.Unlock()
		return
	}
	cancel, done := l.startCancel, l.startDone
	l.mu.Unlock()
	_ = l.finishSuspension(cancel, done)
}

func (l *Lifecycle) runningMemorySafe(observed RuntimeState) bool {
	if !memoryGuardEnabled(l.Config) {
		return true
	}
	snapshot, e := l.memoryMetrics()
	l.mu.Lock()
	if l.busy || l.state.SafetySuspended || !l.matchesSnapshotLocked(observed) {
		l.mu.Unlock()
		return false
	}
	reason := ""
	if e != nil {
		reason = "无法读取主机内存保护指标，Bot 已安全暂停；修复后请由管理员恢复"
	} else if l.observeOOMLocked(snapshot) {
		reason = "项目发生新的 OOM，Bot 已安全暂停；检查资源后请由管理员恢复"
	} else if snapshot.AvailableBytes < l.Config.HostReserveBytes {
		now := time.Now()
		if l.memoryClock != nil {
			now = l.memoryClock()
		}
		if l.lowMemorySamples == 0 {
			l.lowMemorySince = now
		}
		l.lowMemorySamples++
		if l.lowMemorySamples >= 2 && now.Sub(l.lowMemorySince) >= 10*time.Second {
			reason = fmt.Sprintf("主机可用内存持续低于 %d MiB，Bot 已安全暂停；释放资源后请由管理员恢复", l.Config.HostReserveBytes>>20)
		}
	} else {
		l.resetMemoryPressureLocked()
	}
	if reason != "" {
		// Commit the safety decision under the same lock that validates the
		// observation. No fresh generation may consume this OOM and slip past
		// the pause, even if audit/settings writes temporarily fail.
		l.state.SafetySuspended, l.state.SuspensionReason = true, reason
		l.state.Error, l.state.DiagnosticID = reason, randomID()
		_ = l.persistMemorySuspensionLocked()
		cancel, done := l.startCancel, l.startDone
		l.signalLocked()
		l.mu.Unlock()
		_ = l.finishSuspension(cancel, done)
		return false
	}
	l.mu.Unlock()
	return true
}
