package auth

import "context"

type contextKey int

const expiresKey contextKey = 1

func ctxWithExpires(ctx context.Context, expires int64) context.Context {
	return context.WithValue(ctx, expiresKey, expires)
}

// ExpiresFrom 取回中间件放入的过期时间戳。
func ExpiresFrom(ctx context.Context) int64 {
	v, _ := ctx.Value(expiresKey).(int64)
	return v
}