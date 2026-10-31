package host

import (
	"context"
	"encoding/json"
	"errors"
	"filippo.io/age"
	webpush "github.com/SherClockHolmes/webpush-go"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func writeError(w http.ResponseWriter, status int, code, message string, retryable bool) {
	writeJSON(w, status, map[string]any{"error": map[string]any{"code": code, "message": message, "retryable": retryable, "diagnostic_id": randomID()}})
}
func decodeJSON(w http.ResponseWriter, r *http.Request, out any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 128<<10)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if e := d.Decode(out); e != nil {
		writeError(w, 400, "invalid_request", "invalid JSON request", false)
		return false
	}
	if e := d.Decode(&struct{}{}); e != io.EOF {
		writeError(w, 400, "invalid_request", "only one JSON object is accepted", false)
		return false
	}
	return true
}

type Gateway struct {
	Config            Config
	Store             *Store
	Lifecycle         *Lifecycle
	Push              *PushService
	Proxy             *httputil.ReverseProxy
	authGate          chan struct{}
	mu                sync.Mutex
	connections       map[string]map[string]context.CancelFunc
	backupMu          sync.Mutex
	maintenanceHelper *HelperClient
}

func NewGateway(c Config, s *Store, l *Lifecycle) (*Gateway, error) {
	u, e := url.Parse(c.RuntimeURL)
	if e != nil || u.Scheme != "http" || !(u.Hostname() == "127.0.0.1" || u.Hostname() == "::1" || u.Hostname() == "localhost") {
		return nil, errors.New("runtime must use a loopback HTTP endpoint")
	}
	origin, e := url.Parse(c.Origin)
	if e != nil || origin.Scheme != "https" || origin.Host == "" || origin.Path != "" && origin.Path != "/" {
		return nil, errors.New("a trusted HTTPS origin is required")
	}
	push, e := NewPushService(s, c)
	if e != nil {
		return nil, e
	}
	g := &Gateway{Config: c, Store: s, Lifecycle: l, Push: push, authGate: make(chan struct{}, 1), connections: make(map[string]map[string]context.CancelFunc)}
	g.maintenanceHelper = NewHelperClient(c.HelperSocket)
	g.maintenanceHelper.HTTP.Timeout = 15 * time.Minute
	g.Proxy = httputil.NewSingleHostReverseProxy(u)
	director := g.Proxy.Director
	g.Proxy.Director = func(r *http.Request) { director(r); r.Host = u.Host }
	g.Proxy.FlushInterval = -1
	g.Proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, e error) {
		writeError(w, 502, "runtime_unavailable", "运行环境连接中断，请沿原请求编号核对结果。", true)
	}
	return g, nil
}
func (g *Gateway) originAllowed(r *http.Request) bool {
	o := r.Header.Get("Origin")
	return o == "" || o == strings.TrimRight(g.Config.Origin, "/")
}
func (g *Gateway) Handler() http.Handler { return http.HandlerFunc(g.serve) }
func (g *Gateway) serve(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Referrer-Policy", "same-origin")
	w.Header().Set("X-Frame-Options", "DENY")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
	if r.URL.Path == "/health/live" && r.Method == "GET" {
		writeJSON(w, 200, map[string]bool{"live": true})
		return
	}
	if !strings.HasPrefix(r.URL.Path, "/api/") {
		g.static(w, r)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	if !g.originAllowed(r) {
		writeError(w, 403, "origin_forbidden", "请求来源不被允许。", false)
		return
	}
	if r.Method == "OPTIONS" {
		writeError(w, 405, "method_not_allowed", "cross-origin API is disabled", false)
		return
	}
	switch r.URL.Path {
	case "/api/auth/status":
		if r.Method != "GET" {
			break
		}
		s, e := g.Store.Authenticate(r)
		v := map[string]any{"initialized": g.Store.Initialized(), "authenticated": e == nil, "owner": nil}
		if e == nil {
			v["owner"] = map[string]string{"id": s.OwnerID, "username": s.Username}
			v["csrf_token"] = s.CSRF
		}
		writeJSON(w, 200, v)
		return
	case "/api/auth/setup/enrollment", "/api/auth/setup", "/api/auth/login":
		if r.Method != "POST" {
			writeError(w, 405, "method_not_allowed", "POST required", false)
			return
		}
		select {
		case g.authGate <- struct{}{}:
			defer func() { <-g.authGate }()
		default:
			writeError(w, 429, "auth_busy", "认证请求处理中，请稍后重试。", true)
			return
		}
		g.authentication(w, r)
		return
	}
	s, e := g.Store.Authenticate(r)
	if e != nil {
		writeError(w, 401, "authentication_required", "请先登录。", false)
		return
	}
	if r.Method != "GET" && r.Method != "HEAD" && !csrfValid(r, s) {
		writeError(w, 403, "csrf_invalid", "页面验证已过期，请刷新后重试。", false)
		return
	}
	if g.hostRoute(w, r, s) {
		return
	}
	if !strings.HasPrefix(r.URL.Path, "/api/") || strings.Contains(r.URL.EscapedPath(), "%2f") || strings.Contains(r.URL.EscapedPath(), "%2F") {
		writeError(w, 400, "invalid_path", "invalid API path", false)
		return
	}
	passive := r.Method == "GET" && (r.Header.Get("X-Personal-Bot-Passive") == "1" || strings.HasSuffix(r.URL.Path, "/events") || strings.Contains(r.URL.Path, "/browser/view/") || strings.EqualFold(r.Header.Get("Upgrade"), "websocket") && strings.HasPrefix(r.URL.Path, "/api/browser/"))
	snapshot := g.Lifecycle.Snapshot()
	if passive {
		if snapshot.State != "READY" {
			code := "runtime_sleeping"
			if snapshot.State == "FAULTED" {
				code = "runtime_faulted"
			}
			if snapshot.SafetySuspended {
				code = "safety_suspended"
			}
			writeError(w, 503, code, "运行环境休眠或不可用，主动操作后再连接。", true)
			return
		}
	} else {
		ctx, cancel := context.WithTimeout(r.Context(), time.Duration(g.Config.StartupSeconds+5)*time.Second)
		e = g.Lifecycle.EnsureActiveReady(ctx)
		cancel()
		if e != nil {
			code := "runtime_start_failed"
			if g.Lifecycle.Snapshot().SafetySuspended {
				code = "safety_suspended"
			}
			writeError(w, 503, code, "运行环境未就绪，请查看环境状态并沿原请求编号重试。", true)
			return
		}
	}
	if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") && r.Header.Get("Origin") != strings.TrimRight(g.Config.Origin, "/") {
		writeError(w, 403, "origin_forbidden", "WebSocket requires the configured origin", false)
		return
	}
	requestID := r.Header.Get("X-Request-ID")
	if len(requestID) < 1 || len(requestID) > 128 || strings.ContainsAny(requestID, "\r\n") {
		requestID = randomID()
	}
	for key := range r.Header {
		if strings.HasPrefix(strings.ToLower(key), "x-personal-bot-") {
			r.Header.Del(key)
		}
	}
	r.Header.Del("Cookie")
	r.Header.Del("Authorization")
	r.Header.Set("X-Request-ID", requestID)
	r.Header.Set("X-Personal-Bot-Assertion", g.Store.Secrets.assertionForRuntime(r.Method, r.URL.Path, requestID, s.OwnerID, "owner", s.ID, g.Lifecycle.Snapshot().RuntimeID))
	if passive {
		r.Header.Set("X-Personal-Bot-Passive", "1")
	}
	r.Body = http.MaxBytesReader(w, r.Body, 26<<20)
	ctx, cancel := context.WithCancel(r.Context())
	id := randomID()
	g.mu.Lock()
	if g.connections[s.ID] == nil {
		g.connections[s.ID] = make(map[string]context.CancelFunc)
	}
	g.connections[s.ID][id] = cancel
	g.mu.Unlock()
	defer func() {
		cancel()
		g.mu.Lock()
		delete(g.connections[s.ID], id)
		if len(g.connections[s.ID]) == 0 {
			delete(g.connections, s.ID)
		}
		g.mu.Unlock()
	}()
	g.Proxy.ServeHTTP(w, r.WithContext(ctx))
}

func (g *Gateway) authentication(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/api/auth/setup/enrollment":
		var v struct {
			Token    string `json:"setup_token"`
			Username string `json:"username"`
		}
		if !decodeJSON(w, r, &v) {
			return
		}
		out, e := g.Store.Enrollment(v.Token, v.Username)
		if e != nil {
			writeError(w, 400, "enrollment_failed", e.Error(), false)
			return
		}
		writeJSON(w, 200, out)
	case "/api/auth/setup":
		var v struct {
			Token      string `json:"setup_token"`
			Enrollment string `json:"enrollment_id"`
			Username   string `json:"username"`
			Password   string `json:"password"`
			Code       string `json:"totp_code"`
		}
		if !decodeJSON(w, r, &v) {
			return
		}
		s, codes, e := g.Store.Setup(v.Token, v.Enrollment, v.Username, v.Password, v.Code)
		if e != nil {
			writeError(w, 400, "setup_failed", e.Error(), false)
			return
		}
		s, token, e := g.Store.NewSession(s, r.UserAgent())
		if e != nil {
			writeError(w, 500, "session_failed", "账号已创建，请重新登录。", true)
			return
		}
		sessionCookie(w, token)
		writeJSON(w, 200, map[string]any{"owner": map[string]string{"id": s.OwnerID, "username": s.Username}, "csrf_token": s.CSRF, "recovery_codes": codes})
	case "/api/auth/login":
		var v struct {
			Username string `json:"username"`
			Password string `json:"password"`
			Code     string `json:"totp_code"`
			Recovery string `json:"recovery_code"`
		}
		if !decodeJSON(w, r, &v) {
			return
		}
		s, e := g.Store.Login(v.Username, v.Password, v.Code, v.Recovery, remoteHost(r))
		if e != nil {
			writeError(w, 401, "login_failed", "账号、密码或验证凭据无效，或尝试过于频繁。", false)
			return
		}
		s, token, e := g.Store.NewSession(s, r.UserAgent())
		if e != nil {
			writeError(w, 500, "session_failed", "无法建立会话。", true)
			return
		}
		sessionCookie(w, token)
		writeJSON(w, 200, map[string]any{"owner": map[string]string{"id": s.OwnerID, "username": s.Username}, "csrf_token": s.CSRF})
	}
}

func (g *Gateway) revoke(id string) {
	g.mu.Lock()
	for _, cancel := range g.connections[id] {
		cancel()
	}
	delete(g.connections, id)
	g.mu.Unlock()
	if g.Lifecycle.Snapshot().State == "READY" {
		ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
		defer cancel()
		_ = g.Lifecycle.Runtime.Call(ctx, "POST", "/internal/revoke-session", map[string]string{"session_id": id}, nil)
	}
}
func (g *Gateway) hostRoute(w http.ResponseWriter, r *http.Request, s Session) bool {
	switch {
	case r.URL.Path == "/api/auth/logout" && r.Method == "POST":
		if _, e := g.Store.DB.Exec("DELETE FROM sessions WHERE id=?", s.ID); e != nil {
			writeError(w, 500, "storage_failed", "无法注销会话。", true)
			return true
		}
		g.revoke(s.ID)
		clearCookie(w)
		writeJSON(w, 200, map[string]bool{"logged_out": true})
		return true
	case r.URL.Path == "/api/auth/sessions" && r.Method == "GET":
		rows, e := g.Store.DB.Query("SELECT id,created_at,last_seen_at,expires,user_agent FROM sessions WHERE expires>? ORDER BY created_at DESC", time.Now().Unix())
		if e != nil {
			writeError(w, 500, "storage_failed", "无法读取会话。", true)
			return true
		}
		defer rows.Close()
		items := []map[string]any{}
		for rows.Next() {
			var id, created, last, agent string
			var expires int64
			if e = rows.Scan(&id, &created, &last, &expires, &agent); e != nil {
				break
			}
			items = append(items, map[string]any{"id": id, "created_at": created, "last_seen_at": last, "expires_at": timestamp(time.Unix(expires, 0)), "user_agent": agent, "current": id == s.ID})
		}
		writeJSON(w, 200, map[string]any{"items": items})
		return true
	case strings.HasPrefix(r.URL.Path, "/api/auth/sessions/") && r.Method == "DELETE":
		id := strings.TrimPrefix(r.URL.Path, "/api/auth/sessions/")
		if _, e := g.Store.DB.Exec("DELETE FROM sessions WHERE id=?", id); e != nil {
			writeError(w, 500, "storage_failed", "无法撤销会话。", true)
			return true
		}
		g.revoke(id)
		if id == s.ID {
			clearCookie(w)
		}
		writeJSON(w, 200, map[string]bool{"revoked": true})
		return true
	case r.URL.Path == "/api/runtime" && r.Method == "GET":
		writeJSON(w, 200, g.Lifecycle.Status())
		return true
	case r.URL.Path == "/api/runtime/retry" && r.Method == "POST":
		if e := g.Lifecycle.Retry(); e != nil {
			writeError(w, 409, "retry_rejected", e.Error(), false)
		} else {
			writeJSON(w, 200, map[string]bool{"retry_enabled": true})
		}
		return true
	case r.URL.Path == "/api/notifications" && r.Method == "GET":
		rows, e := g.Store.DB.Query("SELECT id,task_id,category,created_at,read_at FROM notifications ORDER BY created_at DESC LIMIT 100")
		if e != nil {
			writeError(w, 500, "storage_failed", "无法读取通知。", true)
			return true
		}
		defer rows.Close()
		items := []map[string]any{}
		for rows.Next() {
			var id, task, category, created string
			var read *string
			if rows.Scan(&id, &task, &category, &created, &read) != nil {
				break
			}
			items = append(items, map[string]any{"id": id, "task_id": task, "category": category, "created_at": created, "read_at": read})
		}
		writeJSON(w, 200, map[string]any{"items": items})
		return true
	case strings.HasPrefix(r.URL.Path, "/api/notifications/") && strings.HasSuffix(r.URL.Path, "/read") && r.Method == "POST":
		id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/notifications/"), "/read")
		_, e := g.Store.DB.Exec("UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=?", timestamp(time.Now()), id)
		if e != nil {
			writeError(w, 500, "storage_failed", "无法更新通知。", true)
		} else {
			writeJSON(w, 200, map[string]bool{"read": true})
		}
		return true
	case r.URL.Path == "/api/push/public-key" && r.Method == "GET":
		writeJSON(w, 200, map[string]any{"public_key": g.Push.Public, "configured": g.Push.Public != "" && g.Push.Contact != ""})
		return true
	case r.URL.Path == "/api/push/subscriptions" && r.Method == "POST":
		var input struct {
			Endpoint       string       `json:"endpoint"`
			Keys           webpush.Keys `json:"keys"`
			ExpirationTime *int64       `json:"expirationTime,omitempty"`
		}
		if !decodeJSON(w, r, &input) {
			return true
		}
		v := webpush.Subscription{Endpoint: input.Endpoint, Keys: input.Keys}
		id, e := g.Push.Subscribe(v)
		if e != nil {
			writeError(w, 400, "subscription_invalid", e.Error(), false)
		} else {
			writeJSON(w, 200, map[string]string{"id": id})
		}
		return true
	case r.URL.Path == "/api/push/subscriptions" && r.Method == "GET":
		rows, e := g.Store.DB.Query("SELECT id,created_at,disabled FROM push_subscriptions")
		if e != nil {
			writeError(w, 500, "storage_failed", "无法读取推送订阅。", true)
			return true
		}
		defer rows.Close()
		items := []map[string]any{}
		for rows.Next() {
			var id, created string
			var disabled bool
			if rows.Scan(&id, &created, &disabled) != nil {
				break
			}
			items = append(items, map[string]any{"id": id, "created_at": created, "disabled": disabled})
		}
		writeJSON(w, 200, map[string]any{"items": items})
		return true
	case strings.HasPrefix(r.URL.Path, "/api/push/subscriptions/") && r.Method == "DELETE":
		id := strings.TrimPrefix(r.URL.Path, "/api/push/subscriptions/")
		tx, e := g.Store.DB.Begin()
		if e == nil {
			_, e = tx.Exec("DELETE FROM push_deliveries WHERE subscription_id=?", id)
			if e == nil {
				_, e = tx.Exec("DELETE FROM push_subscriptions WHERE id=?", id)
			}
			if e == nil {
				e = tx.Commit()
			} else {
				tx.Rollback()
			}
		}
		if e != nil {
			writeError(w, 500, "storage_failed", "无法删除订阅。", true)
		} else {
			writeJSON(w, 200, map[string]bool{"deleted": true})
		}
		return true
	case r.URL.Path == "/api/maintenance/status" && r.Method == "GET":
		writeJSON(w, 200, g.MaintenanceStatus())
		return true
	case r.URL.Path == "/api/settings/backup-recipient" && r.Method == "PUT":
		var v struct {
			Recipient string `json:"recipient"`
		}
		if !decodeJSON(w, r, &v) {
			return true
		}
		if _, e := age.ParseX25519Recipient(v.Recipient); e != nil {
			writeError(w, 400, "recipient_invalid", "需要有效的 age X25519 公钥。", false)
			return true
		}
		if _, e := g.Store.DB.Exec("INSERT INTO settings(key,value) VALUES('backup_recipient',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", v.Recipient); e != nil {
			writeError(w, 500, "storage_failed", "无法保存备份公钥。", true)
		} else {
			writeJSON(w, 200, map[string]bool{"configured": true})
		}
		return true
	case r.URL.Path == "/api/backups" && r.Method == "GET":
		items, e := ListBackups(g.Config.BackupDir)
		if e != nil && !os.IsNotExist(e) {
			writeError(w, 500, "backup_list_failed", "无法读取备份目录。", true)
		} else {
			writeJSON(w, 200, map[string]any{"items": items})
		}
		return true
	case strings.HasPrefix(r.URL.Path, "/api/backups/") && strings.HasSuffix(r.URL.Path, "/download") && r.Method == "GET":
		id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/backups/"), "/download")
		if !validBackupID(id) {
			writeError(w, 404, "not_found", "备份不存在。", false)
			return true
		}
		f, e := os.Open(filepath.Join(g.Config.BackupDir, id))
		if e != nil {
			writeError(w, 404, "not_found", "备份不存在。", false)
			return true
		}
		defer f.Close()
		st, e := f.Stat()
		if e != nil || !st.Mode().IsRegular() {
			writeError(w, 404, "not_found", "备份不存在。", false)
			return true
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Content-Disposition", "attachment; filename=\""+id+"\"")
		http.ServeContent(w, r, id, st.ModTime(), f)
		return true
	case r.URL.Path == "/api/diagnostics/export" && r.Method == "POST":
		w.Header().Set("Content-Disposition", "attachment; filename=personal-bot-diagnostics.json")
		var details any = map[string]bool{"available": false}
		if g.Lifecycle.Snapshot().State == "READY" {
			ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
			var summary map[string]any
			if e := g.Lifecycle.Runtime.Call(ctx, "GET", "/internal/diagnostics", nil, &summary); e == nil {
				details = summary
			}
			cancel()
		}
		writeJSON(w, 200, map[string]any{"generated_at": timestamp(time.Now()), "runtime": g.Lifecycle.Status(), "runtime_details": details, "sqlite_required": ">=3.51.3", "maintenance": g.MaintenanceStatus(), "contains_task_content": false})
		return true
	}
	return false
}

func (g *Gateway) static(w http.ResponseWriter, r *http.Request) {
	if r.Method != "GET" && r.Method != "HEAD" {
		http.NotFound(w, r)
		return
	}
	clean := path.Clean("/" + r.URL.Path)
	relative := strings.TrimPrefix(clean, "/")
	target := filepath.Join(g.Config.StaticDir, filepath.FromSlash(relative))
	st, e := os.Lstat(target)
	if e == nil && st.Mode().IsRegular() {
		if strings.HasPrefix(relative, "assets/") {
			w.Header().Set("Cache-Control", "public,max-age=31536000,immutable")
		} else {
			w.Header().Set("Cache-Control", "no-cache")
		}
		http.ServeFile(w, r, target)
		return
	}
	if path.Ext(clean) != "" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Cache-Control", "no-cache")
	http.ServeFile(w, r, filepath.Join(g.Config.StaticDir, "index.html"))
}
