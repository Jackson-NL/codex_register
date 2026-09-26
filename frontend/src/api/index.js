// ============================================================
// API 客户端 —— 对接后端 FastAPI（http://127.0.0.1:8000）
// Vite dev server 的 /api 已 proxy 到后端
// ============================================================

const BASE = "/api";

async function request(path, options = {}) {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    ...options,
  });
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try {
      const text = await resp.text();
      if (text) {
        try {
          const body = JSON.parse(text);
          msg = typeof body.detail === "string"
            ? body.detail
            : JSON.stringify(body.detail || body);
        } catch {
          msg = text;
        }
      }
    } catch (e) {
      msg = e?.message || msg;
    }
    const error = new Error(msg);
    error.status = resp.status;
    throw error;
  }
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("application/json") ? resp.json() : resp.text();
}

function GET(path) {
  return request(path);
}

function POST(path, body, requestOptions = {}) {
  return request(path, { method: "POST", body: JSON.stringify(body), ...requestOptions });
}

function PUT(path, body) {
  return request(path, { method: "PUT", body: JSON.stringify(body) });
}

function PATCH(path, body) {
  return request(path, { method: "PATCH", body: JSON.stringify(body) });
}

function DEL(path) {
  return request(path, { method: "DELETE" });
}

// ============================================================
// 按资源分组
// ============================================================
export const api = {
  admin: {
    status: () => GET("/admin/status"),
    login: (key) => POST("/admin/login", { key }),
    session: () => GET("/admin/session"),
    overview: () => GET("/admin/overview"),
    logout: () => POST("/admin/logout", {}),
  },

  stats: {
    get: () => GET("/stats"),
  },

  accounts: {
    list: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.status) qs.set("status", params.status);
      if (params.q) qs.set("q", params.q);
      if (params.plan) qs.set("plan", params.plan);
      const s = qs.toString();
      return GET(`/accounts${s ? `?${s}` : ""}`);
    },
    importData: (body) => POST("/accounts/import", body),
    exportData: (body) => POST("/accounts/export", body),
    bulkTag: (body) => POST("/accounts/bulk-tag", body),
    detail: (id) => GET(`/accounts/${id}`),
    batch: (body) => POST("/accounts/batch", body),
    oauthCountries: () => GET("/accounts/oauth/countries"),
    oauthLogs: (after = 0, limit = 300) => GET(`/accounts/oauth/logs?after=${encodeURIComponent(after)}&limit=${encodeURIComponent(limit)}`),
    oauthJobActive: () => GET("/accounts/oauth/jobs/active"),
    oauthJob: (jobId) => GET(`/accounts/oauth/jobs/${encodeURIComponent(jobId)}`),
    startOAuthJob: (body = {}) => POST("/accounts/oauth/jobs", body),
    cancelOAuthJob: (jobId) => POST(`/accounts/oauth/jobs/${encodeURIComponent(jobId)}/cancel`),
    refreshOAuth: (id, body = { headless: false }, requestOptions) => POST(`/accounts/${id}/oauth/refresh-from-profile`, body, requestOptions),
    oauthDryRunPhone: (id, body = {}) => POST(`/accounts/${id}/oauth/dry-run-phone-from-profile`, body),
    oauthAutoPhone: (id, body = {}, requestOptions) => POST(`/accounts/${id}/oauth/auto-phone-from-profile`, body, requestOptions),
    oauthCompletePhone: (id, body = {}) => POST(`/accounts/${id}/oauth/complete-phone-from-profile`, body),
    del: (id) => DEL(`/accounts/${id}`),
    patch: (id, body) => PATCH(`/accounts/${id}`, body),
    writeTotp: (id, secret) => PATCH(`/accounts/${id}/totp`, { secret }),
  },

  sub2api: {
    groups: () => GET("/sub2api/groups"),
    upload: (body) => POST("/sub2api/upload", body),
    createUploadJob: (body) => POST("/sub2api/upload/jobs", body),
    uploadJob: (jobId) => GET(`/sub2api/upload/jobs/${encodeURIComponent(jobId)}`),
    syncUploadStatus: (body) => POST("/sub2api/upload-status/sync", body),
    uploadStatus: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.group_ids) qs.set("group_ids", Array.isArray(params.group_ids) ? params.group_ids.join(",") : params.group_ids);
      if (params.status && params.status !== "all") qs.set("status", params.status);
      if (params.q) qs.set("q", params.q);
      if (params.accountId) qs.set("account_id", String(params.accountId));
      qs.set("page", String(params.page || 1));
      qs.set("page_size", String(params.pageSize || 20));
      return GET(`/sub2api/upload-status?${qs.toString()}`);
    },
  },

  sub2apiRelogin: {
    preview: ({ group_ids = [], only_error = true } = {}) => {
      const ids = (Array.isArray(group_ids) ? group_ids : String(group_ids).split(/[,，\s]+/))
        .map(Number)
        .filter((id) => Number.isInteger(id) && id > 0);
      const qs = new URLSearchParams({ group_ids: ids.join(","), only_error: String(Boolean(only_error)) });
      return GET(`/sub2api/relogin/preview?${qs.toString()}`);
    },
    createJob: (body) => POST("/sub2api/relogin/jobs", body),
    jobs: () => GET("/sub2api/relogin/jobs"),
    job: (id) => GET(`/sub2api/relogin/jobs/${id}`),
    items: (id) => GET(`/sub2api/relogin/jobs/${id}/items`),
    logs: (id, after = 0) => GET(`/sub2api/relogin/jobs/${id}/logs?after=${encodeURIComponent(after)}`),
    cancel: (id) => POST(`/sub2api/relogin/jobs/${id}/cancel`),
  },

  linkExtraction: {
    accounts: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.q) qs.set("q", params.q);
      qs.set("has_token", String(params.hasToken !== false));
      qs.set("page", String(params.page || 1));
      qs.set("page_size", String(params.pageSize || 50));
      return GET(`/link-extraction/accounts?${qs.toString()}`);
    },
    createJob: (body) => POST("/link-extraction/jobs", body),
    jobs: () => GET("/link-extraction/jobs"),
    job: (id) => GET(`/link-extraction/jobs/${id}`),
    items: (id) => GET(`/link-extraction/jobs/${id}/items`),
    logs: (id, after = 0) => GET(`/link-extraction/jobs/${id}/logs?after=${encodeURIComponent(after)}`),
    cancel: (id) => POST(`/link-extraction/jobs/${id}/cancel`),
  },

  registrations: {
    create: (body) => POST("/registrations", body),
    list: (params = {}) => {
      const qs = params.limit ? `?limit=${params.limit}` : "";
      return GET(`/registrations${qs}`);
    },
    get: (id) => GET(`/registrations/${id}`),
    cancel: (id) => POST(`/registrations/${id}/cancel`),
    releaseDebug: (id) => POST(`/registrations/${id}/debug/release`),
    debugScreenshotUrl: (id) => `/api/registrations/${id}/debug/screenshot`,
    debugHar: (id, params = {}) => {
      const qs = new URLSearchParams();
      if (params.limit) qs.set("limit", params.limit);
      const s = qs.toString();
      return GET(`/registrations/${id}/debug/har${s ? `?${s}` : ""}`);
    },
    debugTraceUrl: (id) => `/api/registrations/${id}/debug/trace`,
    debugStatus: (id) => GET(`/registrations/${id}/debug/status`),
    logs: (id, params = {}) => {
      const qs = new URLSearchParams();
      if (params.after != null) qs.set("after", params.after);
      if (params.limit != null) qs.set("limit", params.limit);
      const s = qs.toString();
      return GET(`/registrations/${id}/logs${s ? `?${s}` : ""}`);
    },
    clearLogs: (id) => DEL(`/registrations/${id}/logs`),
    logRedact: () => GET("/registrations/log-redact"),
    setLogRedact: (enabled) => POST("/registrations/log-redact", { enabled }),
  },

  proxies: {
    list: (params = {}) => {
      const qs = new URLSearchParams();
      if (params.status) qs.set("status", params.status);
      if (params.q) qs.set("q", params.q);
      const s = qs.toString();
      return GET(`/proxies${s ? `?${s}` : ""}`);
    },
    create: (body) => POST("/proxies", body),
    patch: (id, body) => PATCH(`/proxies/${id}`, body),
    del: (id) => DEL(`/proxies/${id}`),
    test: (id) => POST(`/proxies/${id}/test`),
  },

  batches: {
    create: (body) => POST("/batches", body),
    get: (id) => GET(`/batches/${id}`),
    logs: (id, params = {}) => {
      const qs = new URLSearchParams();
      if (params.after != null) qs.set("after", params.after);
      if (params.limit != null) qs.set("limit", params.limit);
      return GET(`/batches/${id}/logs?${qs.toString()}`);
    },
    clearLogs: (id) => DEL(`/batches/${id}/logs`),
    cancel: (id) => POST(`/batches/${id}/cancel`),
  },

  gmailSessions: {
    rent: () => POST("/gmail-sessions/rent"),
    active: () => GET("/gmail-sessions/active"),
    list: () => GET("/gmail-sessions"),
    detail: (id) => GET(`/gmail-sessions/${id}`),
    release: (id) => POST(`/gmail-sessions/${id}/release`),
    nextAlias: () => POST("/gmail-sessions/next-alias"),
    prepareNextCode: (id) => POST(`/gmail-sessions/${id}/prepare-next-code`),
    expire: (id) => POST(`/gmail-sessions/${id}/expire`),
  },

  settings: {
    getUi: () => GET("/settings/ui"),
    putUi: (body) => PUT("/settings/ui", body),
    get: () => GET("/settings"),
    put: (body) => POST("/settings", body),
    testSmsbower: () => POST("/settings/smsbower/test"),
  },

  mailConfig: {
    get: () => GET("/mail-config"),
    save: (body) => POST("/mail-config", body),
    test: (body) => POST("/mail-config/test", body),
    poolRequeue: (body) => POST("/mail-config/pool/requeue", body),
    poolRelease: (body) => POST("/mail-config/pool/release", body),
    poolDisable: (body) => POST("/mail-config/pool/disable", body),
    poolEnable: (body) => POST("/mail-config/pool/enable", body),
    poolVerify: (body) => POST("/mail-config/pool/verify", body),
    poolRemove: (body) => POST("/mail-config/pool/remove", body),
  },
};
