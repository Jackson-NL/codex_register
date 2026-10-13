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
	activeAccounts := row("SELECT COUNT(*) FROM accounts WHERE status='active'")
	cooling := row("SELECT COUNT(*) FROM accounts WHERE status='cooling'")
	paused := row("SELECT COUNT(*) FROM accounts WHERE status='paused'")
	unhealthy := row("SELECT COUNT(*) FROM accounts WHERE status='unhealthy'")
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
		"active_accounts":        activeAccounts,
		"cooling":                cooling,
		"paused":                 paused,
		"unhealthy":              unhealthy,
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