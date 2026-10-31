package host

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

type Config struct {
	Listen           string `json:"listen"`
	Origin           string `json:"origin"`
	StateDir         string `json:"state_dir"`
	StaticDir        string `json:"static_dir"`
	HelperSocket     string `json:"helper_socket"`
	BridgeSocket     string `json:"bridge_socket"`
	RuntimeURL       string `json:"runtime_url"`
	IdleSeconds      int    `json:"idle_seconds"`
	StartupSeconds   int    `json:"startup_seconds"`
	VAPIDPublic      string `json:"vapid_public"`
	VAPIDPrivateFile string `json:"vapid_private_file"`
	PushContact      string `json:"push_contact"`
	BackupRecipient  string `json:"backup_recipient"`
	AdminSocket      string `json:"admin_socket"`
	APIPeerUID       uint32 `json:"api_peer_uid"`
	BackupDir        string `json:"backup_dir"`
	GatewayUID       uint32 `json:"gateway_uid"`
	IPCGID           int    `json:"ipc_gid"`
	HostReserveBytes int64  `json:"host_reserve_bytes"`
	ProjectPeakBytes int64  `json:"project_peak_bytes"`
	ProjectSlice     string `json:"project_slice"`
}

func LoadConfig(path string) (Config, error) {
	c := Config{Listen: "127.0.0.1:8090", StateDir: "/var/lib/personal-bot/control", HelperSocket: "/run/personal-bot-host/helper.sock", BridgeSocket: "/run/personal-bot-bridge/bridge.sock", RuntimeURL: "http://127.0.0.1:8091", IdleSeconds: 300, StartupSeconds: 60, AdminSocket: "/run/personal-bot-gateway/admin.sock", APIPeerUID: 31001, BackupDir: "/var/lib/personal-bot/backups", GatewayUID: 31010, IPCGID: 31000, HostReserveBytes: 768 << 20, ProjectPeakBytes: 2432 << 20, ProjectSlice: "personal-bot.slice"}
	b, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	if err = json.Unmarshal(b, &c); err != nil {
		return c, err
	}
	if c.Origin == "" || c.StateDir == "" || c.StaticDir == "" {
		return c, errors.New("origin, state_dir and static_dir are required")
	}
	if c.IdleSeconds < 1 || c.StartupSeconds < 1 {
		return c, errors.New("timeouts must be positive")
	}
	if err = validateMemoryGuard(c); err != nil {
		return c, err
	}
	return c, nil
}

type Secrets struct {
	Key     []byte
	Signing ed25519.PrivateKey
	Epoch   string
}

func randomID() string {
	b := make([]byte, 24)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}
func newUUID() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}
func timestamp(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func privateFile(path string, data []byte) error {
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	if _, e = f.Write(data); e != nil {
		return e
	}
	return f.Sync()
}

func LoadSecrets(dir string) (Secrets, error) {
	var s Secrets
	if e := os.MkdirAll(dir, 0700); e != nil {
		return s, e
	}
	if e := os.Chmod(dir, 0700); e != nil {
		return s, e
	}
	keyPath := filepath.Join(dir, "master.key")
	b, e := os.ReadFile(keyPath)
	if os.IsNotExist(e) {
		b = make([]byte, 32)
		_, e = rand.Read(b)
		if e == nil {
			e = privateFile(keyPath, b)
		}
	}
	if e != nil {
		return s, e
	}
	if len(b) != 32 {
		return s, errors.New("invalid master key")
	}
	s.Key = b
	p := filepath.Join(dir, "assertion.key")
	b, e = os.ReadFile(p)
	if os.IsNotExist(e) {
		_, s.Signing, e = ed25519.GenerateKey(rand.Reader)
		if e != nil {
			return s, e
		}
		der, err := x509.MarshalPKCS8PrivateKey(s.Signing)
		if err != nil {
			return s, err
		}
		b = pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
		e = privateFile(p, b)
	}
	if e != nil {
		return s, e
	}
	block, _ := pem.Decode(b)
	if block == nil {
		return s, errors.New("invalid assertion key")
	}
	pk, e := x509.ParsePKCS8PrivateKey(block.Bytes)
	if e != nil {
		return s, e
	}
	var ok bool
	s.Signing, ok = pk.(ed25519.PrivateKey)
	if !ok {
		return s, errors.New("assertion key must be Ed25519")
	}
	pub, e := x509.MarshalPKIXPublicKey(s.Signing.Public())
	if e != nil {
		return s, e
	}
	pubPath := filepath.Join(dir, "assertion.pub")
	if _, e = os.Stat(pubPath); os.IsNotExist(e) {
		e = privateFile(pubPath, pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pub}))
		if e != nil {
			return s, e
		}
	}
	ep := filepath.Join(dir, "deployment_epoch")
	b, e = os.ReadFile(ep)
	if os.IsNotExist(e) {
		b = []byte(randomID())
		e = privateFile(ep, b)
	}
	if e != nil {
		return s, e
	}
	s.Epoch = string(b)
	if len(s.Epoch) < 20 {
		return s, errors.New("invalid deployment epoch")
	}
	return s, nil
}

func (s Secrets) Encrypt(b []byte, aad string) ([]byte, error) {
	c, e := aes.NewCipher(s.Key)
	if e != nil {
		return nil, e
	}
	g, e := cipher.NewGCM(c)
	if e != nil {
		return nil, e
	}
	nonce := make([]byte, g.NonceSize())
	if _, e = rand.Read(nonce); e != nil {
		return nil, e
	}
	return g.Seal(nonce, nonce, b, []byte(aad)), nil
}
func (s Secrets) Decrypt(b []byte, aad string) ([]byte, error) {
	c, e := aes.NewCipher(s.Key)
	if e != nil {
		return nil, e
	}
	g, e := cipher.NewGCM(c)
	if e != nil {
		return nil, e
	}
	if len(b) < g.NonceSize() {
		return nil, errors.New("invalid encrypted secret")
	}
	return g.Open(nil, b[:g.NonceSize()], b[g.NonceSize():], []byte(aad))
}
