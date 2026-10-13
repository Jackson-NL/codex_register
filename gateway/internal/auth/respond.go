package auth

import (
	"encoding/json"
	"net/http"
)

// writeDetail 输出与 FastAPI 一致的错误体 {"detail": "..."}。
func writeDetail(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": msg})
}
