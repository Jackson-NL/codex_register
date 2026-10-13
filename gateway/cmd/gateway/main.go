// 入口:加载配置、初始化 DB、启动 HTTP、拉起 worker 监督器。
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"time"

	"dsh/openai-register-gateway/internal/api"
	"dsh/openai-register-gateway/internal/auth"
	"dsh/openai-register-gateway/internal/config"
	"dsh/openai-register-gateway/internal/db"
	"dsh/openai-register-gateway/internal/scheduler"
	"dsh/openai-register-gateway/internal/worker"
)

func main() {
	root := flag.String("root", ".", "项目根目录(含 backend/ 与 worker/)")
	flag.Parse()

	rootAbs, err := filepath.Abs(*root)
	if err != nil {
		log.Fatalf("root: %v", err)
	}
	conf, err := config.Load(rootAbs)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("warning: .env not loaded (%v)", err)
		}
	}

	store, err := db.Open(conf.DBPath)
	if err != nil {
		log.Fatalf("db open %s: %v", conf.DBPath, err)
	}

	secret := &auth.Secret{
		Key:          conf.AdminAccessKey,
		Enabled:      conf.AdminAuthEnabled,
		TTL:          conf.AdminSessionTTL,
		CookieSecure: conf.AdminCookieSecure,
	}

	wClient := worker.NewClient("http://127.0.0.1:" + strconv.Itoa(conf.WorkerPort))
	sched := scheduler.New(wClient)

	// 拉起 Python worker(角色 register 的通用进程,承担全部浏览器任务)。
	// 工作目录必须为项目根(worker/ 是根下的包,python -m worker 才能找到)。
	wm := worker.NewManager(conf.WorkerPython, rootAbs, "register", conf.WorkerPort, conf.WorkerEnv())
	wm.Start()
	defer wm.Stop()

	h := &api.Handlers{
		Store:     store,
		Conf:      conf,
		Secret:    secret,
		WorkerMgr: wm,
		Sched:     sched,
	}

	srv := &http.Server{
		Addr:    "127.0.0.1:" + strconv.Itoa(conf.GatewayPort),
		Handler: api.NewRouter(h),
	}

	go func() {
		log.Printf("gateway listening on %s", srv.Addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http: %v", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, os.Interrupt)
	<-quit
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}