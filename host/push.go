package host

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	webpush "github.com/SherClockHolmes/webpush-go"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type PushService struct {
	Store                    *Store
	Public, private, Contact string
	HTTP                     *http.Client
}

func NewPushService(s *Store, c Config) (*PushService, error) {
	p := &PushService{Store: s, Contact: c.PushContact}
	var encoded string
	e := s.DB.QueryRow("SELECT value FROM settings WHERE key='vapid'").Scan(&encoded)
	if e == nil {
		b, e := base64.RawStdEncoding.DecodeString(encoded)
		if e != nil {
			return nil, e
		}
		b, e = s.Secrets.Decrypt(b, "vapid")
		if e != nil {
			return nil, e
		}
		var v struct{ Public, Private string }
		if e = json.Unmarshal(b, &v); e != nil {
			return nil, e
		}
		p.Public, p.private = v.Public, v.Private
	} else {
		if c.VAPIDPublic != "" && c.VAPIDPrivateFile != "" {
			b, e := os.ReadFile(c.VAPIDPrivateFile)
			if e != nil {
				return nil, e
			}
			p.Public = c.VAPIDPublic
			p.private = strings.TrimSpace(string(b))
		} else {
			private, public, e := webpush.GenerateVAPIDKeys()
			if e != nil {
				return nil, e
			}
			p.Public, p.private = public, private
		}
		b, _ := json.Marshal(map[string]string{"Public": p.Public, "Private": p.private})
		b, e = s.Secrets.Encrypt(b, "vapid")
		if e != nil {
			return nil, e
		}
		if _, e = s.DB.Exec("INSERT INTO settings(key,value) VALUES('vapid',?)", base64.RawStdEncoding.EncodeToString(b)); e != nil {
			return nil, e
		}
	}
	p.HTTP = &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
		host, port, e := net.SplitHostPort(address)
		if e != nil {
			return nil, e
		}
		ips, e := net.DefaultResolver.LookupIPAddr(ctx, host)
		if e != nil {
			return nil, e
		}
		if len(ips) == 0 {
			return nil, errors.New("push hostname has no addresses")
		}
		for _, ip := range ips {
			if !publicIP(ip.IP) {
				return nil, errors.New("push target is not a public address")
			}
		}
		var last error
		for _, ip := range ips {
			c, e := (&net.Dialer{Timeout: 8 * time.Second}).DialContext(ctx, network, net.JoinHostPort(ip.IP.String(), port))
			if e == nil {
				return c, nil
			}
			last = e
		}
		return nil, last
	}, TLSHandshakeTimeout: 8 * time.Second, MaxIdleConnsPerHost: 2}, Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("push redirects are disabled") }}
	return p, nil
}
func publicIP(ip net.IP) bool {
	return ip.IsGlobalUnicast() && !ip.IsPrivate() && !ip.IsLoopback() && !ip.IsLinkLocalUnicast() && !ip.IsLinkLocalMulticast() && !ip.IsUnspecified()
}
func (p *PushService) Subscribe(v webpush.Subscription) (string, error) {
	u, e := url.Parse(v.Endpoint)
	if e != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || len(v.Endpoint) > 3000 {
		return "", errors.New("invalid HTTPS push endpoint")
	}
	if ip := net.ParseIP(u.Hostname()); ip != nil && !publicIP(ip) {
		return "", errors.New("push endpoint must be public")
	}
	key, e := base64.RawURLEncoding.DecodeString(v.Keys.P256dh)
	if e != nil || len(key) != 65 {
		return "", errors.New("invalid push encryption key")
	}
	auth, e := base64.RawURLEncoding.DecodeString(v.Keys.Auth)
	if e != nil || len(auth) != 16 {
		return "", errors.New("invalid push auth secret")
	}
	b, e := json.Marshal(v)
	if e != nil {
		return "", e
	}
	hash := digest(v.Endpoint)
	var existing string
	_ = p.Store.DB.QueryRow("SELECT id FROM push_subscriptions WHERE endpoint_hash=?", hash).Scan(&existing)
	id := existing
	if id == "" {
		id = randomID()
	}
	b, e = p.Store.Secrets.Encrypt(b, "push:"+id)
	if e != nil {
		return "", e
	}
	_, e = p.Store.DB.Exec("INSERT INTO push_subscriptions(id,endpoint_hash,subscription,created_at) VALUES(?,?,?,?) ON CONFLICT(endpoint_hash) DO UPDATE SET subscription=excluded.subscription,disabled=0", id, hash, b, timestamp(time.Now()))
	return id, e
}
func (p *PushService) Deliver(ctx context.Context) error {
	if p.Contact == "" {
		return nil
	}
	rows, e := p.Store.DB.Query("SELECT d.notification_id,d.subscription_id,d.attempts,s.subscription,n.created_at FROM push_deliveries d JOIN push_subscriptions s ON s.id=d.subscription_id JOIN notifications n ON n.id=d.notification_id WHERE d.state='pending' AND d.next_attempt<=? AND s.disabled=0 LIMIT 10", time.Now().Unix())
	if e != nil {
		return e
	}
	type delivery struct {
		notification, subscription string
		attempts                   int
		encrypted                  []byte
		created                    string
	}
	var items []delivery
	for rows.Next() {
		var v delivery
		if e = rows.Scan(&v.notification, &v.subscription, &v.attempts, &v.encrypted, &v.created); e != nil {
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
		created, _ := time.Parse(time.RFC3339Nano, v.created)
		if v.attempts >= 6 || time.Since(created) > 24*time.Hour {
			_, e = p.Store.DB.Exec("UPDATE push_deliveries SET state='expired' WHERE notification_id=? AND subscription_id=?", v.notification, v.subscription)
			if e != nil {
				return e
			}
			continue
		}
		b, e := p.Store.Secrets.Decrypt(v.encrypted, "push:"+v.subscription)
		if e != nil {
			return e
		}
		var sub webpush.Subscription
		if e = json.Unmarshal(b, &sub); e != nil {
			return e
		}
		message, _ := json.Marshal(map[string]string{"title": "个人 Bot", "body": "有新的任务状态，请登录查看。", "url": "/tasks", "notification_id": v.notification})
		resp, sendErr := webpush.SendNotificationWithContext(ctx, message, &sub, &webpush.Options{Subscriber: p.Contact, VAPIDPublicKey: p.Public, VAPIDPrivateKey: p.private, TTL: 3600, HTTPClient: p.HTTP, Topic: digest(v.notification)[:32]})
		status := 0
		if resp != nil {
			status = resp.StatusCode
			io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
			resp.Body.Close()
		}
		if sendErr == nil && status >= 200 && status < 300 {
			_, e = p.Store.DB.Exec("UPDATE push_deliveries SET state='sent' WHERE notification_id=? AND subscription_id=?", v.notification, v.subscription)
		} else if status == 404 || status == 410 {
			tx, err := p.Store.DB.Begin()
			if err != nil {
				return err
			}
			_, e = tx.Exec("UPDATE push_subscriptions SET disabled=1 WHERE id=?", v.subscription)
			if e == nil {
				_, e = tx.Exec("UPDATE push_deliveries SET state='invalid_subscription' WHERE subscription_id=?", v.subscription)
			}
			if e == nil {
				e = tx.Commit()
			} else {
				tx.Rollback()
			}
		} else {
			delay := time.Duration(1<<min(v.attempts+3, 10)) * time.Second
			_, e = p.Store.DB.Exec("UPDATE push_deliveries SET attempts=attempts+1,next_attempt=? WHERE notification_id=? AND subscription_id=?", time.Now().Add(delay).Unix(), v.notification, v.subscription)
		}
		if e != nil {
			return e
		}
	}
	return nil
}
