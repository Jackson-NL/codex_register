// sub2api / sub2apiRelogin / linkExtraction 端点。
package api

import (
	"database/sql"
	"net/http"
	"strconv"
	"strings"

	"dsh/openai-register-gateway/internal/db"
)

// ------------------------------------------------------------ sub2api
func (h *Handlers) registerSub2API(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/sub2api/groups", h.Sub2APIGroups)
	mux.HandleFunc("POST /api/sub2api/upload", h.Sub2APIUpload)
	mux.HandleFunc("POST /api/sub2api/upload/jobs", h.Sub2APIUploadJobCreate)
	mux.HandleFunc("GET /api/sub2api/upload/jobs/{job}", h.Sub2APIUploadJob)
	mux.HandleFunc("POST /api/sub2api/upload-status/sync", h.Sub2APISync)
	mux.HandleFunc("GET /api/sub2api/upload-status", h.Sub2APIUploadStatus)
}

func (h *Handlers) Sub2APIGroups(w http.ResponseWriter, r *http.Request) {
	_ = h.Sched.Dispatch(r.Context(), "s2a_"+randomHex(), "sub2api_groups", map[string]interface{}{})
	writeJSON(w, http.StatusOK, map[string]interface{}{"groups": []interface{}{}, "detail": "远端分组由 worker 拉取"})
}

func (h *Handlers) Sub2APIUpload(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	_ = readBody(r, &b)
	taskID := "s2a_upload_" + randomHex()
	_ = h.Sched.Dispatch(r.Context(), taskID, "sub2api_upload", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"task_id": taskID, "status": "pending"})
}

func (h *Handlers) Sub2APIUploadJobCreate(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	_ = readBody(r, &b)
	taskID := "s2a_job_" + randomHex()
	_ = h.Sched.Dispatch(r.Context(), taskID, "sub2api_upload_job", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"job_id": taskID, "status": "pending"})
}

func (h *Handlers) Sub2APIUploadJob(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"job_id": r.PathValue("job"), "status": "pending"})
}

func (h *Handlers) Sub2APISync(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	_ = readBody(r, &b)
	taskID := "s2a_sync_" + randomHex()
	_ = h.Sched.Dispatch(r.Context(), taskID, "sub2api_sync", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"task_id": taskID, "status": "pending"})
}

func (h *Handlers) Sub2APIUploadStatus(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	where, args := []string{"1=1"}, []interface{}{}
	if v := q.Get("group_ids"); v != "" {
		ids := strings.Split(v, ",")
		phs := []string{}
		for _, id := range ids {
			phs = append(phs, "?")
			args = append(args, id)
		}
		where = append(where, "group_id IN ("+strings.Join(phs, ",")+")")
	}
	if v := q.Get("status"); v != "" && v != "all" {
		where = append(where, "status=?")
		args = append(args, v)
	}
	if v := q.Get("q"); v != "" {
		where = append(where, "email LIKE ?")
		args = append(args, "%"+v+"%")
	}
	if v := q.Get("account_id"); v != "" {
		where = append(where, "account_id=?")
		args = append(args, v)
	}
	page := atoiDefault(q.Get("page"), 1)
	pageSize := atoiDefault(q.Get("page_size"), 20)
	items, err := db.List[db.AccountSub2APIUpload](h.Store,
		"SELECT * FROM account_sub2api_uploads WHERE "+strings.Join(where, " AND ")+" ORDER BY id DESC LIMIT ? OFFSET ?",
		append(args, pageSize, (page-1)*pageSize)...)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	var total int64
	_ = h.Store.QueryRow("SELECT COUNT(*) FROM account_sub2api_uploads WHERE "+strings.Join(where, " AND "), args...).Scan(&total)
	writeJSON(w, http.StatusOK, map[string]interface{}{"items": items, "total": total, "page": page, "page_size": pageSize})
}

// ------------------------------------------------------------ sub2apiRelogin
func (h *Handlers) registerSub2APIRelogin(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/sub2api/relogin/preview", h.ReloginPreview)
	mux.HandleFunc("POST /api/sub2api/relogin/jobs", h.ReloginCreate)
	mux.HandleFunc("GET /api/sub2api/relogin/jobs", h.ReloginJobs)
	mux.HandleFunc("GET /api/sub2api/relogin/jobs/{id}", h.ReloginJob)
	mux.HandleFunc("GET /api/sub2api/relogin/jobs/{id}/items", h.ReloginItems)
	mux.HandleFunc("GET /api/sub2api/relogin/jobs/{id}/logs", h.ReloginLogs)
	mux.HandleFunc("POST /api/sub2api/relogin/jobs/{id}/cancel", h.ReloginCancel)
}

func (h *Handlers) ReloginPreview(w http.ResponseWriter, r *http.Request) {
	// 候选 = 本地 account_sub2api_uploads(group_ids 过滤)且本地 accounts 有 access_token。
	q := r.URL.Query()
	where, args := []string{"u.group_id IN (SELECT id)"}, []interface{}{}
	_ = where
	groupIDs := q.Get("group_ids")
	onlyError := q.Get("only_error") != "false"
	joins := " JOIN accounts a ON a.id=u.account_id"
	conds := []string{"a.access_token != ''"}
	args = []interface{}{}
	if groupIDs != "" {
		phs := []string{}
		for _, id := range strings.Split(groupIDs, ",") {
			phs = append(phs, "?")
			args = append(args, id)
		}
		conds = append(conds, "u.group_id IN ("+strings.Join(phs, ",")+")")
	}
	if onlyError {
		conds = append(conds, "(u.status IN ('uploaded_error','token_error','remote_error') OR u.remote_status IS NOT NULL)")
	}
	items, err := db.List[db.AccountSub2APIUpload](h.Store,
		"SELECT u.* FROM account_sub2api_uploads u"+joins+" WHERE "+strings.Join(conds, " AND ")+" ORDER BY u.id", args...)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"items": items, "total": len(items)})
}

func (h *Handlers) ReloginCreate(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	res, err := h.Store.Exec("INSERT INTO sub2api_relogin_jobs(status,group_ids,headless,concurrency,only_error,created_at) VALUES('pending',?,?,?,?,datetime('now'))",
		s(ctx(b, "group_ids")), b["headless"] == true, int64f(ctx(b, "concurrency"), 3), b["only_error"] != false)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	id, _ := res.LastInsertId()
	b["job_id"] = id
	_ = h.Sched.Dispatch(r.Context(), "relogin_"+itoa64(id), "relogin", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"id": id, "status": "pending"})
}

func (h *Handlers) ReloginJobs(w http.ResponseWriter, r *http.Request) {
	items, err := db.List[db.Sub2APIReloginJob](h.Store, "SELECT * FROM sub2api_relogin_jobs ORDER BY id DESC")
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) ReloginJob(w http.ResponseWriter, r *http.Request) {
	it, err := db.One[db.Sub2APIReloginJob](h.Store, "SELECT * FROM sub2api_relogin_jobs WHERE id=?", pathID(r))
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "重登任务不存在")
		return
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, it)
}

func (h *Handlers) ReloginItems(w http.ResponseWriter, r *http.Request) {
	items, err := db.List[db.Sub2APIReloginItem](h.Store, "SELECT * FROM sub2api_relogin_items WHERE job_id=? ORDER BY id", pathID(r))
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) ReloginLogs(w http.ResponseWriter, r *http.Request) {
	after := atoiDefault(r.URL.Query().Get("after"), 0)
	it, err := db.One[db.Sub2APIReloginJob](h.Store, "SELECT logs_json FROM sub2api_relogin_jobs WHERE id=?", pathID(r))
	if err != nil {
		writeDetail(w, http.StatusNotFound, "重登任务不存在")
		return
	}
	var rows []interface{}
	if it.LogsJSON != nil {
		_ = jsonUnmarshal([]byte(*it.LogsJSON), &rows)
	}
	if int(after) > len(rows) {
		after = len(rows)
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"logs": rows[after:], "next_seq": len(rows)})
}

func (h *Handlers) ReloginCancel(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE sub2api_relogin_jobs SET status='canceled' WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

// ------------------------------------------------------------ linkExtraction
func (h *Handlers) registerLinkExtraction(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/link-extraction/accounts", h.LinkAccounts)
	mux.HandleFunc("POST /api/link-extraction/jobs", h.LinkCreate)
	mux.HandleFunc("GET /api/link-extraction/jobs", h.LinkJobs)
	mux.HandleFunc("GET /api/link-extraction/jobs/{id}", h.LinkJob)
	mux.HandleFunc("GET /api/link-extraction/jobs/{id}/items", h.LinkItems)
	mux.HandleFunc("GET /api/link-extraction/jobs/{id}/logs", h.LinkLogs)
	mux.HandleFunc("POST /api/link-extraction/jobs/{id}/cancel", h.LinkCancel)
}

func (h *Handlers) LinkAccounts(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	where, args := []string{"1=1"}, []interface{}{}
	if v := q.Get("q"); v != "" {
		where = append(where, "(email LIKE ? OR phone LIKE ?)")
		args = append(args, "%"+v+"%", "%"+v+"%")
	}
	if q.Get("has_token") != "false" {
		where = append(where, "access_token != ''")
	}
	page := atoiDefault(q.Get("page"), 1)
	pageSize := atoiDefault(q.Get("page_size"), 50)
	items, err := db.List[db.Account](h.Store,
		"SELECT * FROM accounts WHERE "+strings.Join(where, " AND ")+" ORDER BY id DESC LIMIT ? OFFSET ?",
		append(args, pageSize, (page-1)*pageSize)...)
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	var total int64
	_ = h.Store.QueryRow("SELECT COUNT(*) FROM accounts WHERE "+strings.Join(where, " AND "), args...).Scan(&total)
	writeJSON(w, http.StatusOK, map[string]interface{}{"items": items, "total": total})
}

func (h *Handlers) LinkCreate(w http.ResponseWriter, r *http.Request) {
	var b map[string]interface{}
	if err := readBody(r, &b); err != nil {
		writeDetail(w, http.StatusBadRequest, "请求体解析失败")
		return
	}
	res, err := h.Store.Exec("INSERT INTO link_extraction_jobs(status,total,concurrency,country,payment_method,created_at) VALUES('pending',0,?,?,?,datetime('now'))",
		int64f(ctx(b, "concurrency"), 2), s(ctx(b, "country")), s(ctx(b, "payment_method")))
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	id, _ := res.LastInsertId()
	b["job_id"] = id
	_ = h.Sched.Dispatch(r.Context(), "link_"+itoa64(id), "link", b)
	writeJSON(w, http.StatusOK, map[string]interface{}{"id": id, "status": "pending"})
}

func (h *Handlers) LinkJobs(w http.ResponseWriter, r *http.Request) {
	items, err := db.List[db.LinkExtractionJob](h.Store, "SELECT * FROM link_extraction_jobs ORDER BY id DESC")
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) LinkJob(w http.ResponseWriter, r *http.Request) {
	it, err := db.One[db.LinkExtractionJob](h.Store, "SELECT * FROM link_extraction_jobs WHERE id=?", pathID(r))
	if err == sql.ErrNoRows {
		writeDetail(w, http.StatusNotFound, "提链任务不存在")
		return
	}
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, it)
}

func (h *Handlers) LinkItems(w http.ResponseWriter, r *http.Request) {
	items, err := db.List[db.LinkExtractionItem](h.Store, "SELECT * FROM link_extraction_items WHERE job_id=? ORDER BY id", pathID(r))
	if err != nil {
		writeDetail(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, items)
}

func (h *Handlers) LinkLogs(w http.ResponseWriter, r *http.Request) {
	after := atoiDefault(r.URL.Query().Get("after"), 0)
	it, err := db.One[db.LinkExtractionJob](h.Store, "SELECT logs_json FROM link_extraction_jobs WHERE id=?", pathID(r))
	if err != nil {
		writeDetail(w, http.StatusNotFound, "提链任务不存在")
		return
	}
	var rows []interface{}
	if it.LogsJSON != nil {
		_ = jsonUnmarshal([]byte(*it.LogsJSON), &rows)
	}
	if int(after) > len(rows) {
		after = len(rows)
	}
	writeJSON(w, http.StatusOK, map[string]interface{}{"logs": rows[after:], "next_seq": len(rows)})
}

func (h *Handlers) LinkCancel(w http.ResponseWriter, r *http.Request) {
	_, _ = h.Store.Exec("UPDATE link_extraction_jobs SET status='canceled' WHERE id=?", pathID(r))
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func itoa64(n int64) string {
	return strconv.FormatInt(n, 10)
}