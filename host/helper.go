package host

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sync"
	"time"
)

type HelperConfig struct {
	Socket          string `json:"socket"`
	DockerSocket    string `json:"docker_socket"`
	GatewayUID      uint32 `json:"gateway_uid"`
	GatewayGID      uint32 `json:"gateway_gid"`
	Image           string `json:"image"`
	DataRoot        string `json:"data_root"`
	BridgeDir       string `json:"bridge_dir"`
	PublicKeyPath   string `json:"public_key_path"`
	RuntimePort     int    `json:"runtime_port"`
	Network         string `json:"network"`
	CgroupParent    string `json:"cgroup_parent"`
	MemoryBytes     int64  `json:"memory_bytes"`
	NanoCPUs        int64  `json:"nano_cpus"`
	PidsLimit       int64  `json:"pids_limit"`
	AppArmorProfile string `json:"apparmor_profile,omitempty"`
	SeccompProfile  string `json:"seccomp_profile,omitempty"`
	BackupRecipient string `json:"backup_recipient"`
}

var immutableImagePattern = regexp.MustCompile(`^(sha256:[0-9a-f]{64}|[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64})$`)

func LoadHelperConfig(path string) (HelperConfig, error) {
	c := HelperConfig{Socket: "/run/personal-bot-host/helper.sock", DockerSocket: "/var/run/docker.sock", DataRoot: "/var/lib/personal-bot", BridgeDir: "/run/personal-bot-bridge", RuntimePort: 8091, Network: "personal-bot", CgroupParent: "personal-bot.slice", MemoryBytes: 2 << 30, NanoCPUs: 1e9, PidsLimit: 256}
	if e := rootOwned(path); e != nil {
		return c, e
	}
	b, e := os.ReadFile(path)
	if e != nil {
		return c, e
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e = d.Decode(&c); e != nil {
		return c, e
	}
	if c.GatewayGID == 0 {
		c.GatewayGID = c.GatewayUID
	}
	if !immutableImagePattern.MatchString(c.Image) {
		return c, errors.New("image must be pinned to a sha256 digest")
	}
	for _, p := range []string{c.DataRoot, c.BridgeDir, c.PublicKeyPath, c.Socket, c.DockerSocket} {
		if !filepath.IsAbs(p) || filepath.Clean(p) != p || p == "/" {
			return c, errors.New("helper paths must be fixed absolute paths")
		}
	}
	if c.GatewayUID == 0 || c.MemoryBytes < 256<<20 || c.MemoryBytes > 6<<30 || c.NanoCPUs <= 0 || c.NanoCPUs > 2e9 || c.PidsLimit < 32 || c.PidsLimit > 512 || c.RuntimePort < 1024 || c.RuntimePort > 65535 || c.Network == "host" || c.Network == "none" || c.Network == "" {
		return c, errors.New("unsafe helper template")
	}
	if c.AppArmorProfile == "unconfined" {
		return c, errors.New("AppArmor may not be disabled")
	}
	if c.SeccompProfile != "" {
		if !filepath.IsAbs(c.SeccompProfile) {
			return c, errors.New("seccomp profile must be a fixed absolute root-owned file")
		}
		if _, e := fixedSeccompJSON(c.SeccompProfile); e != nil {
			return c, e
		}
	}
	return c, nil
}
func fixedSeccompJSON(path string) (string, error) {
	if e := rootOwned(path); e != nil {
		return "", e
	}
	b, e := os.ReadFile(path)
	if e != nil {
		return "", e
	}
	if len(b) > 1<<20 {
		return "", errors.New("seccomp policy too large")
	}
	var policy struct {
		DefaultAction string `json:"defaultAction"`
	}
	if e = json.Unmarshal(b, &policy); e != nil {
		return "", e
	}
	switch policy.DefaultAction {
	case "SCMP_ACT_ERRNO", "SCMP_ACT_KILL", "SCMP_ACT_KILL_PROCESS", "SCMP_ACT_KILL_THREAD":
	default:
		return "", errors.New("seccomp policy must have a denying default action")
	}
	var compact bytes.Buffer
	if e = json.Compact(&compact, b); e != nil {
		return "", e
	}
	return compact.String(), nil
}

type Instance struct {
	Exists      bool   `json:"exists"`
	ContainerID string `json:"container_id"`
	RuntimeID   string `json:"runtime_id"`
	Epoch       string `json:"deployment_epoch"`
	State       string `json:"state"`
	Image       string `json:"image"`
}
type EnsureRequest struct {
	RuntimeID string `json:"runtime_id"`
	Epoch     string `json:"deployment_epoch"`
}
type RemoveRequest struct {
	ContainerID string `json:"container_id"`
	RuntimeID   string `json:"runtime_id"`
	Epoch       string `json:"deployment_epoch"`
}
type ContainerManager interface {
	Inspect(context.Context) (Instance, error)
	Ensure(context.Context, EnsureRequest) (Instance, error)
	Remove(context.Context, RemoveRequest) error
}

func unixClient(socket string, timeout time.Duration) *http.Client {
	return unixPeerClient(socket, timeout, nil)
}
func unixPeerClient(socket string, timeout time.Duration, expected *uint32) *http.Client {
	return &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		c, e := (&net.Dialer{}).DialContext(ctx, "unix", socket)
		if e != nil {
			return nil, e
		}
		if expected != nil {
			uid, ok := connectionPeerUID(c)
			if !ok || uid != *expected {
				c.Close()
				return nil, errors.New("Unix server peer credential mismatch")
			}
		}
		return c, nil
	}, MaxIdleConnsPerHost: 2}, Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirects disabled") }}
}

type HelperClient struct{ HTTP *http.Client }

func NewHelperClient(socket string) *HelperClient {
	root := uint32(0)
	return &HelperClient{HTTP: unixPeerClient(socket, 70*time.Second, &root)}
}
func (h *HelperClient) call(ctx context.Context, path string, in, out any) error {
	b, e := json.Marshal(in)
	if e != nil {
		return e
	}
	r, e := http.NewRequestWithContext(ctx, "POST", "http://helper"+path, bytes.NewReader(b))
	if e != nil {
		return e
	}
	r.Header.Set("Content-Type", "application/json")
	resp, e := h.HTTP.Do(r)
	if e != nil {
		return e
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("helper operation rejected (%d)", resp.StatusCode)
	}
	if out != nil {
		return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
	}
	return nil
}
func (h *HelperClient) Inspect(ctx context.Context) (Instance, error) {
	var out Instance
	e := h.call(ctx, "/v1/inspect", struct{}{}, &out)
	return out, e
}
func (h *HelperClient) Ensure(ctx context.Context, r EnsureRequest) (Instance, error) {
	var out Instance
	e := h.call(ctx, "/v1/ensure", r, &out)
	return out, e
}
func (h *HelperClient) Remove(ctx context.Context, r RemoveRequest) error {
	return h.call(ctx, "/v1/stop-remove", r, nil)
}

type DockerManager struct {
	Config HelperConfig
	HTTP   *http.Client
	mu     sync.Mutex
}

func NewDockerManager(c HelperConfig) *DockerManager {
	return &DockerManager{Config: c, HTTP: unixClient(c.DockerSocket, 65*time.Second)}
}
func (d *DockerManager) docker(ctx context.Context, method, path string, in, out any) (int, error) {
	var body io.Reader
	if in != nil {
		b, e := json.Marshal(in)
		if e != nil {
			return 0, e
		}
		body = bytes.NewReader(b)
	}
	r, e := http.NewRequestWithContext(ctx, method, "http://docker/v1.45"+path, body)
	if e != nil {
		return 0, e
	}
	r.Header.Set("Content-Type", "application/json")
	resp, e := d.HTTP.Do(r)
	if e != nil {
		return 0, e
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return resp.StatusCode, fmt.Errorf("Docker %s rejected (%d)", method, resp.StatusCode)
	}
	if out != nil {
		e = json.NewDecoder(io.LimitReader(resp.Body, 2<<20)).Decode(out)
	}
	return resp.StatusCode, e
}

const projectLabel = "personal-bot.project"

func (d *DockerManager) inspectID(ctx context.Context, id string) (Instance, error) {
	var v struct {
		ID     string `json:"Id"`
		Config struct {
			Image  string
			Labels map[string]string
		}
		State struct{ Status string }
	}
	code, e := d.docker(ctx, "GET", "/containers/"+url.PathEscape(id)+"/json", nil, &v)
	if code == 404 {
		return Instance{}, nil
	}
	if e != nil {
		return Instance{}, e
	}
	if v.Config.Labels[projectLabel] != "personal-bot" || v.Config.Image != d.Config.Image {
		return Instance{}, errors.New("container does not match the fixed project template")
	}
	return Instance{Exists: true, ContainerID: v.ID, RuntimeID: v.Config.Labels["personal-bot.runtime_id"], Epoch: v.Config.Labels["personal-bot.deployment_epoch"], State: v.State.Status, Image: v.Config.Image}, nil
}
func (d *DockerManager) inspect(ctx context.Context) (Instance, error) {
	filters, _ := json.Marshal(map[string][]string{"label": {projectLabel + "=personal-bot"}})
	var rows []struct {
		ID string `json:"Id"`
	}
	_, e := d.docker(ctx, "GET", "/containers/json?all=1&filters="+url.QueryEscape(string(filters)), nil, &rows)
	if e != nil {
		return Instance{}, e
	}
	if len(rows) > 1 {
		return Instance{}, errors.New("multiple project instances; manual reconciliation required")
	}
	if len(rows) == 0 {
		return Instance{}, nil
	}
	return d.inspectID(ctx, rows[0].ID)
}
func (d *DockerManager) Inspect(ctx context.Context) (Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.inspect(ctx)
}

var identityPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{20,100}$`)

func (d *DockerManager) Ensure(ctx context.Context, r EnsureRequest) (Instance, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !identityPattern.MatchString(r.RuntimeID) || !identityPattern.MatchString(r.Epoch) {
		return Instance{}, errors.New("invalid runtime identity")
	}
	old, e := d.inspect(ctx)
	if e != nil {
		return old, e
	}
	if old.Exists {
		if old.RuntimeID != r.RuntimeID || old.Epoch != r.Epoch {
			return old, errors.New("another project runtime exists")
		}
		return old, nil
	}
	pub, e := os.ReadFile(d.Config.PublicKeyPath)
	if e != nil {
		return Instance{}, errors.New("gateway verification key unavailable")
	}
	pubDir := filepath.Join(d.Config.DataRoot, "runtime-public")
	if e = os.MkdirAll(pubDir, 0755); e != nil {
		return Instance{}, e
	}
	if e = os.Chmod(pubDir, 0755); e != nil {
		return Instance{}, e
	}
	if e = os.WriteFile(filepath.Join(pubDir, "gateway.pub"), pub, 0444); e != nil {
		return Instance{}, e
	}
	if e = os.Chmod(filepath.Join(pubDir, "gateway.pub"), 0444); e != nil {
		return Instance{}, e
	}
	binds := []string{filepath.Join(d.Config.DataRoot, "app") + ":/data/app", filepath.Join(d.Config.DataRoot, "files") + ":/data/files", filepath.Join(d.Config.DataRoot, "browser") + ":/data/browser", d.Config.BridgeDir + ":/run/personal-bot/host:ro", pubDir + ":/run/personal-bot-gateway:ro"}
	security := []string{"no-new-privileges:true"}
	if d.Config.SeccompProfile != "" {
		policy, err := fixedSeccompJSON(d.Config.SeccompProfile)
		if err != nil {
			return Instance{}, err
		}
		security = append(security, "seccomp="+policy)
	}
	if d.Config.AppArmorProfile != "" {
		security = append(security, "apparmor="+d.Config.AppArmorProfile)
	}
	body := map[string]any{"Image": d.Config.Image, "Hostname": "personal-bot", "Labels": map[string]string{projectLabel: "personal-bot", "personal-bot.runtime_id": r.RuntimeID, "personal-bot.deployment_epoch": r.Epoch}, "Env": []string{"PERSONAL_BOT_RUNTIME_ID=" + r.RuntimeID, "PERSONAL_BOT_DEPLOYMENT_EPOCH=" + r.Epoch, "PERSONAL_BOT_GATEWAY_PUBLIC_KEY=/run/personal-bot-gateway/gateway.pub", "PERSONAL_BOT_HOST_BRIDGE=/run/personal-bot/host/bridge.sock", "OMP_NUM_THREADS=1", "OPENBLAS_NUM_THREADS=1", "MKL_NUM_THREADS=1"}, "ExposedPorts": map[string]any{"8091/tcp": map[string]any{}}, "HostConfig": map[string]any{"AutoRemove": true, "RestartPolicy": map[string]string{"Name": "no"}, "ReadonlyRootfs": true, "Binds": binds, "PortBindings": map[string]any{"8091/tcp": []map[string]string{{"HostIp": "127.0.0.1", "HostPort": fmt.Sprint(d.Config.RuntimePort)}}}, "Memory": d.Config.MemoryBytes, "MemorySwap": d.Config.MemoryBytes, "NanoCpus": d.Config.NanoCPUs, "PidsLimit": d.Config.PidsLimit, "CgroupParent": d.Config.CgroupParent, "ShmSize": int64(256 << 20), "NetworkMode": d.Config.Network, "CapDrop": []string{"ALL"}, "CapAdd": []string{"SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "KILL", "SETPCAP"}, "SecurityOpt": security, "Tmpfs": map[string]string{"/run": "rw,nosuid,nodev,size=64m,mode=755", "/tmp": "rw,nosuid,nodev,noexec,size=128m,mode=1777", "/scratch": "rw,nosuid,nodev,noexec,size=256m,mode=1777"}, "LogConfig": map[string]any{"Type": "json-file", "Config": map[string]string{"max-size": "10m", "max-file": "3"}}}}
	var created struct {
		ID string `json:"Id"`
	}
	_, e = d.docker(ctx, "POST", "/containers/create?name=personal-bot-"+r.RuntimeID[:12], body, &created)
	if e != nil {
		return Instance{}, e
	}
	_, e = d.docker(ctx, "POST", "/containers/"+created.ID+"/start", nil, nil)
	if e != nil {
		return Instance{}, e
	}
	return d.inspectID(ctx, created.ID)
}
func (d *DockerManager) Remove(ctx context.Context, r RemoveRequest) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(r.ContainerID) || !identityPattern.MatchString(r.RuntimeID) || !identityPattern.MatchString(r.Epoch) {
		return errors.New("invalid deletion identity")
	}
	v, e := d.inspectID(ctx, r.ContainerID)
	if e != nil || !v.Exists {
		return e
	}
	if v.RuntimeID != r.RuntimeID || v.Epoch != r.Epoch {
		return errors.New("deletion identity mismatch")
	}
	code, e := d.docker(ctx, "POST", "/containers/"+v.ContainerID+"/stop?t=10", nil, nil)
	if e != nil && code == http.StatusConflict {
		// Docker AutoRemove can already be removing the stopped container.
		// A conflict alone is not proof of removal; verify the exact ID vanishes.
		return d.waitRemoved(ctx, r)
	}
	if e != nil && code != 304 && code != 404 {
		return e
	}
	code, e = d.docker(ctx, "DELETE", "/containers/"+v.ContainerID+"?force=1&v=0", nil, nil)
	if e != nil && code != 404 && code != http.StatusConflict {
		return e
	}
	return d.waitRemoved(ctx, r)
}

func (d *DockerManager) waitRemoved(ctx context.Context, r RemoveRequest) error {
	for {
		v, e := d.inspectID(ctx, r.ContainerID)
		if e != nil {
			return e
		}
		if !v.Exists {
			return nil
		}
		if v.RuntimeID != r.RuntimeID || v.Epoch != r.Epoch {
			return errors.New("deletion identity changed while waiting")
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(100 * time.Millisecond):
		}
	}
}

func (d *DockerManager) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u, ok := peerUID(r.Context())
		if !ok || (u != d.Config.GatewayUID && u != 0) {
			writeError(w, 403, "peer_forbidden", "local peer is not authorized", false)
			return
		}
		if r.Method != "POST" {
			writeError(w, 405, "method_not_allowed", "POST required", false)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 65*time.Second)
		defer cancel()
		var out any
		var e error
		switch r.URL.Path {
		case "/v1/inspect":
			out, e = d.Inspect(ctx)
		case "/v1/ensure":
			var v EnsureRequest
			if !decodeJSON(w, r, &v) {
				return
			}
			out, e = d.Ensure(ctx, v)
		case "/v1/stop-remove":
			var v RemoveRequest
			if !decodeJSON(w, r, &v) {
				return
			}
			e = d.Remove(ctx, v)
			out = map[string]bool{"removed": e == nil}
		case "/v1/backup":
			var v BackupRequest
			if !decodeJSON(w, r, &v) {
				return
			}
			out, e = d.BackupArchive(r.Context(), v)
		case "/v1/offline-snapshot":
			var v struct {
				ID string `json:"maintenance_id"`
			}
			if !decodeJSON(w, r, &v) {
				return
			}
			e = d.OfflineSnapshot(ctx, v.ID)
			out = map[string]bool{"snapshot": e == nil}
		case "/v1/restore-preview", "/v1/restore":
			if u != 0 {
				writeError(w, 403, "root_required", "restore requires local root", false)
				return
			}
			var v RestoreRequest
			if !decodeJSON(w, r, &v) {
				return
			}
			if r.URL.Path == "/v1/restore-preview" {
				out, e = d.RestorePreview(r.Context(), v)
			} else {
				out, e = d.RestoreArchive(r.Context(), v)
			}
		default:
			writeError(w, 404, "not_found", "unknown fixed operation", false)
			return
		}
		if e != nil {
			writeError(w, 409, "helper_rejected", e.Error(), true)
			return
		}
		writeJSON(w, 200, out)
	})
}
