package api

import (
	"net/http"
)

func (h *Handlers) registerStats(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/stats", h.GetStats)
}

// GetStats 与 backend/app/api/stats.py 聚合逻辑一致。
func (h *Handlers) GetStats(w http.ResponseWriter, r *http.Request) {
	st := h.Store
	row := func(q string) int64 {
		var n int64
		_ = st.QueryRow(q).Scan(&n)
		return n
	}
	totalAccounts := row("SELECT COUNT(*) FROM accounts")
	// 账号状态只分两态：正常 / 异常。
	// ⚠️ 局限：这里只能按 status 字段计数。FastAPI 的完整口径是「status 正常 **且**
	// 凭据当下可用」（JWT exp 需 base64 解码，SQL 无法计算，见
	// backend/app/services/account_status.py）。因此网关的 normal_accounts 会偏高——
	// 需要精确值请走 FastAPI 的 /api/stats；本实现仅作无 Python 进程时的降级视图。
	normalAccounts := row(statusCountQuery(statusNormal))
	abnormalAccounts := row(statusCountQuery(statusAbnormal))
	todaySuccess := row("SELECT COUNT(*) FROM registrations WHERE status='success' AND date(finished_at)=date('now','localtime')")
	todayFailed := row("SELECT COUNT(*) FROM registrations WHERE status='failed' AND date(finished_at)=date('now','localtime')")
	running := row("SELECT COUNT(*) FROM registrations WHERE status IN ('pending','running')")
	failedRegs := row("SELECT COUNT(*) FROM registrations WHERE status='failed'")
	totpBound := row("SELECT COUNT(*) FROM accounts WHERE totp_secret != ''")
	proxyOnline := row("SELECT COUNT(*) FROM proxies WHERE status='ok'")
	proxyTotal := row("SELECT COUNT(*) FROM proxies")
	proxyFailed := row("SELECT COUNT(*) FROM proxies WHERE status IN ('failed','offline')")
	gmailActive := row("SELECT COUNT(*) FROM gmail_sessions WHERE status='active'")
	gmailExpired := row("SELECT COUNT(*) FROM gmail_sessions WHERE status='expired'")

	passRate := 0.0
	if todaySuccess+todayFailed > 0 {
		passRate = round1(float64(todaySuccess) / float64(todaySuccess+todayFailed) * 100)
	}
	totpCoverage := 0.0
	if totalAccounts > 0 {
		totpCoverage = round1(float64(totpBound) / float64(totalAccounts) * 100)
	}

	writeJSON(w, http.StatusOK, map[string]interface{}{
		"total_accounts":         totalAccounts,
		"normal_accounts":        normalAccounts,
		"abnormal_accounts":      abnormalAccounts,
		"today_success":          todaySuccess,
		"today_failed":           todayFailed,
		"pass_rate":              passRate,
		"running":                running,
		"failed_regs":            failedRegs,
		"totp_bound":             totpBound,
		"totp_coverage":          totpCoverage,
		"gmail_active_sessions":  gmailActive,
		"gmail_expired_sessions": gmailExpired,
		"proxy_online":           proxyOnline,
		"proxy_total":            proxyTotal,
		"proxy_failed":           proxyFailed,
	})
}

func round1(v float64) float64 {
	return float64(int(v*10+0.5)) / 10
}