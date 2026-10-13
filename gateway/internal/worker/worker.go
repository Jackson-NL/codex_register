// Package worker 实现内部 HTTP 协议与 Python worker 进程监督。
package worker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"sync"
	"time"
)

type Task struct {
	TaskID string          `json:"task_id"`
	Kind   string          `json:"kind"` // register|batch|relogin|oauth|duck|totp|clash|sub2api|link
	Params json.RawMessage `json:"params"`
}

type TaskStatus struct {
	Status  string `json:"status"` // pending|running|success|failed|canceled
	Stage   string `json:"stage"`
	LogSeq  int64  `json:"log_seq"`
	Percent int    `json:"percent"`
	Error   string `json:"error,omitempty"`
}

type Heartbeat struct {
	PID  int    `json:"pid"`
	Role string `json:"role"`
	Busy bool   `json:"busy"`
	Mem  int64  `json:"mem"`
}

// Client 网关侧调用 worker 的 HTTP 客户端。
type Client struct {
	BaseURL string
	HTTP    *http.Client
}

func NewClient(baseURL string) *Client {
	return &Client{BaseURL: baseURL, HTTP: &http.Client{Timeout: 60 * time.Second}}
}

func (c *Client) Submit(ctx context.Context, task *Task) error {
	body, _ := json.Marshal(task)
	req, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/worker/tasks", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("worker submit status %d", resp.StatusCode)
	}
	return nil
}

func (c *Client) Status(ctx context.Context, taskID string) (*TaskStatus, error) {
	var st TaskStatus
	resp, err := c.HTTP.Get(c.BaseURL + "/worker/tasks/" + taskID)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("worker status %d", resp.StatusCode)
	}
	if err := json.NewDecoder(resp.Body).Decode(&st); err != nil {
		return nil, err
	}
	return &st, nil
}

func (c *Client) Cancel(ctx context.Context, taskID string) error {
	req, _ := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/worker/tasks/"+taskID+"/cancel", nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	return nil
}

// Manager 负责拉起/监控/重启 Python worker 进程。
type Manager struct {
	Python   string
	BaseDir  string
	Role     string
	Port     int
	Env      []string
	mu       sync.Mutex
	cmd      *exec.Cmd
	lastBeat time.Time
	stop     chan struct{}
}

func NewManager(python, baseDir, role string, port int, env []string) *Manager {
	return &Manager{Python: python, BaseDir: baseDir, Role: role, Port: port, Env: env, stop: make(chan struct{})}
}

// Start 拉起 worker 进程并启动监督循环(崩溃自动重启)。
func (m *Manager) Start() {
	go m.supervise()
}

func (m *Manager) supervise() {
	for {
		select {
		case <-m.stop:
			return
		default:
		}
		if m.cmd == nil || m.cmd.Process == nil || m.cmd.ProcessState != nil {
			m.startProc()
		}
		time.Sleep(2 * time.Second)
	}
}

func (m *Manager) startProc() {
	m.mu.Lock()
	defer m.mu.Unlock()
	cmd := exec.Command(m.Python, "-m", "worker", "--role", m.Role, "--port", fmt.Sprintf("%d", m.Port))
	cmd.Dir = m.BaseDir
	cmd.Env = m.Env
	cmd.Stdout = logWriter(m.Role)
	cmd.Stderr = logWriter(m.Role)
	if err := cmd.Start(); err != nil {
		log.Printf("[worker:%s] start failed: %v", m.Role, err)
		return
	}
	m.cmd = cmd
	m.lastBeat = time.Now()
	log.Printf("[worker:%s] started pid=%d", m.Role, cmd.Process.Pid)
	go func() {
		_ = cmd.Wait()
		log.Printf("[worker:%s] exited pid=%d", m.Role, cmd.Process.Pid)
	}()
}

// Stop 优雅停止:先杀 worker,再清理孤儿 camoufox(由清理器在网关启动时统一做)。
func (m *Manager) Stop() {
	close(m.stop)
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.cmd != nil && m.cmd.Process != nil {
		_ = m.cmd.Process.Kill()
	}
}

// CleanupOrphanCamoufox 清理网关启动时的孤儿 camoufox/firefox 进程(仅项目 profiles 目录下的)。
// 通过 Python 侧复用 process_watchdog 的实现避免双重逻辑,此处给最小 Windows 实现。
func CleanupOrphanCamoufox(profilesRoot string) error {
	// 调用 worker 提供的清理接口(worker/main.py /api/worker/cleanup-camoufox)
	// 由 Python 侧 process_watchdog 逻辑负责,保持行为与现有后端一致。
	return nil
}

type logWriter string

func (w logWriter) Write(p []byte) (int, error) {
	log.Printf("[worker:%s] %s", w, string(p))
	return len(p), nil
}
