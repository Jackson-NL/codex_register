// Package db 定义 16 张表的 Go 结构体(列名与 backend/app/models.py 完全一致,
// json tag = 列名 = FastAPI 响应字段名)。类型映射已按 models.py 修正。
package db

type Account struct {
	ID                 int64   `json:"id"`
	Phone              *string `json:"phone"`
	Password           *string `json:"password"`
	AccessToken        *string `json:"access_token"`
	RefreshToken       *string `json:"refresh_token"`
	IDToken            *string `json:"id_token"`
	AccountID          *string `json:"account_id"`
	UserID             *string `json:"user_id"`
	PlanType           *string `json:"plan_type"`
	Proxy              *string `json:"proxy"`
	ProfilePath        *string `json:"profile_path"`
	Status             string  `json:"status"`
	CreatedAt          string  `json:"created_at"`
	LastCheckAt        *string `json:"last_check_at"`
	Email              *string `json:"email"`
	TotpSecret         *string `json:"totp_secret"`
	WarmupUntil        *string `json:"warmup_until"`
	Note               *string `json:"note"`
	OauthRefreshStatus *string `json:"oauth_refresh_status"`
	OauthRefreshError  *string `json:"oauth_refresh_error"`
	OauthRefreshedAt   *string `json:"oauth_refreshed_at"`
	QuotaStatus        *string `json:"quota_status"`
	QuotaError         *string `json:"quota_error"`
	QuotaCheckedAt     *string `json:"quota_checked_at"`
	QuotaJSON          *string `json:"quota_json"`
	MailProvider       *string `json:"mail_provider"`
	Tag                *string `json:"tag"`
	ProfileSource      *string `json:"profile_source"`
	ProfileLastUsedAt  *string `json:"profile_last_used_at"`
}

type Registration struct {
	ID           int64   `json:"id"`
	Status       string  `json:"status"`
	Proxy        *string `json:"proxy"`
	Headless     bool    `json:"headless"`
	AccountID    *int64  `json:"account_id"`
	Error        *string `json:"error"`
	ResultJSON   *string `json:"result_json"`
	CreatedAt    string  `json:"created_at"`
	FinishedAt   *string `json:"finished_at"`
	LogsJSON     *string `json:"logs_json"`
	BindTotp     bool    `json:"bind_totp"`
	BatchID      *int64  `json:"batch_id"`
	GmailAlias   *string `json:"gmail_alias"`
	GmailMailID  *string `json:"gmail_mail_id"`
	DebugMode    bool    `json:"debug_mode"`
	DebugTrace   bool    `json:"debug_trace"`
	MailProvider *string `json:"mail_provider"`
}

type Batch struct {
	ID                   int64   `json:"id"`
	Status               string  `json:"status"`
	Target               int64   `json:"target"`
	Concurrency          int64   `json:"concurrency"`
	Proxy                *string `json:"proxy"`
	Headless             bool    `json:"headless"`
	BindTotp             bool    `json:"bind_totp"`
	Succeeded            int64   `json:"succeeded"`
	Failed               int64   `json:"failed"`
	CreatedAt            string  `json:"created_at"`
	FinishedAt           *string `json:"finished_at"`
	GmailMode            bool    `json:"gmail_mode"`
	LogsJSON             *string `json:"logs_json"`
	GmailOrdersCompleted int64   `json:"gmail_orders_completed"`
	DebugMode            bool    `json:"debug_mode"`
	DebugTrace           bool    `json:"debug_trace"`
}

type GmailSession struct {
	ID               int64   `json:"id"`
	BaseEmail        string  `json:"base_email"`
	MailID           string  `json:"mail_id"`
	AliasCounter     int64   `json:"alias_counter"`
	Status           string  `json:"status"`
	CreatedAt        string  `json:"created_at"`
	MaxAliases       int64   `json:"max_aliases"`
	ExpiresAt        *string `json:"expires_at"`
	ExpiredReason    *string `json:"expired_reason"`
	UpdatedAt        *string `json:"updated_at"`
	OtpTimeoutStreak int64   `json:"otp_timeout_streak"`
}

type Sub2APIReloginJob struct {
	ID          int64   `json:"id"`
	Status      string  `json:"status"`
	GroupIDs    *string `json:"group_ids"`
	Headless    bool    `json:"headless"`
	Concurrency int64   `json:"concurrency"`
	OnlyError   bool    `json:"only_error"`
	Total       int64   `json:"total"`
	Pending     int64   `json:"pending"`
	Success     int64   `json:"success"`
	Failed      int64   `json:"failed"`
	Skipped     int64   `json:"skipped"`
	Error       *string `json:"error"`
	LogsJSON    *string `json:"logs_json"`
	ConfigJSON  *string `json:"config_json"`
	CreatedAt   string  `json:"created_at"`
	FinishedAt  *string `json:"finished_at"`
}

type Sub2APIReloginItem struct {
	ID               int64   `json:"id"`
	JobID            int64   `json:"job_id"`
	RemoteAccountID  string  `json:"remote_account_id"`
	LocalAccountID   *int64  `json:"local_account_id"`
	Email            *string `json:"email"`
	RemoteStatus     *string `json:"remote_status"`
	RemoteError      *string `json:"remote_error"`
	Status           string  `json:"status"`
	Reason           *string `json:"reason"`
	Error            *string `json:"error"`
	ReauthEndpoint   *string `json:"reauth_endpoint"`
	CallbackEndpoint *string `json:"callback_endpoint"`
	StartedAt        *string `json:"started_at"`
	FinishedAt       *string `json:"finished_at"`
	ProxyID          *int64  `json:"proxy_id"`
}

type AccountSub2APIUpload struct {
	ID                int64   `json:"id"`
	AccountID         int64   `json:"account_id"`
	Email             *string `json:"email"`
	RemoteID          string  `json:"remote_id"`
	GroupID           int64   `json:"group_id"`
	GroupName         *string `json:"group_name"`
	Status            string  `json:"status"`
	RemoteStatus      *string `json:"remote_status"`
	RemoteError       *string `json:"remote_error"`
	HasAccessToken    *bool   `json:"has_access_token"`
	HasRefreshToken   *bool   `json:"has_refresh_token"`
	RemoteConcurrency *int64  `json:"remote_concurrency"`
	RemoteLoadFactor  *int64  `json:"remote_load_factor"`
	UploadedAt        *string `json:"uploaded_at"`
	VerifiedAt        *string `json:"verified_at"`
	LastError         *string `json:"last_error"`
	CreatedAt         string  `json:"created_at"`
	UpdatedAt         string  `json:"updated_at"`
}

type CustomMailbox struct {
	ID                  int64   `json:"id"`
	Address             string  `json:"address"`
	Active              bool    `json:"active"`
	Status              string  `json:"status"`
	AllocatedAt         *string `json:"allocated_at"`
	UsedAt              *string `json:"used_at"`
	LastError           *string `json:"last_error"`
	CreatedAt           string  `json:"created_at"`
	UpdatedAt           *string `json:"updated_at"`
	ReasonCode          *string `json:"reason_code"`
	LeaseOwner          *string `json:"lease_owner"`
	LeaseExpiresAt      *string `json:"lease_expires_at"`
	AttemptCount        int64   `json:"attempt_count"`
	RecycleCount        int64   `json:"recycle_count"`
	AccountID           *int64  `json:"account_id"`
	HasRefreshToken     *bool   `json:"has_refresh_token"`
	CredentialCheckedAt *string `json:"credential_checked_at"`
}

type LinkExtractionJob struct {
	ID                  int64   `json:"id"`
	Status              string  `json:"status"`
	Total               int64   `json:"total"`
	Pending             int64   `json:"pending"`
	Running             int64   `json:"running"`
	Succeeded           int64   `json:"succeeded"`
	Failed              int64   `json:"failed"`
	Canceled            int64   `json:"canceled"`
	Concurrency         int64   `json:"concurrency"`
	Country             *string `json:"country"`
	PaymentMethod       *string `json:"payment_method"`
	ApplyCheckoutUpdate bool    `json:"apply_checkout_update"`
	OaicsOnly           bool    `json:"oaics_only"`
	ConfigJSON          *string `json:"config_json"`
	LogsJSON            *string `json:"logs_json"`
	Error               *string `json:"error"`
	CreatedAt           string  `json:"created_at"`
	StartedAt           *string `json:"started_at"`
	FinishedAt          *string `json:"finished_at"`
}

type LinkExtractionItem struct {
	ID                int64    `json:"id"`
	JobID             int64    `json:"job_id"`
	AccountID         *int64   `json:"account_id"`
	Email             *string  `json:"email"`
	Status            string   `json:"status"`
	Stage             *string  `json:"stage"`
	Progress          *int64   `json:"progress"`
	SessionKind       *string  `json:"session_kind"`
	CheckoutSessionID *string  `json:"checkout_session_id"`
	Currency          *string  `json:"currency"`
	AmountDue         *float64 `json:"amount_due"`
	ProviderURL       *string  `json:"provider_url"`
	PaypalURL         *string  `json:"paypal_url"`
	GopayURL          *string  `json:"gopay_url"`
	GcashURL          *string  `json:"gcash_url"`
	ResultJSON        *string  `json:"result_json"`
	Error             *string  `json:"error"`
	NetworkError      bool     `json:"network_error"`
	StartedAt         *string  `json:"started_at"`
	FinishedAt        *string  `json:"finished_at"`
}

type UiSetting struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}

type Task struct {
	ID               int64   `json:"id"`
	Status           string  `json:"status"`
	Phone            *string `json:"phone"`
	Country          *int64  `json:"country"`
	Proxy            *string `json:"proxy"`
	ConcurrencyGroup string  `json:"concurrency_group"`
	Error            *string `json:"error"`
	CreatedAt        string  `json:"created_at"`
}

type SmsActivation struct {
	ID           int64    `json:"id"`
	TaskID       int64    `json:"task_id"`
	ActivationID *string  `json:"activation_id"`
	Phone        *string  `json:"phone"`
	Service      *string  `json:"service"`
	Status       *string  `json:"status"`
	Cost         *float64 `json:"cost"`
	Raw          *string  `json:"raw"`
}

type OAuthLog struct {
	ID        int64  `json:"id"`
	Seq       int64  `json:"seq"`
	Ts        string `json:"ts"`
	Msg       string `json:"msg"`
	CreatedAt string `json:"created_at"`
}

type Proxy struct {
	ID         int64   `json:"id"`
	URL        string  `json:"url"`
	Country    *string `json:"country"`
	Status     string  `json:"status"`
	UsedCount  int64   `json:"used_count"`
	LastUsedAt *string `json:"last_used_at"`
}

type HealthCheck struct {
	ID        int64   `json:"id"`
	AccountID *int64  `json:"account_id"`
	CheckType *string `json:"check_type"`
	Result    *string `json:"result"`
	Detail    *string `json:"detail"`
	CheckedAt *string `json:"checked_at"`
}