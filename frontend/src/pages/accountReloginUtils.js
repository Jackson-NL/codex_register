export const MAX_RELOGIN_ACCOUNTS = 20;
export const RELOGIN_JOB_STORAGE_KEY = "accountops-relogin-job-id";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const JOB_ID = /^[a-f\d-]{16,64}$/i;
const TOTP_SECRET = /^[A-Z2-7]{16,128}={0,6}$/;

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, Math.trunc(parsed))) : fallback;
}

export function parseReloginAccounts(form = {}) {
  const accounts = [];
  const errors = [];
  const seen = new Map();
  let rows;
  if (form.mode === "batch") {
    rows = String(form.batch || "").split(/\r?\n/).map((line, index) => ({ line, number: index + 1 })).filter(({ line }) => line.trim());
  } else {
    rows = [{ fields: [form.email || "", form.password || "", form.totp_secret || ""], number: 1 }];
  }
  if (!rows.length) return { accounts: [], errors: ["请输入邮箱和密码"] };
  if (rows.length > MAX_RELOGIN_ACCOUNTS) return { accounts: [], errors: [`每次最多 ${MAX_RELOGIN_ACCOUNTS} 个账号`] };
  for (const row of rows) {
    const fields = row.fields || row.line.split("----");
    const prefix = `第 ${row.number} 行`;
    if (fields.length < 2 || fields.length > 3) {
      errors.push(`${prefix}：格式应为邮箱----密码----2FA密钥`);
      continue;
    }
    const email = String(fields[0]).trim().toLowerCase();
    const password = String(fields[1]);
    const totp_secret = String(fields[2] || "").replace(/\s+/g, "").toUpperCase();
    if (!EMAIL.test(email) || email.length > 254) {
      errors.push(`${prefix}：邮箱格式无效`);
      continue;
    }
    if (!password || password.length > 512) {
      errors.push(`${prefix}：请输入 1–512 字符的密码`);
      continue;
    }
    if (totp_secret && !TOTP_SECRET.test(totp_secret)) {
      errors.push(`${prefix}：2FA 应为 Base32 密钥，不是六位验证码`);
      continue;
    }
    const account = { email, password, totp_secret };
    if (seen.has(email)) {
      const previous = seen.get(email);
      if (previous.password !== password || previous.totp_secret !== totp_secret) errors.push(`${prefix}：同一邮箱出现不同凭据，请仅保留一条`);
      continue;
    }
    seen.set(email, account);
    accounts.push(account);
  }
  return { accounts, errors };
}

export function buildReloginPayload(form = {}) {
  const { accounts, errors } = parseReloginAccounts(form);
  if (errors.length || !accounts.length) throw new Error(errors[0] || "请输入邮箱和密码");
  return {
    accounts,
    proxy: String(form.proxy || "").trim(),
    concurrency: boundedInteger(form.concurrency || 1, 1, 1, 3),
    timeout_s: boundedInteger(form.timeout_s || 90, 90, 30, 180),
  };
}

export function clearReloginSecrets(form) {
  return { ...form, password: "", totp_secret: "", batch: "" };
}

export function reloginJobActive(status) {
  return status === "pending" || status === "running";
}

export const RELOGIN_STATUS = {
  pending: { label: "排队中", color: "neutral" },
  running: { label: "进行中", color: "info" },
  succeeded: { label: "成功", color: "success" },
  partial: { label: "部分成功", color: "warning" },
  failed: { label: "失败", color: "danger" },
  canceled: { label: "已停止", color: "neutral" },
};

export const RELOGIN_STAGES = {
  pending: "等待执行", queued: "等待执行", starting: "准备连接",
  web_csrf: "初始化网页会话", web_authorize: "网页登录授权", email: "提交邮箱",
  password: "验证密码", totp: "验证 2FA", web_session: "验证网页会话",
  oauth_authorize: "Codex OAuth 授权", oauth_exchange: "交换 OAuth 凭据",
  saving: "保存账号凭据", done: "完成", completed: "完成", canceled: "已停止", failed: "失败",
};

export function redactReloginText(value, limit = 400) {
  return String(value || "")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [hidden]")
    .replace(/\beyJ[A-Za-z\d_-]+\.[A-Za-z\d_-]+\.[A-Za-z\d_-]+/g, "[hidden]")
    .replace(/(access_token|refresh_token|id_token|session_token|password|totp_secret|csrfToken|authorization)\s*[:=]\s*[^\s,;]+/gi, "$1=[hidden]")
    .slice(0, limit);
}

export function projectReloginJob(source) {
  if (!source || typeof source !== "object") return null;
  const count = (value) => Math.max(0, Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : 0);
  return {
    id: String(source.id || ""),
    status: Object.hasOwn(RELOGIN_STATUS, source.status) ? source.status : "pending",
    total: count(source.total), completed: count(source.completed), succeeded: count(source.succeeded), failed: count(source.failed),
    created_at: String(source.created_at || ""),
    items: (Array.isArray(source.items) ? source.items : []).slice(0, MAX_RELOGIN_ACCOUNTS).map((item) => ({
      id: String(item.id || ""), email: String(item.email || ""),
      status: Object.hasOwn(RELOGIN_STATUS, item.status) ? item.status : "pending",
      stage: Object.hasOwn(RELOGIN_STAGES, item.stage) ? item.stage : "pending",
      has_access_token: item.has_access_token === true,
      has_refresh_token: item.has_refresh_token === true,
      has_web_session: item.has_web_session === true,
      token_source: item.token_source === "oauth" || item.token_source === "codex" ? "oauth" : "web",
      error_code: String(item.error_code || ""), error: redactReloginText(item.error), warning: redactReloginText(item.warning),
      saved_account_id: Number.isInteger(item.saved_account_id) && item.saved_account_id > 0 ? item.saved_account_id : null,
      save_error: redactReloginText(item.save_error),
    })),
    logs: (Array.isArray(source.logs) ? source.logs : []).slice(-150).map((log) => ({
      time: typeof log === "object" && log ? String(log.time || log.at || log.timestamp || "") : "",
      message: redactReloginText(typeof log === "string" ? log : log?.message || log?.text),
    })),
  };
}

export function safeReloginError(error) {
  if (error?.status === 401 || error?.status === 403) return "需要管理员授权，请重新登录后重试";
  if (error?.status === 404) return "任务已过期或服务已重启，请重新提交";
  if (error?.status === 409) return "无法保存：已有凭据可能冲突，原账号凭据未被覆盖";
  if (error?.status === 422 || error?.status === 400) return "输入格式无效，请检查邮箱、密码和 2FA 密钥";
  if (error?.status === 429 || error?.status === 503) return "服务繁忙或任务已满，请稍后重试";
  return "请求失败，请检查服务状态后重试";
}

export function readReloginJobId(storage) {
  try {
    const id = storage?.getItem(RELOGIN_JOB_STORAGE_KEY);
    return typeof id === "string" && JOB_ID.test(id) ? id : null;
  } catch { return null; }
}

export function saveReloginJobId(id, storage) {
  try {
    if (typeof id === "string" && JOB_ID.test(id)) storage?.setItem(RELOGIN_JOB_STORAGE_KEY, id);
    else storage?.removeItem(RELOGIN_JOB_STORAGE_KEY);
  } catch { /* Storage may be unavailable in private browsing. */ }
}
