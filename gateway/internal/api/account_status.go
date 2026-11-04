// 账号状态两态口径，与 backend/app/services/account_status.py 同源：
// 底层 accounts.status 保留多个取值（active / cooling / paused / disabled ...），
// 但对外展示与统计只分「正常 / 异常」两态。
//
//   - 正常：active、cooling（冷却会自动恢复，本身可用）
//   - 异常：其余一切取值，含空值与未来新增取值（fail-closed）
//
// 改这里必须同步改 Python 侧与前端 accountsShared.jsx，三处漂移会让账号页徽标
// 与仪表盘分布互相矛盾。
package api

import "strings"

const (
	statusNormal   = "normal"
	statusAbnormal = "abnormal"

	// 两态判定的 SQL 片段，与 Python status_case() 生成的表达式逐字对应。
	normalConditionSQL   = "lower(trim(coalesce(status, ''))) IN ('active', 'cooling')"
	abnormalConditionSQL = "lower(trim(coalesce(status, ''))) NOT IN ('active', 'cooling')"
)

// statusCondition 返回按状态桶过滤的 SQL 片段与参数。
// bucket 为 normal / abnormal 时走两态口径；其余值按原始 status 精确匹配，
// 保持既有调用方（如 status=disabled）兼容。
func statusCondition(bucket string) (string, []any) {
	switch strings.ToLower(strings.TrimSpace(bucket)) {
	case statusNormal:
		return normalConditionSQL, nil
	case statusAbnormal:
		return abnormalConditionSQL, nil
	default:
		return "status=?", []any{bucket}
	}
}

// statusCountQuery 返回统计指定状态桶账号数的 SQL（与 stats.py 同一口径）。
func statusCountQuery(bucket string) string {
	condition, _ := statusCondition(bucket)
	return "SELECT COUNT(*) FROM accounts WHERE " + condition
}
