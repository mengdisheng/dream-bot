package host

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"encoding/base32"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func setupTestOwner(t *testing.T, s *Store) (Session, []string) {
	t.Helper()
	token, e := s.SetupToken()
	if e != nil {
		t.Fatal(e)
	}
	enrollment, e := s.Enrollment(token, "owner")
	if e != nil {
		t.Fatal(e)
	}
	seed, e := base32.StdEncoding.WithPadding(base32.NoPadding).DecodeString(enrollment["totp_secret"].(string))
	if e != nil {
		t.Fatal(e)
	}
	owner, codes, e := s.Setup(token, enrollment["enrollment_id"].(string), "owner", "test-password-at-least-12", TOTP(seed, time.Now().Unix()/30))
	if e != nil {
		t.Fatal(e)
	}
	if s.setupValid(token) {
		t.Fatal("setup credential survived initialization")
	}
	return owner, codes
}
func TestTOTPReplayAndRecoveryCodeConsumption(t *testing.T) {
	seed := []byte("12345678901234567890")
	if got := TOTP(seed, 1); got != "287082" {
		t.Fatalf("RFC6238 mismatch: %s", got)
	}
	if _, ok := verifyTOTP(seed, "287082", time.Unix(59, 0), 1); ok {
		t.Fatal("reused TOTP accepted")
	}
	s := testStore(t)
	owner, codes := setupTestOwner(t, s)
	if len(codes) != 10 {
		t.Fatal("missing recovery codes")
	}
	if _, e := s.Login("owner", "test-password-at-least-12", "", codes[0], "127.0.0.1"); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Login("owner", "test-password-at-least-12", "", codes[0], "127.0.0.1"); e == nil {
		t.Fatal("recovery code used twice")
	}
	session, token, e := s.NewSession(owner, "test device")
	if e != nil {
		t.Fatal(e)
	}
	r := httptest.NewRequest("GET", "https://bot.example.invalid/api/runtime", nil)
	r.AddCookie(&http.Cookie{Name: cookieName, Value: token})
	if got, e := s.Authenticate(r); e != nil || got.ID != session.ID {
		t.Fatal("valid session rejected", e)
	}
	if e = s.RevokeAll("test"); e != nil {
		t.Fatal(e)
	}
	if _, e = s.Authenticate(r); e == nil {
		t.Fatal("revoked session survived")
	}
	var encrypted []byte
	if e = s.DB.QueryRow("SELECT totp FROM owner").Scan(&encrypted); e != nil {
		t.Fatal(e)
	}
	if bytes.Contains(encrypted, seed) {
		t.Fatal("TOTP stored as plaintext")
	}
}
func testGateway(t *testing.T) (*Gateway, *fakeManager, *http.Cookie, string) {
	t.Helper()
	l, m, _ := testLifecycle(t)
	owner, _ := setupTestOwner(t, l.Store)
	session, token, e := l.Store.NewSession(owner, "test")
	if e != nil {
		t.Fatal(e)
	}
	g, e := NewGateway(l.Config, l.Store, l)
	if e != nil {
		t.Fatal(e)
	}
	return g, m, &http.Cookie{Name: cookieName, Value: token}, session.CSRF
}
func callGateway(g *Gateway, method, target string, body any, cookie *http.Cookie, headers map[string]string) *httptest.ResponseRecorder {
	var raw []byte
	if body != nil {
		raw, _ = json.Marshal(body)
	}
	r := httptest.NewRequest(method, "https://bot.example.invalid"+target, bytes.NewReader(raw))
	if cookie != nil {
		r.AddCookie(cookie)
	}
	for k, v := range headers {
		r.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	g.Handler().ServeHTTP(w, r)
	return w
}
func TestPassiveAndUnauthenticatedRequestsNeverWake(t *testing.T) {
	g, m, cookie, _ := testGateway(t)
	for _, target := range []string{"/api/runtime", "/api/notifications", "/api/auth/status"} {
		if w := callGateway(g, "GET", target, nil, cookie, nil); w.Code != 200 {
			t.Fatal(target, w.Code)
		}
	}
	if w := callGateway(g, "GET", "/api/tasks", nil, nil, nil); w.Code != 401 {
		t.Fatal("unauthenticated runtime request accepted")
	}
	for _, target := range []string{"/api/tasks/one/events", "/api/tasks"} {
		headers := map[string]string{"X-Personal-Bot-Passive": "1"}
		if w := callGateway(g, "GET", target, nil, cookie, headers); w.Code != 503 || !strings.Contains(w.Body.String(), "runtime_sleeping") {
			t.Fatalf("passive request did not sleep: %s", w.Body.String())
		}
	}
	if m.creates != 0 {
		t.Fatal("passive/auth/status woke a container")
	}
}
func TestCSRFOriginAndDeviceRevocation(t *testing.T) {
	g, _, cookie, csrf := testGateway(t)
	if w := callGateway(g, "POST", "/api/auth/logout", map[string]any{}, cookie, nil); w.Code != 403 {
		t.Fatal("mutation without CSRF accepted")
	}
	if w := callGateway(g, "POST", "/api/auth/logout", map[string]any{}, cookie, map[string]string{"X-CSRF-Token": csrf, "Origin": "https://evil.invalid"}); w.Code != 403 {
		t.Fatal("foreign origin accepted")
	}
	if w := callGateway(g, "POST", "/api/auth/logout", map[string]any{}, cookie, map[string]string{"X-CSRF-Token": csrf}); w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if w := callGateway(g, "GET", "/api/runtime", nil, cookie, nil); w.Code != 401 {
		t.Fatal("logged out device retained access")
	}
}
func TestProxyReplacesForgedIdentityAndStripsCredentials(t *testing.T) {
	g, _, cookie, _ := testGateway(t)
	seen := make(chan bool, 1)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		parts := strings.Split(r.Header.Get("X-Personal-Bot-Assertion"), ".")
		valid := len(parts) == 2
		if valid {
			sig, e := base64.RawURLEncoding.DecodeString(parts[1])
			valid = e == nil && ed25519.Verify(g.Store.Secrets.Signing.Public().(ed25519.PublicKey), []byte(parts[0]), sig)
			b, _ := base64.RawURLEncoding.DecodeString(parts[0])
			var claims map[string]any
			_ = json.Unmarshal(b, &claims)
			valid = valid && claims["method"] == r.Method && claims["path"] == r.URL.Path && claims["deployment_epoch"] == g.Store.Secrets.Epoch && claims["session_id"] != ""
		}
		valid = valid && r.Header.Get("Cookie") == "" && r.Header.Get("Authorization") == ""
		seen <- valid
		writeJSON(w, 200, map[string]any{"items": []string{}})
	}))
	defer backend.Close()
	u := backend.URL
	g.Lifecycle.Runtime.URL = u
	g.Config.RuntimeURL = u
	replacement, e := NewGateway(g.Config, g.Store, g.Lifecycle)
	if e != nil {
		t.Fatal(e)
	}
	g.Proxy = replacement.Proxy
	g.Lifecycle.mu.Lock()
	g.Lifecycle.state.State = "READY"
	g.Lifecycle.mu.Unlock()
	w := callGateway(g, "GET", "/api/tasks", nil, cookie, map[string]string{"X-Personal-Bot-Assertion": "forged", "Authorization": "Bearer external-secret"})
	if w.Code != 200 || !<-seen {
		t.Fatalf("identity forwarding failed: %d %s", w.Code, w.Body.String())
	}
}
func TestBridgeRejectsHeaderUIDAndStaleRuntime(t *testing.T) {
	l, _, _ := testLifecycle(t)
	l.mu.Lock()
	l.state.RuntimeID = randomID()
	rid := l.state.RuntimeID
	l.mu.Unlock()
	event := BridgeEvent{ID: "notification-event", Kind: "notification", RuntimeID: rid, Epoch: l.Store.Secrets.Epoch, Payload: json.RawMessage(`{"notification_id":"n1","task_id":"t1","category":"completed"}`)}
	b, _ := json.Marshal(event)
	r := httptest.NewRequest("POST", "http://bridge/internal/events", bytes.NewReader(b))
	r.Header.Set("X-UID", "31001")
	w := httptest.NewRecorder()
	l.BridgeHandler().ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal("header spoofed Unix peer")
	}
	r = httptest.NewRequest("POST", "http://bridge/internal/events", bytes.NewReader(b))
	r = r.WithContext(context.WithValue(r.Context(), peerKey{}, uint32(31001)))
	w = httptest.NewRecorder()
	l.BridgeHandler().ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	event.RuntimeID = randomID()
	b, _ = json.Marshal(event)
	r = httptest.NewRequest("POST", "http://bridge/internal/events", bytes.NewReader(b))
	r = r.WithContext(context.WithValue(r.Context(), peerKey{}, uint32(31001)))
	w = httptest.NewRecorder()
	l.BridgeHandler().ServeHTTP(w, r)
	if w.Code != 409 {
		t.Fatal("stale runtime accepted")
	}
}
