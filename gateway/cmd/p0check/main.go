// P0 自检:打开现有 DB,16 表计数;事务内写+回滚验证写路径。
// 用法:go run ./cmd/p0check --root <项目根>
package main

import (
	"flag"
	"fmt"
	"log"
	"os"

	"dsh/openai-register-gateway/internal/config"
	"dsh/openai-register-gateway/internal/db"
)

func main() {
	root := flag.String("root", ".", "项目根目录")
	flag.Parse()
	rootAbs, err := os.Getwd()
	if err == nil && *root != "." {
		rootAbs = *root
	}
	conf, err := config.Load(rootAbs)
	if err != nil {
		log.Printf("warning: %v", err)
	}
	st, err := db.Open(conf.DBPath)
	if err != nil {
		log.Fatalf("FAIL db open: %v", err)
	}
	counts, err := st.CountAll()
	if err != nil {
		log.Fatalf("FAIL count: %v", err)
	}
	ok := true
	for _, t := range []string{
		"accounts", "registrations", "batches", "sub2api_relogin_jobs",
		"sub2api_relogin_items", "link_extraction_jobs", "link_extraction_items",
		"account_sub2api_uploads", "gmail_sessions", "custom_mailboxes",
		"ui_settings", "tasks", "sms_activations", "oauth_logs", "proxies", "health_checks",
	} {
		fmt.Printf("  %-26s %d\n", t, counts[t])
	}
	if err := st.TestWriteRollback(); err != nil {
		log.Printf("FAIL write-rollback: %v", err)
		ok = false
	} else {
		fmt.Println("  write-rollback ok(事务写入已回滚,无脏数据)")
	}
	if !ok {
		os.Exit(1)
	}
	fmt.Println("P0 PASS")
}