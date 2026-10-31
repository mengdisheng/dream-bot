package host

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base32"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"golang.org/x/crypto/argon2"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const cookieName = "__Host-personal_bot"

type Session struct{ ID, OwnerID, Username, CSRF string }

func digest(s string) string { b := sha256.Sum256([]byte(s)); return hex.EncodeToString(b[:]) }
func passwordHash(password string) string {
	salt := make([]byte, 16)
	if _, e := rand.Read(salt); e != nil {
		panic(e)
	}
	h := argon2.IDKey([]byte(password), salt, 3, 32*1024, 1, 32)
	return "argon2id$3$32768$1$" + base64.RawStdEncoding.EncodeToString(salt) + "$" + base64.RawStdEncoding.EncodeToString(h)
}
func passwordMatches(encoded, password string) bool {
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[0] != "argon2id" {
		return false
	}
	rounds, e := strconv.Atoi(parts[1])
	if e != nil || rounds != 3 {
		return false
	}
	memory, e := strconv.Atoi(parts[2])
	if e != nil || memory != 32768 {
		return false
	}
	if parts[3] != "1" {
		return false
	}
	salt, e := base64.RawStdEncoding.DecodeString(parts[4])
	if e != nil || len(salt) != 16 {
		return false
	}
	expected, e := base64.RawStdEncoding.DecodeString(parts[5])
	if e != nil || len(expected) != 32 {
		return false
	}
	h := argon2.IDKey([]byte(password), salt, uint32(rounds), uint32(memory), 1, 32)
	return subtle.ConstantTimeCompare(h, expected) == 1
}

func TOTP(seed []byte, step int64) string {
	m := hmac.New(sha1.New, seed)
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(step))
	m.Write(b[:])
	v := m.Sum(nil)
	i := v[len(v)-1] & 15
	n := (binary.BigEndian.Uint32(v[i:i+4]) & 0x7fffffff) % 1000000
	return fmt.Sprintf("%06d", n)
}
func verifyTOTP(seed []byte, code string, now time.Time, last int64) (int64, bool) {
	if len(code) != 6 {
		return 0, false
	}
	step := now.Unix() / 30
	for _, v := range []int64{step, step - 1, step + 1} {
		if v > last && subtle.ConstantTimeCompare([]byte(TOTP(seed, v)), []byte(code)) == 1 {
			return v, true
		}
	}
	return 0, false
}

func (s *Store) SetupToken() (string, error) {
	if s.Initialized() {
		return "", errors.New("owner already initialized")
	}
	p := filepath.Join(s.Dir, "setup.token")
	b, e := os.ReadFile(p)
	if os.IsNotExist(e) {
		b = []byte(randomID())
		e = privateFile(p, b)
	}
	return strings.TrimSpace(string(b)), e
}
func (s *Store) setupValid(token string) bool {
	expected, e := s.SetupToken()
	return e == nil && len(token) >= 24 && subtle.ConstantTimeCompare([]byte(expected), []byte(token)) == 1
}

func (s *Store) Enrollment(token, username string) (map[string]any, error) {
	if !s.setupValid(token) {
		return nil, errors.New("invalid setup credential")
	}
	if len(username) < 1 || len(username) > 80 {
		return nil, errors.New("username must contain 1–80 characters")
	}
	seed := make([]byte, 20)
	if _, e := rand.Read(seed); e != nil {
		return nil, e
	}
	id := randomID()
	enc, e := s.Secrets.Encrypt(seed, "totp-enrollment:"+id)
	if e != nil {
		return nil, e
	}
	_, e = s.DB.Exec("INSERT INTO enrollment(id,username,totp,expires) VALUES(?,?,?,?)", id, username, enc, time.Now().Add(10*time.Minute).Unix())
	if e != nil {
		return nil, e
	}
	secret := base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString(seed)
	uri := "otpauth://totp/" + url.PathEscape("Personal Bot:"+username) + "?secret=" + secret + "&issuer=Personal%20Bot&algorithm=SHA1&digits=6&period=30"
	return map[string]any{"enrollment_id": id, "totp_uri": uri, "totp_secret": secret}, nil
}

func (s *Store) Setup(token, enrollmentID, username, password, code string) (Session, []string, error) {
	var session Session
	if !s.setupValid(token) {
		return session, nil, errors.New("invalid setup credential")
	}
	if len(password) < 12 || len(password) > 1024 {
		return session, nil, errors.New("password must contain 12–1024 bytes")
	}
	tx, e := s.DB.Begin()
	if e != nil {
		return session, nil, e
	}
	defer tx.Rollback()
	var n int
	if e = tx.QueryRow("SELECT count(*) FROM owner").Scan(&n); e != nil || n != 0 {
		return session, nil, errors.New("owner already initialized")
	}
	var enc []byte
	var stored string
	var expires int64
	if e = tx.QueryRow("SELECT username,totp,expires FROM enrollment WHERE id=?", enrollmentID).Scan(&stored, &enc, &expires); e != nil || stored != username || expires < time.Now().Unix() {
		return session, nil, errors.New("enrollment expired")
	}
	seed, e := s.Secrets.Decrypt(enc, "totp-enrollment:"+enrollmentID)
	if e != nil {
		return session, nil, e
	}
	step, ok := verifyTOTP(seed, code, time.Now(), 0)
	if !ok {
		return session, nil, errors.New("invalid authenticator code")
	}
	ownerID := randomID()
	enc, e = s.Secrets.Encrypt(seed, "totp:"+ownerID)
	if e != nil {
		return session, nil, e
	}
	if _, e = tx.Exec("INSERT INTO owner(id,username,password,totp,last_totp_step) VALUES(?,?,?,?,?)", ownerID, username, passwordHash(password), enc, step); e != nil {
		return session, nil, e
	}
	codes := make([]string, 10)
	for i := range codes {
		codes[i] = randomID()
		if _, e = tx.Exec("INSERT INTO recovery_codes(hash,owner_id) VALUES(?,?)", digest(codes[i]), ownerID); e != nil {
			return session, nil, e
		}
	}
	if _, e = tx.Exec("DELETE FROM enrollment"); e != nil {
		return session, nil, e
	}
	if _, e = tx.Exec("INSERT INTO audit(kind,subject,created_at) VALUES('owner.setup',?,?)", ownerID, timestamp(time.Now())); e != nil {
		return session, nil, e
	}
	if e = tx.Commit(); e != nil {
		return session, nil, e
	}
	_ = os.Remove(filepath.Join(s.Dir, "setup.token"))
	return Session{OwnerID: ownerID, Username: username}, codes, nil
}

func (s *Store) Login(username, password, code, recovery, remote string) (Session, error) {
	var out Session
	now := time.Now()
	key := digest(remote + "|" + username)
	var count, reset int64
	_ = s.DB.QueryRow("SELECT count,reset_at FROM login_limits WHERE key=?", key).Scan(&count, &reset)
	if reset > now.Unix() && count >= 8 {
		return out, errors.New("too many attempts; try again later")
	}
	fail := func() (Session, error) {
		_, _ = s.DB.Exec("INSERT INTO login_limits(key,count,reset_at) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN reset_at<? THEN 1 ELSE count+1 END,reset_at=CASE WHEN reset_at<? THEN excluded.reset_at ELSE reset_at END", key, now.Add(15*time.Minute).Unix(), now.Unix(), now.Unix())
		return out, errors.New("invalid credentials")
	}
	var hash string
	var enc []byte
	var last int64
	e := s.DB.QueryRow("SELECT id,username,password,totp,last_totp_step FROM owner WHERE username=?", username).Scan(&out.OwnerID, &out.Username, &hash, &enc, &last)
	if e != nil {
		passwordMatches("argon2id$3$32768$1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", password)
		return fail()
	}
	if !passwordMatches(hash, password) {
		return fail()
	}
	seed, e := s.Secrets.Decrypt(enc, "totp:"+out.OwnerID)
	if e != nil {
		return out, errors.New("authentication secret unavailable")
	}
	tx, e := s.DB.Begin()
	if e != nil {
		return out, e
	}
	defer tx.Rollback()
	if recovery != "" {
		r, e := tx.Exec("DELETE FROM recovery_codes WHERE hash=? AND owner_id=?", digest(recovery), out.OwnerID)
		if e != nil {
			return out, e
		}
		n, _ := r.RowsAffected()
		if n != 1 {
			tx.Rollback()
			return fail()
		}
		_, _ = tx.Exec("INSERT INTO audit(kind,subject,created_at) VALUES('owner.recovery-code',?,?)", out.OwnerID, timestamp(now))
	} else {
		step, ok := verifyTOTP(seed, code, now, last)
		if !ok {
			tx.Rollback()
			return fail()
		}
		r, e := tx.Exec("UPDATE owner SET last_totp_step=? WHERE id=? AND last_totp_step<?", step, out.OwnerID, step)
		if e != nil {
			return out, e
		}
		n, _ := r.RowsAffected()
		if n != 1 {
			tx.Rollback()
			return fail()
		}
	}
	if _, e = tx.Exec("DELETE FROM login_limits WHERE key=?", key); e != nil {
		return out, e
	}
	if e = tx.Commit(); e != nil {
		return out, e
	}
	return out, nil
}

func (s *Store) NewSession(owner Session, agent string) (Session, string, error) {
	owner.ID = randomID()
	owner.CSRF = randomID()
	token := randomID()
	now := time.Now()
	if len(agent) > 300 {
		agent = agent[:300]
	}
	_, e := s.DB.Exec("INSERT INTO sessions(id,token_hash,owner_id,csrf,created_at,last_seen_at,expires,user_agent) VALUES(?,?,?,?,?,?,?,?)", owner.ID, digest(token), owner.OwnerID, owner.CSRF, timestamp(now), timestamp(now), now.Add(7*24*time.Hour).Unix(), agent)
	return owner, token, e
}
func (s *Store) Authenticate(r *http.Request) (Session, error) {
	var out Session
	c, e := r.Cookie(cookieName)
	if e != nil {
		return out, e
	}
	e = s.DB.QueryRow("SELECT s.id,s.owner_id,o.username,s.csrf FROM sessions s JOIN owner o ON o.id=s.owner_id WHERE s.token_hash=? AND s.expires>?", digest(c.Value), time.Now().Unix()).Scan(&out.ID, &out.OwnerID, &out.Username, &out.CSRF)
	if e != nil {
		return out, e
	}
	_, e = s.DB.Exec("UPDATE sessions SET last_seen_at=? WHERE id=?", timestamp(time.Now()), out.ID)
	return out, e
}
func sessionCookie(w http.ResponseWriter, token string) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: token, Path: "/", Secure: true, HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: 7 * 24 * 3600})
}
func clearCookie(w http.ResponseWriter) {
	http.SetCookie(w, &http.Cookie{Name: cookieName, Value: "", Path: "/", Secure: true, HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: -1})
}
func remoteHost(r *http.Request) string {
	h, _, e := net.SplitHostPort(r.RemoteAddr)
	if e != nil {
		return r.RemoteAddr
	}
	return h
}
func csrfValid(r *http.Request, s Session) bool {
	v := r.Header.Get("X-CSRF-Token")
	return len(v) > 20 && subtle.ConstantTimeCompare([]byte(v), []byte(s.CSRF)) == 1
}

func (s *Store) RevokeAll(reason string) error {
	tx, e := s.DB.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	if _, e = tx.Exec("DELETE FROM sessions"); e != nil {
		return e
	}
	if _, e = tx.Exec("INSERT INTO audit(kind,subject,created_at) VALUES('sessions.revoke-all',?,?)", reason, timestamp(time.Now())); e != nil {
		return e
	}
	return tx.Commit()
}
