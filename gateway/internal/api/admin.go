package api

import (
	"net/http"
	"time"

	"dsh/openai-register-gateway/internal/auth"
)

func (h *Handlers) Health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "gateway": "go"})
}

func (h *Handlers) AdminStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"enabled": h.Conf.AdminAuthEnabled})
}

type adminLoginBody struct {
	Key string `json:"key"`
}

func (h *Handlers) AdminLogin(w http.ResponseWriter, r *http.Request) {
	var body adminLoginBody
	if err := readBody(r, &body); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	if !h.Conf.AdminAuthEnabled {
		writeDetail(w, http.StatusNotFound, "管理员功能已关闭")
		return
	}
	if h.Conf.AdminAccessKey == "" {
		writeDetail(w, http.StatusServiceUnavailable, "管理员密匙未配置")
		return
	}
	if body.Key != h.Conf.AdminAccessKey {
		writeDetail(w, http.StatusUnauthorized, "管理员密匙错误")
		return
	}
	token, expires, err := h.Secret.IssueSession(time.Now())
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, "会话签发失败")
		return
	}
	auth.SetSessionCookie(w, token, h.Conf.AdminSessionTTL, h.Conf.AdminCookieSecure)
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"authenticated": true,
		"expires_at":    isoUTC(expires),
	})
}

func (h *Handlers) AdminSession(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"authenticated": true,
		"expires_at":    isoUTC(auth.ExpiresFrom(r.Context())),
	})
}

func (h *Handlers) AdminOverview(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"authenticated": true,
		"expires_at":    isoUTC(auth.ExpiresFrom(r.Context())),
		"capabilities":  []string{"账号运维", "注册任务", "OAuth 授权", "Sub2API 管理"},
	})
}

func (h *Handlers) AdminLogout(w http.ResponseWriter, r *http.Request) {
	auth.ClearSessionCookie(w, h.Conf.AdminCookieSecure)
	writeJSON(w, http.StatusOK, map[string]interface{}{"authenticated": false})
}

// isoUTC 与 FastAPI datetime.fromtimestamp(tz=utc).isoformat() 对齐(+00:00)。
func isoUTC(unix int64) string {
	return time.Unix(unix, 0).UTC().Format("2006-01-02T15:04:05+00:00")
}
