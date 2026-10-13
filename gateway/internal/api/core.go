// accounts / registrations / batches 端点。DB 直读部分完整;
// 浏览器类端点按协议派发 worker(Dispatch)。
// ⚠️ 分页包装/精确响应结构以现有 FastAPI 运行为准,P1 契约验证时逐端对齐。
package api

import (
	"database/sql"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"dsh/openai-register-gateway/internal/db"
)

func (h *Handlers) registerAccounts(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/accounts", h.AccountsList)
	mux.HandleFunc("POST /api/accounts/import", h.AccountsImport)
	mux.HandleFunc("POST /api/accounts/export", h.AccountsExport)
	mux.HandleFunc("POST /api/accounts/bulk-tag", h.AccountsBulkTag)
	mux.HandleFunc("POST /api/accounts/batch", h.AccountsBatch)
	mux.HandleFunc("GET /api/accounts/oauth/countries", h.AccountsOAuthCountries)
	mux.HandleFunc("GET /api/accounts/oauth/logs", h.AccountsOAuthLogs)
	mux.HandleFunc("GET /api/accounts/oauth/jobs/active", h.AccountsOAuthJobsActive)
	mux.HandleFunc("POST /api/accounts/oauth/jobs", h.AccountsOAuthJobsCreate)
	mux.HandleFunc("GET /api/accounts/oauth/jobs/{job}", h.AccountsOAuthJob)
	mux.HandleFunc("POST /api/accounts/oauth/jobs/{job}/cancel", h.AccountsOAuthJobCancel)
	mux.HandleFunc("GET /api/accounts/{id}", h.AccountsDetail)
	mux.HandleFunc("PATCH /api/accounts/{id}", h.AccountsPatch)
	mux.HandleFunc("PATCH /api/accounts/{id}/totp", h.AccountsTotp)
	mux.HandleFunc("DELETE /api/accounts/{id}", h.AccountsDelete)
	mux.HandleFunc("POST /api/accounts/{id}/oauth/refresh-from-profile", h.AccountsOAuthRefresh)
	mux.HandleFunc("POST /api/accounts/{id}/oauth/dry-run-phone-from-profile", h.AccountsOAuthDryRun)
	mux.HandleFunc("POST /api/accounts/{id}/oauth/auto-phone-from-profile", h.AccountsOAuthAutoPhone)
	mux.HandleFunc("POST /api/accounts/{id}/oauth/complete-phone-from-profile", h.AccountsOAuthComplete)
}

func (h *Handlers) AccountsList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	where := []string{"1=1"}
	args := []interface{}{}
	if v := q.Get("status"); v != "" {
		where = append(where, "status=?")
		args = append(args, v)
	}
	if v := q.Get("q"); v != "" {
		where = append(where, "(email LIKE ? OR phone LIKE ?)")
		args = append(args, "%"+v+"%", "%"+v+"%")
	}
	if v := q.Get("plan"); v != "" {
		where = append(where, "plan_type=?")
		args = append(args, v)
	}
	items, err := db.List[db.Account](h.Store, "SELECT * FROM accounts WHERE "+strings.Join(where, " AND ")+" ORDER BY id DESC", args...)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"items": items, "total": len(items)})
}

func (h *Handlers) AccountsDetail(w http.ResponseWriter, r *http.Request) {
	id := pathID(r)
	a, err := db.One[db.Account](h.Store, "SELECT * FROM accounts WHERE id=?", id)
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "账号不存在")
		return
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, a)
}

type bulkTagBody struct {
	IDs []int64 `json:"ids"`
	Tag string  `json:"tag"`
}

func (h *Handlers) AccountsBulkTag(w http.ResponseWriter, r *http.Request) {
	var b bulkTagBody
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	for _, id := range b.IDs {
		_, _ = h.Store.Exec("UPDATE accounts SET tag=? WHERE id=?", b.Tag, id)
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "updated": len(b.IDs)})
}

type accountsBatchBody struct {
	IDs    []int64 `json:"ids"`
	Action string  `json:"action"`
}

func (h *Handlers) AccountsBatch(w http.ResponseWriter, r *http.Request) {
	var b accountsBatchBody
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	status := "active"
	if b.Action == "pause" {
		status = "paused"
	}
	for _, id := range b.IDs {
		_, _ = h.Store.Exec("UPDATE accounts SET status=? WHERE id=?", status, id)
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "updated": len(b.IDs), "status": status})
}

type totpBody struct {
	Secret string `json:"secret"`
}

func (h *Handlers) AccountsTotp(w http.ResponseWriter, r *http.Request) {
	id := pathID(r)
	var b totpBody
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	if _, err := h.Store.Exec("UPDATE accounts SET totp_secret=? WHERE id=?", b.Secret, id); err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "id": id})
}

func (h *Handlers) AccountsPatch(w http.ResponseWriter, r *http.Request) {
	id := pathID(r)
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	sets := []string{}
	args := []interface{}{}
	for _, k := range []string{"note", "tag"} {
		if v, ok := b[k]; ok {
			sets = append(sets, k+"=?")
			args = append(args, v)
		}
	}
	if len(sets) == 0 {
		writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
		return
	}
	args = append(args, id)
	if _, err := h.Store.Exec("UPDATE accounts SET "+strings.Join(sets, ",")+" WHERE id=?", args...); err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "id": id})
}

func (h *Handlers) AccountsDelete(w http.ResponseWriter, r *http.Request) {
	id := pathID(r)
	if _, err := h.Store.Exec("DELETE FROM accounts WHERE id=?", id); err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "id": id})
}

func (h *Handlers) AccountsImport(w http.ResponseWriter, r *http.Request) {
	// 简易导入:支持 cpa(phone:password:token...) 与 sub2api(email:password...) 文本行。
	var b struct {
		Format  string `json:"format"`
		Content string `json:"content"`
		Dedup   string `json:"dedup"`
	}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	imported, skipped := 0, 0
	for _, line := range strings.Split(b.Content, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		cols := strings.Split(line, ":")
		if len(cols) < 2 {
			skipped++
			continue
		}
		var phone, email, pass, at, rt string
		switch b.Format {
		case "cpa":
			phone, pass = cols[0], cols[1]
			if len(cols) > 2 {
				at = cols[2]
			}
		default: // sub2api
			email, pass = cols[0], cols[1]
			if len(cols) > 2 {
				at = cols[2]
			}
		}
		key := phone
		if key == "" {
			key = email
		}
		var dup int
		_ = h.Store.QueryRow("SELECT COUNT(*) FROM accounts WHERE phone=? OR email=?", key, key).Scan(&dup)
		if dup > 0 && b.Dedup == "skip" {
			skipped++
			continue
		}
		if dup > 0 {
			_, _ = h.Store.Exec("UPDATE accounts SET password=?, access_token=?, refresh_token=? WHERE phone=? OR email=?",
				pass, at, rt, key, key)
			imported++
			continue
		}
		_, _ = h.Store.Exec("INSERT INTO accounts(phone,email,password,access_token,refresh_token,status,plan_type,mail_provider,profile_source) VALUES(?,?,?,?,?,'active','free','unknown','import')",
			phone, email, pass, at, rt)
		imported++
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"imported": imported, "skipped": skipped})
}

func (h *Handlers) AccountsExport(w http.ResponseWriter, r *http.Request) {
	var b struct {
		Format string  `json:"format"`
		IDs    []int64 `json:"ids"`
	}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	where := "1=1"
	args := []interface{}{}
	if len(b.IDs) > 0 {
		phs := []string{}
		for _, id := range b.IDs {
			phs = append(phs, "?")
			args = append(args, id)
		}
		where = "id IN (" + strings.Join(phs, ",") + ")"
		args = []interface{}{}
		for _, id := range b.IDs {
			args = append(args, id)
		}
	}
	items, err := db.List[db.Account](h.Store, "SELECT * FROM accounts WHERE "+where+" ORDER BY id", args...)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	lines := []string{}
	for _, a := range items {
		if b.Format == "cpa" && a.Phone != nil {
			lines = append(lines, fmt.Sprintf("%s:%s:%s", nz(a.Phone), nz(a.Password), nz(a.AccessToken)))
		} else {
			lines = append(lines, fmt.Sprintf("%s:%s:%s", nz(a.Email), nz(a.Password), nz(a.AccessToken)))
		}
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"content": strings.Join(lines, "\n"), "count": len(lines)})
}

// oauth 端点:worker 派发 + oauth_logs 直读。
func (h *Handlers) AccountsOAuthLogs(w http.ResponseWriter, r *http.Request) {
	after := atoiDefault(r.URL.Query().Get("after"), 0)
	limit := atoiDefault(r.URL.Query().Get("limit"), 300)
	items, err := db.List[db.OAuthLog](h.Store,
		"SELECT * FROM oauth_logs WHERE id>? ORDER BY id ASC LIMIT ?", after, limit)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"items": items})
}

func (h *Handlers) AccountsOAuthCountries(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, []map[string]string{{"iso": "BR", "dialing_code": "55"}})
}

func (h *Handlers) AccountsOAuthJobsActive(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"jobs": []interface{}{}})
}

func (h *Handlers) AccountsOAuthJobsCreate(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	_ = readBody(r, &b)
	taskID := "oauth_" + randomHex()
	_ = h.Sched.Dispatch(r.Context(), taskID, "oauth", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"task_id": taskID, "status": "pending"})
}

func (h *Handlers) AccountsOAuthJob(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"job_id": r.PathValue("job"), "status": "pending"})
}

func (h *Handlers) AccountsOAuthJobCancel(w http.ResponseWriter, r *http.Request) {
	_ = h.WorkerMgr // 占位
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) oauthDispatch(w http.ResponseWriter, r *http.Request, action string) {
	id := pathID(r)
	var b map[string]interface{}
	_ = readBody(r, &b)
	if b == nil {
		b = map[string]interface{}{}
	}
	b["account_id"] = id
	b["action"] = action
	taskID := fmt.Sprintf("oauth_%d_%s", id, randomHex())
	_ = h.Sched.Dispatch(r.Context(), taskID, "oauth", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"task_id": taskID, "status": "pending"})
}

func (h *Handlers) AccountsOAuthRefresh(w http.ResponseWriter, r *http.Request) {
	h.oauthDispatch(w, r, "refresh")
}
func (h *Handlers) AccountsOAuthDryRun(w http.ResponseWriter, r *http.Request) {
	h.oauthDispatch(w, r, "dry_run_phone")
}
func (h *Handlers) AccountsOAuthAutoPhone(w http.ResponseWriter, r *http.Request) {
	h.oauthDispatch(w, r, "auto_phone")
}
func (h *Handlers) AccountsOAuthComplete(w http.ResponseWriter, r *http.Request) {
	h.oauthDispatch(w, r, "complete_phone")
}

// ------------------------------------------------------------ registrations
func (h *Handlers) registerRegistrations(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/registrations", h.RegistrationsCreate)
	mux.HandleFunc("GET /api/registrations", h.RegistrationsList)
	mux.HandleFunc("GET /api/registrations/{id}", h.RegistrationsDetail)
	mux.HandleFunc("POST /api/registrations/{id}/cancel", h.RegistrationsCancel)
	mux.HandleFunc("POST /api/registrations/{id}/debug/release", h.RegistrationsDebugRelease)
	mux.HandleFunc("GET /api/registrations/{id}/logs", h.RegistrationsLogs)
	mux.HandleFunc("DELETE /api/registrations/{id}/logs", h.RegistrationsClearLogs)
	mux.HandleFunc("GET /api/registrations/log-redact", h.RegistrationsLogRedactGet)
	mux.HandleFunc("POST /api/registrations/log-redact", h.RegistrationsLogRedactSet)
}

func (h *Handlers) RegistrationsCreate(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	res, err := h.Store.Exec(
		"INSERT INTO registrations(status,proxy,headless,bind_totp,debug_mode,gmail_mode,created_at) VALUES('pending',?,?,?,?,?,datetime('now'))",
		s(ctx(b, "proxy")), boolOf(ctx(b, "headless")), boolOf(ctx(b, "bind_totp")),
		boolOf(ctx(b, "debug_mode")), boolOf(ctx(b, "gmail_mode")))
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	id, _ := res.LastInsertId()
	b["registration_id"] = id
	_ = h.Sched.Dispatch(r.Context(), fmt.Sprintf("reg_%d", id), "register", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"id": id, "status": "pending"})
}

func (h *Handlers) RegistrationsList(w http.ResponseWriter, r *http.Request) {
	limit := atoiDefault(r.URL.Query().Get("limit"), 100)
	items, err := db.List[db.Registration](h.Store, "SELECT * FROM registrations ORDER BY id DESC LIMIT ?", limit)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) RegistrationsDetail(w http.ResponseWriter, r *http.Request) {
	it, err := db.One[db.Registration](h.Store, "SELECT * FROM registrations WHERE id=?", pathID(r))
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "注册任务不存在")
		return
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, it)
}

func (h *Handlers) RegistrationsCancel(w http.ResponseWriter, r *http.Request) {
	id := pathID(r)
	_, _ = h.Store.Exec("UPDATE registrations SET status='canceled' WHERE id=?", id)
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true, "id": id})
}

func (h *Handlers) RegistrationsDebugRelease(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) RegistrationsLogs(w http.ResponseWriter, r *http.Request) {
	after := atoiDefault(r.URL.Query().Get("after"), 0)
	limit := atoiDefault(r.URL.Query().Get("limit"), 500)
	it, err := db.One[db.Registration](h.Store, "SELECT logs_json FROM registrations WHERE id=?", pathID(r))
	if err != nil {
		writeDetail(w, http.StatusNotFound, "注册任务不存在")
		return
	}
	var rows []interface{}
	if it.LogsJSON != nil {
		_ = jsonUnmarshal([]byte(*it.LogsJSON), &rows)
	}
	total := len(rows)
	if after > total {
		after = total
	}
	end := after + limit
	if end > total {
		end = total
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"logs": rows[after:end], "next_seq": end})
}

func (h *Handlers) RegistrationsClearLogs(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE registrations SET logs_json='[]' WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) RegistrationsLogRedactGet(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"enabled": false})
}

func (h *Handlers) RegistrationsLogRedactSet(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	_ = readBody(r, &b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"enabled": b["enabled"] == true})
}

// ------------------------------------------------------------ batches
func (h *Handlers) registerBatches(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/batches", h.BatchesCreate)
	mux.HandleFunc("GET /api/batches/{id}", h.BatchesDetail)
	mux.HandleFunc("GET /api/batches/{id}/logs", h.BatchesLogs)
	mux.HandleFunc("DELETE /api/batches/{id}/logs", h.BatchesClearLogs)
	mux.HandleFunc("POST /api/batches/{id}/cancel", h.BatchesCancel)
}

func (h *Handlers) BatchesCreate(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	target := int64(1)
	if v, ok := b["target"].(float64); ok {
		target = int64(v)
	}
	res, err := h.Store.Exec("INSERT INTO batches(status,target,concurrency,headless,bind_totp,gmail_mode,created_at) VALUES('running',?,?,?,?,?,datetime('now'))",
		target, int64f(b["concurrency"], 1), b["headless"] == true, b["bind_totp"] == true, b["gmail_mode"] == true)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	id, _ := res.LastInsertId()
	b["batch_id"] = id
	_ = h.Sched.Dispatch(r.Context(), fmt.Sprintf("batch_%d", id), "batch", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"id": id, "status": "running"})
}

func (h *Handlers) BatchesDetail(w http.ResponseWriter, r *http.Request) {
	it, err := db.One[db.Batch](h.Store, "SELECT * FROM batches WHERE id=?", pathID(r))
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "批任务不存在")
		return
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, it)
}

func (h *Handlers) BatchesLogs(w http.ResponseWriter, r *http.Request) {
	after := atoiDefault(r.URL.Query().Get("after"), 0)
	limit := atoiDefault(r.URL.Query().Get("limit"), 500)
	it, err := db.One[db.Batch](h.Store, "SELECT logs_json FROM batches WHERE id=?", pathID(r))
	if err != nil {
		writeDetail(w, http.StatusNotFound, "批任务不存在")
		return
	}
	var rows []interface{}
	if it.LogsJSON != nil {
		_ = jsonUnmarshal([]byte(*it.LogsJSON), &rows)
	}
	total := len(rows)
	if after > total {
		after = total
	}
	end := after + limit
	if end > total {
		end = total
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"logs": rows[after:end], "next_seq": end})
}

func (h *Handlers) BatchesClearLogs(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE batches SET logs_json='[]' WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func (h *Handlers) BatchesCancel(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE batches SET status='canceled' WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

// ------------------------------------------------------------ helpers
func pathID(r *http.Request) int64 {
	v, _ := strconv.ParseInt(r.PathValue("id"), 10, 64)
	return v
}

func atoiDefault(s string, def int) int {
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}

func nz(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func s(v interface{}) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

func int64f(v interface{}, def int64) int64 {
	if f, ok := v.(float64); ok {
		return int64(f)
	}
	return def
}

func ctx(m map[string]interface{}, k string) interface{} {
	if m == nil {
		return nil
	}
	return m[k]
}

func boolOf(v interface{}) bool {
	if v == nil {
		return false
	}
	if b, ok := v.(bool); ok {
		return b
	}
	return false
}
