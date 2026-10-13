// proxies / settings / mailConfig / gmailSessions 端点。
package api

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"time"

	"dsh/openai-register-gateway/internal/db"
)

func randomHex() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func mustURL(raw string) *url.URL {
	u, _ := url.Parse(raw)
	return u
}

func jsonUnmarshal(data []byte, v interface{}) error {
	return json.Unmarshal(data, v)
}

// ------------------------------------------------------------ proxies
func (h *Handlers) registerProxies(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/proxies", h.ProxiesList)
	mux.HandleFunc("POST /api/proxies", h.ProxiesCreate)
	mux.HandleFunc("PATCH /api/proxies/{id}", h.ProxiesPatch)
	mux.HandleFunc("DELETE /api/proxies/{id}", h.ProxiesDelete)
	mux.HandleFunc("POST /api/proxies/{id}/test", h.ProxiesTest)
}

func (h *Handlers) ProxiesList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	where, args := []string{"1=1"}, []interface{}{}
	if v := q.Get("status"); v != "" {
		where = append(where, "status=?")
		args = append(args, v)
	}
	if v := q.Get("q"); v != "" {
		where = append(where, "url LIKE ?")
		args = append(args, "%"+v+"%")
	}
	items, err := db.List[db.Proxy](h.Store, "SELECT * FROM proxies WHERE "+strings.Join(where, " AND ")+" ORDER BY id", args...)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) ProxiesCreate(w http.ResponseWriter, r *http.Request) {
	var b struct {
		URL     string `json:"url"`
		Country string `json:"country"`
	}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	res, err := h.Store.Exec("INSERT INTO proxies(url,country,status,used_count) VALUES(?,?,'ok',0)", b.URL, b.Country)
	if err != nil {
		writeDetail(w, http.StatusBadRequest, "代理已存在或参数非法")
		return
	}
	id, _ := res.LastInsertId()
	writeJSON(w, http.StatusOK, map[string]interface{}{"id": id, "url": b.URL, "country": b.Country, "status": "ok"})
}

func (h *Handlers) ProxiesPatch(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	sets, args := []string{}, []interface{}{}
	for _, k := range []string{"url", "country", "status"} {
		if v, ok := b[k]; ok {
			sets = append(sets, k+"=?")
			args = append(args, v)
		}
	}
	if len(sets) > 0 {
		args = append(args, pathID(r))
		_, _ = h.Store.Exec("UPDATE proxies SET "+strings.Join(sets, ",")+" WHERE id=?", args...)
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) ProxiesDelete(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("DELETE FROM proxies WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) ProxiesTest(w http.ResponseWriter, r *http.Request) {
	// 经代理探测目标延迟(简单实现:用代理 GET 一个 URL,量往返时间)。
	p, err := db.One[db.Proxy](h.Store, "SELECT * FROM proxies WHERE id=?", pathID(r))
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "代理不存在")
		return
	}
	start := time.Now()
	done := make(chan error, 1)
	go func() {
		req, _ := http.NewRequest("GET", "http://www.gstatic.com/generate_204", nil)
		tr := &http.Transport{Proxy: http.ProxyURL(mustURL(p.URL))}
		client := &http.Client{Transport: tr, Timeout: 8 * time.Second}
		_, err := client.Do(req)
		done <- err
	}()
	select {
	case err := <-done:
		ms := time.Since(start).Milliseconds()
		status := "ok"
		detail := "ok"
		if err != nil {
			status = "failed"
			detail = err.Error()
		}
		_, _ = h.Store.Exec("UPDATE proxies SET status=?, last_used_at=datetime('now') WHERE id=?", status, pathID(r))
		writeJSON(w, http.StatusOK, map[string]interface{}{"delay_ms": ms, "status": status, "detail": detail})
	case <-time.After(10 * time.Second):
		writeJSON(w, http.StatusOK, map[string]interface{}{"delay_ms": -1, "status": "failed", "detail": "timeout"})
	}
}

// ------------------------------------------------------------ settings
func (h *Handlers) registerSettings(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/settings/ui", h.SettingsUiGet)
	mux.HandleFunc("PUT /api/settings/ui", h.SettingsUiPut)
	mux.HandleFunc("GET /api/settings", h.SettingsGet)
	mux.HandleFunc("POST /api/settings", h.SettingsPut)
	mux.HandleFunc("POST /api/settings/smsbower/test", h.SettingsSmsbowerTest)
}

func (h *Handlers) uiValue(w http.ResponseWriter, r *http.Request) (string, bool) {
	var v string
	err := h.Store.QueryRow("SELECT value FROM ui_settings WHERE key='ui'").Scan(&v)
	if err == sql.ErrNoRows {
		writeJSON(w, http.StatusOK, map[string]interface{}{})
		return "", false
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return "", false
	}
	return v, true
}

func (h *Handlers) SettingsUiGet(w http.ResponseWriter, r *http.Request) {
	v, ok := h.uiValue(w, r)
	if !ok {
		return
	}
	var obj map[string]interface{}
	if err := json.Unmarshal([]byte(v), &obj); err != nil || obj == nil {
		obj = map[string]interface{}{}
	}
	writeJSON(w, http.StatusOK, obj)
}

func (h *Handlers) SettingsUiPut(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	raw, _ := json.Marshal(b)
	_, _ = h.Store.Exec("INSERT INTO ui_settings(key,value) VALUES('ui',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(raw))
	writeJSON(w, http.StatusOK, b)
}

func (h *Handlers) SettingsGet(w http.ResponseWriter, r *http.Request) {
	// 全量设置:返回 config 关键字段子集(与现有 FastAPI settings 模型字段名对齐)。
	writeJSON(w, http.StatusOK, map[string]interface{}{
		"admin_auth_enabled":        h.Conf.AdminAuthEnabled,
		"sub2api_base_url":          h.Conf.Sub2APIBaseURL,
		"sub2api_group_ids":         h.Conf.Sub2APIGroupIDs,
		"registration_country_iso":  "BR",
		"registration_country_dialing_code": "55",
		"smsbower_country":          73,
	})
}

func (h *Handlers) SettingsPut(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	_ = readBody(r, &b)
	raw, _ := json.Marshal(b)
	_, _ = h.Store.Exec("INSERT INTO ui_settings(key,value) VALUES('settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(raw))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) SettingsSmsbowerTest(w http.ResponseWriter, r *http.Request) {
	_ = h.Sched.Dispatch(r.Context(), "sms_" + randomHex(), "smsbower_test", map[string]interface{}{})
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "detail": "已派发 worker 测试"})
}

// ------------------------------------------------------------ mailConfig
func (h *Handlers) registerMailConfig(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/mail-config", h.MailConfigGet)
	mux.HandleFunc("POST /api/mail-config", h.MailConfigSave)
	mux.HandleFunc("POST /api/mail-config/test", h.MailConfigTest)
	mux.HandleFunc("POST /api/mail-config/pool/requeue", h.MailPoolAction)
	mux.HandleFunc("POST /api/mail-config/pool/release", h.MailPoolAction)
	mux.HandleFunc("POST /api/mail-config/pool/disable", h.MailPoolAction)
	mux.HandleFunc("POST /api/mail-config/pool/enable", h.MailPoolAction)
	mux.HandleFunc("POST /api/mail-config/pool/verify", h.MailPoolAction)
	mux.HandleFunc("POST /api/mail-config/pool/remove", h.MailPoolAction)
}

func (h *Handlers) MailConfigGet(w http.ResponseWriter, r *http.Request) {
	v, ok := h.uiValue(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ui": json.RawMessage(v)})
}

func (h *Handlers) MailConfigSave(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	raw, _ := json.Marshal(b)
	_, _ = h.Store.Exec("INSERT INTO ui_settings(key,value) VALUES('mail_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", string(raw))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) MailConfigTest(w http.ResponseWriter, r *http.Request) {
	_ = h.Sched.Dispatch(r.Context(), "mail_"+randomHex(), "mail_test", map[string]interface{}{})
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "detail": "已派发 worker 测试"})
}

func (h *Handlers) MailPoolAction(w http.ResponseWriter, r *http.Request) {
	var b struct {
		ID     int64  `json:"id"`
		IDs    []int64 `json:"ids"`
		Reason string `json:"reason_code"`
	}
	_ = readBody(r, &b)
	ids := b.IDs
	if len(ids) == 0 && b.ID > 0 {
		ids = []int64{b.ID}
	}
	action := strings.TrimPrefix(r.URL.Path, "/api/mail-config/pool/")
	updated := int64(0)
	for _, id := range ids {
		switch action {
		case "requeue":
			updated += execRows(h.Store, "UPDATE custom_mailboxes SET status='unused', lease_owner='', allocated_at=NULL WHERE id=?", id)
		case "release":
			updated += execRows(h.Store, "UPDATE custom_mailboxes SET lease_owner='', allocated_at=NULL WHERE id=? AND status='in_use'", id)
		case "disable":
			updated += execRows(h.Store, "UPDATE custom_mailboxes SET active=0 WHERE id=?", id)
		case "enable":
			updated += execRows(h.Store, "UPDATE custom_mailboxes SET active=1 WHERE id=?", id)
		case "verify":
			updated += execRows(h.Store, "UPDATE custom_mailboxes SET status='unused', reason_code='' WHERE id=?", id)
		case "remove":
			updated += execRows(h.Store, "DELETE FROM custom_mailboxes WHERE id=?", id)
		}
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "updated": updated})
}

func execRows(st *db.Store, q string, args ...interface{}) int64 {
	res, err := st.Exec(q, args...)
	if err != nil {
		return 0
	}
	n, _ := res.RowsAffected()
	return n
}

// ------------------------------------------------------------ gmailSessions
func (h *Handlers) registerGmailSessions(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/gmail-sessions/rent", h.GmailRent)
	mux.HandleFunc("GET /api/gmail-sessions/active", h.GmailActive)
	mux.HandleFunc("GET /api/gmail-sessions", h.GmailList)
	mux.HandleFunc("GET /api/gmail-sessions/{id}", h.GmailDetail)
	mux.HandleFunc("POST /api/gmail-sessions/{id}/release", h.GmailRelease)
	mux.HandleFunc("POST /api/gmail-sessions/{id}/prepare-next-code", h.GmailPrepare)
	mux.HandleFunc("POST /api/gmail-sessions/{id}/expire", h.GmailExpire)
	mux.HandleFunc("POST /api/gmail-sessions/next-alias", h.GmailNextAlias)
}

func (h *Handlers) GmailRent(w http.ResponseWriter, r *http.Request) {
	_ = h.Sched.Dispatch(r.Context(), "gmail_"+randomHex(), "gmail_rent", map[string]interface{}{})
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "detail": "已派发 worker 租用"})
}

func (h *Handlers) GmailActive(w http.ResponseWriter, r *http.Request) {
	items, err := db.List[db.GmailSession](h.Store, "SELECT * FROM gmail_sessions WHERE status='active' ORDER BY id DESC")
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) GmailList(w http.ResponseWriter, r *http.Request) {
	items, err := db.List[db.GmailSession](h.Store, "SELECT * FROM gmail_sessions ORDER BY id DESC")
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) GmailDetail(w http.ResponseWriter, r *http.Request) {
	it, err := db.One[db.GmailSession](h.Store, "SELECT * FROM gmail_sessions WHERE id=?", pathID(r))
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "会话不存在")
		return
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, it)
}

func (h *Handlers) GmailRelease(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE gmail_sessions SET status='expired', expired_reason='released', updated_at=datetime('now') WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) GmailPrepare(w http.ResponseWriter, r *http.Request) {
	_ = h.Sched.Dispatch(r.Context(), "gmail_"+randomHex(), "gmail_prepare", map[string]interface{}{"session_id": pathID(r)})
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "detail": "已派发 worker 预取验证码"})
}

func (h *Handlers) GmailExpire(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE gmail_sessions SET status='expired', updated_at=datetime('now') WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) GmailNextAlias(w http.ResponseWriter, r *http.Request) {
	res, err := h.Store.Exec("UPDATE gmail_sessions SET alias_counter=alias_counter+1 WHERE id=(SELECT id FROM gmail_sessions WHERE status='active' ORDER BY id DESC LIMIT 1)")
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	if n, _ := res.RowsAffected(); n == 0 {
		writeDetail(w, http.StatusNotFound, "没有活动会话")
		return
	}
	var base string
	_ = h.Store.QueryRow("SELECT base_email FROM gmail_sessions WHERE status='active' ORDER BY id DESC LIMIT 1").Scan(&base)
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "alias": base})
}
