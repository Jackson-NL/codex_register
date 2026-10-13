package db

import (
	"database/sql"
	"fmt"
	"reflect"
	"strings"

	_ "modernc.org/sqlite" // 纯 Go SQLite 驱动(无 cgo)
)

type Store struct {
	*sql.DB
}

// Open 打开 SQLite 并设置 WAL + busy_timeout(两进程(网关/worker)并发写的基础)。
func Open(path string) (*Store, error) {
	dsn := fmt.Sprintf("file:%s?_pragma=journal_mode(WAL)&_pragma=busy_timeout(10000)&_pragma=foreign_keys(1)&_pragma=synchronous(NORMAL)", path)
	conn, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	conn.SetMaxOpenConns(1) // SQLite 单写者:进程内所有写串行
	if err := conn.Ping(); err != nil {
		return nil, err
	}
	return &Store{conn}, nil
}

// listRaw 反射 helper:把一行扫描进带 json tag 的结构体。
// 支持目标类型: string/int64/float64/bool 及指针;SQLite 值:
// INTEGER→int64, TEXT→string, REAL→float64, NULL→nil, BOOLEAN 列以 INTEGER(0/1) 存储。
func scanRow(row *sql.Rows, dst interface{}) error {
	rv := reflect.ValueOf(dst)
	if rv.Kind() != reflect.Ptr || rv.Elem().Kind() != reflect.Struct {
		return fmt.Errorf("dst must be pointer to struct")
	}
	st := rv.Elem()
	names, err := row.Columns()
	if err != nil {
		return err
	}
	// 构建 列名→字段 索引
	fields := map[string]reflect.Value{}
	typ := st.Type()
	for i := 0; i < st.NumField(); i++ {
		f := typ.Field(i)
		tag := f.Tag.Get("json")
		if tag == "" {
			continue
		}
		if idx := strings.Index(tag, ","); idx >= 0 {
			tag = tag[:idx]
		}
		fields[tag] = st.Field(i)
	}
	raws := make([]interface{}, len(names))
	for i, n := range names {
		f, ok := fields[n]
		if !ok {
			var discard interface{}
			raws[i] = &discard
			continue
		}
		raws[i] = makeRawTarget(f)
	}
	if err := row.Scan(raws...); err != nil {
		return err
	}
	for i, n := range names {
		f, ok := fields[n]
		if !ok {
			continue
		}
		if err := assignRaw(f, raws[i]); err != nil {
			return fmt.Errorf("column %s: %w", n, err)
		}
	}
	return nil
}

// makeRawTarget 为字段分配扫描目标:统一用 sql.Null* 包装(NULL 安全),
// 指针字段按元素类型取包装。
func makeRawTarget(f reflect.Value) interface{} {
	base := f.Type()
	if base.Kind() == reflect.Ptr {
		base = base.Elem()
	}
	switch base.Kind() {
	case reflect.String:
		return &sql.NullString{}
	case reflect.Int64, reflect.Int:
		return &sql.NullInt64{}
	case reflect.Float64, reflect.Float32:
		return &sql.NullFloat64{}
	case reflect.Bool:
		return &sql.NullInt64{} // SQLite Boolean 以 0/1 存储
	default:
		var discard interface{}
		return &discard
	}
}

// assignRaw 把扫描目标写回字段。
func assignRaw(f reflect.Value, raw interface{}) error {
	switch t := raw.(type) {
	case *sql.NullString:
		if !t.Valid {
			if f.Kind() == reflect.Ptr {
				f.Set(reflect.Zero(f.Type()))
			} else {
				f.SetString("")
			}
			return nil
		}
		if f.Kind() == reflect.Ptr {
			f.Set(reflect.ValueOf(&t.String))
		} else {
			f.SetString(t.String)
		}
		return nil
	case *sql.NullInt64:
		if !t.Valid {
			f.Set(reflect.Zero(f.Type()))
			return nil
		}
		switch f.Kind() {
		case reflect.Ptr:
			switch f.Type().Elem().Kind() {
			case reflect.Int64, reflect.Int:
				v := t.Int64
				f.Set(reflect.ValueOf(&v))
			case reflect.Bool:
				b := t.Int64 != 0
				f.Set(reflect.ValueOf(&b))
			}
		case reflect.Int64, reflect.Int:
			f.SetInt(t.Int64)
		case reflect.Bool:
			f.SetBool(t.Int64 != 0)
		}
		return nil
	case *sql.NullFloat64:
		if !t.Valid {
			f.Set(reflect.Zero(f.Type()))
			return nil
		}
		if f.Kind() == reflect.Ptr {
			v := t.Float64
			f.Set(reflect.ValueOf(&v))
		} else {
			f.SetFloat(t.Float64)
		}
		return nil
	}
	return fmt.Errorf("unexpected raw type %T", raw)
}

// List 查询多行到 []T(泛型导出,按 json tag 列名映射)。
func List[T any](st *Store, query string, args ...interface{}) ([]T, error) {
	return list[T](st, query, args...)
}

// One 查询一行;无记录返回 ErrNoRows。
func One[T any](st *Store, query string, args ...interface{}) (T, error) {
	return one[T](st, query, args...)
}

// list 查询多行到 []T。
func list[T any](st *Store, query string, args ...interface{}) ([]T, error) {
	rows, err := st.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []T{}
	for rows.Next() {
		var item T
		if err := scanRow(rows, &item); err != nil {
			return nil, err
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

// one 查询一行;无记录返回 ErrNoRows。
func one[T any](st *Store, query string, args ...interface{}) (T, error) {
	var item T
	rows, err := st.Query(query, args...)
	if err != nil {
		return item, err
	}
	defer rows.Close()
	if !rows.Next() {
		return item, sql.ErrNoRows
	}
	if err := scanRow(rows, &item); err != nil {
		return item, err
	}
	return item, rows.Err()
}

// CountAll P0 自检:16 张表行数。
func (st *Store) CountAll() (map[string]int64, error) {
	tables := []string{
		"accounts", "registrations", "batches", "sub2api_relogin_jobs",
		"sub2api_relogin_items", "link_extraction_jobs", "link_extraction_items",
		"account_sub2api_uploads", "gmail_sessions", "custom_mailboxes",
		"ui_settings", "tasks", "sms_activations", "oauth_logs", "proxies", "health_checks",
	}
	out := map[string]int64{}
	for _, t := range tables {
		var n int64
		if err := st.QueryRow(fmt.Sprintf("SELECT COUNT(*) FROM %s", t)).Scan(&n); err != nil {
			return nil, fmt.Errorf("count %s: %w", t, err)
		}
		out[t] = n
	}
	return out, nil
}

// TestWriteRollback P0 自检:事务内写一条 test 记录后回滚,验证 Go 写路径可用且不脏数据。
func (st *Store) TestWriteRollback() error {
	tx, err := st.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.Exec("INSERT INTO ui_settings(key, value) VALUES('__gateway_probe__', '1')"); err != nil {
		return err
	}
	if _, err := tx.Exec("DELETE FROM ui_settings WHERE key='__gateway_probe__'"); err != nil {
		return err
	}
	return nil
}