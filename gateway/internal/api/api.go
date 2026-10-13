// Package api 提供全部 HTTP 路由(与 frontend/src/api/index.js 89 个调用一一对应)。
package api

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"dsh/openai-register-gateway/internal/auth"
	"dsh/openai-register-gateway/internal/config"
	"dsh/openai-register-gateway/internal/db"
	"dsh/openai-register-gateway/internal/scheduler"
	"dsh/openai-register-gateway/internal/worker"
)

type Handlers struct {
	Store     *db.Store
	Conf      *config.Config
	Secret    *auth.Secret
	WorkerMgr *worker.Manager
	Sched     *scheduler.Scheduler
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeDetail(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"detail": msg})
}

func readBody(r *http.Request, dst interface{}) error {
	defer r.Body.Close()
	return json.NewDecoder(r.Body).Decode(dst)
}

// NewRouter 组装全部路由。不需要 admin 鉴权的端点单独注册;
// 其余套 auth 中间件(admin_auth_enabled 关闭时中间件会返回 404,
// 与 FastAPI 的 _ensure_enabled 行为一致)。
func NewRouter(h *Handlers) http.Handler {
	mux := http.NewServeMux()

	// 无需鉴权(或自带登录逻辑)
	registerPublic(mux, h)
	mux.Handle("/api/admin/", adminProtected(mux, h))
	mux.Handle("/api/", publicGroup(mux, h))
	return logRequests(mux)
}

// adminProtected 只把需会话的 admin 端点(除 status/login)包进鉴权中间件。
func adminProtected(mux *http.ServeMux, h *Handlers) http.Handler {
	inner := http.NewServeMux()
	inner.HandleFunc("GET /api/admin/session", h.AdminSession)
	inner.HandleFunc("GET /api/admin/overview", h.AdminOverview)
	inner.HandleFunc("POST /api/admin/logout", h.AdminLogout)
	return h.Secret.RequireAdmin(inner)
}

// publicGroup 其余 /api/* 端点(与 FastAPI 一致:仅 /api/admin/* 需管理员会话,
// 其余业务端点公开,前端用 cookie 可选)。
func publicGroup(mux *http.ServeMux, h *Handlers) http.Handler {
	inner := http.NewServeMux()
	registerAll(inner, h)
	return inner
}

func registerPublic(mux *http.ServeMux, h *Handlers) {
	// 健康检查(P0 自检可绕开鉴权)
	mux.HandleFunc("GET /api/health", h.Health)
	// admin 公开端点(status/login 与 FastAPI 一致不要求会话)
	mux.HandleFunc("GET /api/admin/status", h.AdminStatus)
	mux.HandleFunc("POST /api/admin/login", h.AdminLogin)
}

func registerAll(mux *http.ServeMux, h *Handlers) {
	h.registerStats(mux)
	h.registerAccounts(mux)
	h.registerRegistrations(mux)
	h.registerBatches(mux)
	h.registerProxies(mux)
	h.registerGmailSessions(mux)
	h.registerSettings(mux)
	h.registerMailConfig(mux)
	h.registerSub2API(mux)
	h.registerSub2APIRelogin(mux)
	h.registerLinkExtraction(mux)
}

// logRequests 简单访问日志。
func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s (%s)", r.Method, r.URL.Path, time.Since(start).Round(time.Millisecond))
	})
}
