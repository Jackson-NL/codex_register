// Package config 加载网关配置:同名环境变量 + backend/.env(注入 worker 用)。
package config

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Config struct {
	ProjectRoot string
	DBPath      string
	ProfilesDir string

	GatewayPort int
	WorkerPort  int
	WorkerCount int
	WorkerPython string
	WorkerBaseDir string // Python worker 启动目录(含 worker/ 包)

	AdminAuthEnabled bool
	AdminAccessKey   string
	AdminSessionTTL  int
	AdminCookieSecure bool

	Sub2APIBaseURL   string
	Sub2APIAdminKey  string
	Sub2APIJWT       string
	Sub2APIGroupIDs  string
	Sub2APIProxy     string
	Sub2APITimeout   float64

	// DotEnv 记录 backend/.env 全部键值,启动 worker 时注入其环境。
	DotEnv map[string]string
}

func Load(root string) (*Config, error) {
	c := &Config{
		ProjectRoot:      root,
		DBPath:           filepath.Join(root, "backend", "data", "openai_register.db"),
		ProfilesDir:      filepath.Join(root, "backend", "profiles"),
		GatewayPort:      envInt("GATEWAY_PORT", 8000),
		WorkerPort:       envInt("WORKER_PORT", 9100),
		WorkerCount:      envInt("WORKER_COUNT", 1),
		WorkerPython:     envStr("WORKER_PYTHON", filepath.Join(root, "backend", ".venv", "Scripts", "python.exe")),
		WorkerBaseDir:    filepath.Join(root, "worker"),
		AdminAuthEnabled: envBool("ADMIN_AUTH_ENABLED", false),
		AdminAccessKey:   os.Getenv("ADMIN_ACCESS_KEY"),
		AdminSessionTTL:  envInt("ADMIN_SESSION_TTL_SECONDS", 8*60*60),
		AdminCookieSecure: envBool("ADMIN_COOKIE_SECURE", false),
		Sub2APIBaseURL:   os.Getenv("SUB2API_BASE_URL"),
		Sub2APIAdminKey:  os.Getenv("SUB2API_ADMIN_API_KEY"),
		Sub2APIJWT:       os.Getenv("SUB2API_JWT"),
		Sub2APIGroupIDs:  os.Getenv("SUB2API_GROUP_IDS"),
		Sub2APIProxy:     os.Getenv("SUB2API_PROXY"),
		Sub2APITimeout:   envFloat("SUB2API_TIMEOUT", 30),
		DotEnv:           map[string]string{},
	}

	// 若 WORKER_PYTHON 未显式设置,优先探测生产解释器(start.ps1 同款)。
	if os.Getenv("WORKER_PYTHON") == "" {
		for _, cand := range []string{
			`E:\python\python3.13.3\python.exe`,
			filepath.Join(root, "backend", ".venv", "Scripts", "python.exe"),
		} {
			if _, err := os.Stat(cand); err == nil {
				c.WorkerPython = cand
				break
			}
		}
	}

	if err := c.loadDotEnv(filepath.Join(root, "backend", ".env")); err != nil {
		return nil, err
	}
	return c, nil
}

// loadDotEnv 读取 backend/.env(不覆盖已存在的进程环境变量)。
func (c *Config) loadDotEnv(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		eq := strings.Index(line, "=")
		if eq <= 0 {
			continue
		}
		key := strings.TrimSpace(line[:eq])
		val := strings.Trim(strings.TrimSpace(line[eq+1:]), `"'`)
		c.DotEnv[key] = val
	}
	return sc.Err()
}

// WorkerEnv 返回注入 Python worker 的完整环境(进程 env + backend/.env 键,
// 已存在的进程环境优先,避免 worker 环境与网关不一致)。
func (c *Config) WorkerEnv() []string {
	env := os.Environ()
	seen := map[string]bool{}
	for _, kv := range env {
		k := kv[:strings.Index(kv, "=")]
		seen[k] = true
	}
	for k, v := range c.DotEnv {
		if !seen[k] {
			env = append(env, k+"="+v)
		}
	}
	return env
}

func envStr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envBool(k string, def bool) bool {
	if v := os.Getenv(k); v != "" {
		if b, err := strconv.ParseBool(v); err == nil {
			return b
		}
	}
	return def
}

func envFloat(k string, def float64) float64 {
	if v := os.Getenv(k); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
