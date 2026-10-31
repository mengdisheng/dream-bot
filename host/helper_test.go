package host

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func TestImmutableImageReferences(t *testing.T) {
	for _, image := range []string{"sha256:" + strings.Repeat("a", 64), "registry.example/bot@sha256:" + strings.Repeat("b", 64)} {
		if !immutableImagePattern.MatchString(image) {
			t.Fatal("immutable reference rejected", image)
		}
	}
	for _, image := range []string{"bot:latest", "bot:v1", "sha256:abc", "evil @sha256:" + strings.Repeat("a", 64)} {
		if immutableImagePattern.MatchString(image) {
			t.Fatal("mutable or invalid reference accepted", image)
		}
	}
}

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func TestRestrictedHelperTemplateAndDeletionIdentity(t *testing.T) {
	root := t.TempDir()
	pub := filepath.Join(root, "gateway.pub")
	if e := os.WriteFile(pub, []byte("public-test-key"), 0600); e != nil {
		t.Fatal(e)
	}
	cfg := HelperConfig{DataRoot: root, PublicKeyPath: pub, Image: "bot@sha256:" + strings.Repeat("a", 64), BridgeDir: "/run/personal-bot-bridge", RuntimePort: 8091, Network: "personal-bot", CgroupParent: "personal-bot.slice", MemoryBytes: 2 << 30, NanoCPUs: 1e9, PidsLimit: 256, GatewayUID: 31010}
	var instance Instance
	var labels map[string]string
	var created map[string]any
	deletes := 0
	engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := strings.TrimPrefix(r.URL.Path, "/v1.45")
		switch {
		case p == "/containers/json":
			items := []map[string]string{}
			if instance.Exists {
				items = append(items, map[string]string{"Id": instance.ContainerID})
			}
			writeJSON(w, 200, items)
		case p == "/containers/create":
			if e := json.NewDecoder(r.Body).Decode(&created); e != nil {
				t.Error(e)
			}
			b, _ := json.Marshal(created["Labels"])
			_ = json.Unmarshal(b, &labels)
			instance = Instance{Exists: true, ContainerID: strings.Repeat("b", 64), RuntimeID: labels["personal-bot.runtime_id"], Epoch: labels["personal-bot.deployment_epoch"], State: "running"}
			writeJSON(w, 201, map[string]string{"Id": instance.ContainerID})
		case strings.HasSuffix(p, "/start") || strings.HasSuffix(p, "/stop"):
			w.WriteHeader(204)
		case strings.HasSuffix(p, "/json"):
			if !instance.Exists {
				w.WriteHeader(404)
				return
			}
			writeJSON(w, 200, map[string]any{"Id": instance.ContainerID, "Config": map[string]any{"Image": cfg.Image, "Labels": labels}, "State": map[string]string{"Status": "running"}})
		case r.Method == "DELETE":
			deletes++
			instance.Exists = false
			w.WriteHeader(204)
		default:
			t.Error("unexpected Docker operation", r.Method, p)
			w.WriteHeader(500)
		}
	}))
	defer engine.Close()
	base, _ := url.Parse(engine.URL)
	d := &DockerManager{Config: cfg, HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		r.URL.Scheme = base.Scheme
		r.URL.Host = base.Host
		return http.DefaultTransport.RoundTrip(r)
	})}}
	rid, epoch := randomID(), randomID()
	v, e := d.Ensure(context.Background(), EnsureRequest{rid, epoch})
	if e != nil {
		t.Fatal(e)
	}
	config := created["HostConfig"].(map[string]any)
	if config["AutoRemove"] != true || config["ReadonlyRootfs"] != true || config["Memory"] != float64(2<<30) || config["NetworkMode"] == "host" {
		t.Fatal("unsafe container template", config)
	}
	for _, bind := range config["Binds"].([]any) {
		if strings.Contains(bind.(string), "docker.sock") {
			t.Fatal("Docker socket mounted into runtime")
		}
	}
	ports := config["PortBindings"].(map[string]any)["8091/tcp"].([]any)[0].(map[string]any)
	if ports["HostIp"] != "127.0.0.1" {
		t.Fatal("runtime port is public")
	}
	if e = d.Remove(context.Background(), RemoveRequest{v.ContainerID, randomID(), epoch}); e == nil || deletes != 0 {
		t.Fatal("mismatched runtime deletion accepted")
	}
	labels[projectLabel] = "other-service"
	if e = d.Remove(context.Background(), RemoveRequest{v.ContainerID, rid, epoch}); e == nil || deletes != 0 {
		t.Fatal("unrelated service deletion accepted")
	}
	labels[projectLabel] = "personal-bot"
	if e = d.Remove(context.Background(), RemoveRequest{v.ContainerID, rid, epoch}); e != nil || deletes != 1 {
		t.Fatal("exact project removal failed", e)
	}
}
func TestHelperRejectsCallerDefinedTemplateAndSpoofedPeer(t *testing.T) {
	d := NewDockerManager(HelperConfig{GatewayUID: 31010})
	r := httptest.NewRequest("POST", "http://helper/v1/ensure", strings.NewReader(`{"runtime_id":"aaaaaaaaaaaaaaaaaaaaaaaa","deployment_epoch":"bbbbbbbbbbbbbbbbbbbbbbbb","image":"malicious"}`))
	r = r.WithContext(context.WithValue(r.Context(), peerKey{}, uint32(31010)))
	w := httptest.NewRecorder()
	d.Handler().ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatal("caller could override image", w.Code)
	}
	r = httptest.NewRequest("POST", "http://helper/v1/inspect", bytes.NewBufferString(`{}`))
	r.Header.Set("X-UID", "31010")
	w = httptest.NewRecorder()
	d.Handler().ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("spoofed helper peer accepted")
	}
}

func TestAutoRemoveConflictsRequireObservedDisappearance(t *testing.T) {
	for _, conflict := range []string{"stop", "delete"} {
		t.Run(conflict, func(t *testing.T) {
			cfg := HelperConfig{Image: "sha256:" + strings.Repeat("a", 64)}
			request := RemoveRequest{strings.Repeat("b", 64), randomID(), randomID()}
			stopped, deletes, afterStopInspects := false, 0, 0
			engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case strings.HasSuffix(r.URL.Path, "/json"):
					if stopped {
						afterStopInspects++
						if afterStopInspects >= 2 {
							w.WriteHeader(404)
							return
						}
					}
					writeJSON(w, 200, map[string]any{"Id": request.ContainerID,
						"Config": map[string]any{"Image": cfg.Image, "Labels": map[string]string{projectLabel: "personal-bot", "personal-bot.runtime_id": request.RuntimeID, "personal-bot.deployment_epoch": request.Epoch}},
						"State":  map[string]string{"Status": "removing"}})
				case strings.HasSuffix(r.URL.Path, "/stop"):
					stopped = true
					if conflict == "stop" {
						w.WriteHeader(409)
					} else {
						w.WriteHeader(204)
					}
				case r.Method == "DELETE":
					deletes++
					w.WriteHeader(409)
				default:
					t.Error("unexpected Docker operation")
					w.WriteHeader(500)
				}
			}))
			defer engine.Close()
			base, _ := url.Parse(engine.URL)
			manager := &DockerManager{Config: cfg, HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
				r.URL.Scheme, r.URL.Host = base.Scheme, base.Host
				return http.DefaultTransport.RoundTrip(r)
			})}}
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			if e := manager.Remove(ctx, request); e != nil {
				t.Fatal(e)
			}
			if afterStopInspects != 2 || conflict == "stop" && deletes != 0 || conflict == "delete" && deletes != 1 {
				t.Fatal("conflict was accepted without verifying disappearance, or deletion was repeated")
			}
		})
	}
}

func TestRemovalConflictCannotHideAnExistingContainer(t *testing.T) {
	cfg := HelperConfig{Image: "sha256:" + strings.Repeat("a", 64)}
	request := RemoveRequest{strings.Repeat("b", 64), randomID(), randomID()}
	engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/json") {
			writeJSON(w, 200, map[string]any{"Id": request.ContainerID,
				"Config": map[string]any{"Image": cfg.Image, "Labels": map[string]string{projectLabel: "personal-bot", "personal-bot.runtime_id": request.RuntimeID, "personal-bot.deployment_epoch": request.Epoch}},
				"State":  map[string]string{"Status": "running"}})
		} else if r.Method == "DELETE" {
			w.WriteHeader(409)
		} else {
			w.WriteHeader(204)
		}
	}))
	defer engine.Close()
	base, _ := url.Parse(engine.URL)
	manager := &DockerManager{Config: cfg, HTTP: &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		r.URL.Scheme, r.URL.Host = base.Scheme, base.Host
		return http.DefaultTransport.RoundTrip(r)
	})}}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	if e := manager.Remove(ctx, request); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatalf("unconfirmed removal did not fail closed: %v", e)
	}
}
