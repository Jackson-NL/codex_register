// Package auth 复刻 FastAPI 的 HMAC-SHA256 签名会话(与现有 cookie 完全兼容,
// 后端重启后可验证,改密匙即失效)。
package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const CookieName = "accountops_admin_session"

// Secret 登录用的管理员密匙(对应 ADMIN_ACCESS_KEY)。
type Secret struct {
	Key         string
	Enabled     bool
	TTL         int // 秒
	CookieSecure bool
}

func signature(payload, key string) string {
	h := hmac.New(sha256.New, []byte(key))
	h.Write([]byte(payload))
	return strings.TrimRight(base64.URLEncoding.EncodeToString(h.Sum(nil)), "=")
}

func randomToken(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(b), nil
}

// IssueSession 签发 "{issued}.{rand}.{sig}";返回 token 与过期时间戳。
func (s *Secret) IssueSession(now time.Time) (string, int64, error) {
	randPart, err := randomToken(18)
	if err != nil {
		return "", 0, err
	}
	payload := fmt.Sprintf("%d.%s", now.Unix(), randPart)
	expires := now.Unix() + int64(s.ttl())
	return payload + "." + signature(payload, s.Key), expires, nil
}

func (s *Secret) ttl() int {
	if s.TTL < 60 {
		return 28800
	}
	return s.TTL
}

// VerifySession 校验 cookie 票据;返回过期时间戳,失败返回 0。
func (s *Secret) VerifySession(token string, now time.Time) int64 {
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" {
		return 0
	}
	issuedAt, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return 0
	}
	cur := now.Unix()
	ttl := int64(s.ttl())
	if issuedAt > cur+60 || cur-issuedAt > ttl {
		return 0
	}
	expected := signature(strings.Join(parts[:2], "."), s.Key)
	if !hmac.Equal([]byte(parts[2]), []byte(expected)) {
		return 0
	}
	return issuedAt + ttl
}

// RequireAdmin 中间件:admin_auth_enabled 关闭时 404;无有效票据时 401。
func (s *Secret) RequireAdmin(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !s.Enabled {
			writeDetail(w, http.StatusNotFound, "管理员功能已关闭")
			return
		}
		if s.Key == "" {
			writeDetail(w, http.StatusServiceUnavailable, "管理员密匙未配置")
			return
		}
		cookie, err := r.Cookie(CookieName)
		expires := int64(0)
		if err == nil {
			expires = s.VerifySession(cookie.Value, time.Now())
		}
		if expires == 0 {
			writeDetail(w, http.StatusUnauthorized, "需要管理员授权")
			return
		}
		r = withExpires(r, expires)
		next.ServeHTTP(w, r)
	})
}

func withExpires(r *http.Request, expires int64) *http.Request {
	return r.WithContext(ctxWithExpires(r.Context(), expires))
}
func SetSessionCookie(w http.ResponseWriter, token string, maxAge int, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    token,
		Path:     "/",
		MaxAge:   maxAge,
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   secure,
	})
}

func ClearSessionCookie(w http.ResponseWriter, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     CookieName,
		Value:    "",
		Path:     "/",
		MaxAge:   -1,
		HttpOnly: true,
		SameSite: http.SameSiteLaxMode,
		Secure:   secure,
	})
}
