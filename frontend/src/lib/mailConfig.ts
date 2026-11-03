export type MailProvider = "cf_temp_email" | "icloud";
export const PROVIDER_NAMES: Record<MailProvider, string> = {
  cf_temp_email: "Cloudflare",
  icloud: "iCloud",
};
export const POOL_STATUSES: Record<string, string> = {
  unused: "未使用",
  in_use: "使用中",
  used: "已使用",
  failed: "失败",
  disabled: "已停用",
};
export interface PoolItem {
  id: number;
  address: string;
  status: string;
  reason_code?: string;
  has_refresh_token?: boolean | null;
  attempt_count?: number;
  recycle_count?: number;
  lease_expires_at?: string | null;
}
export interface CfDraft {
  enabled: boolean;
  base_url: string;
  domain: string;
  address_mode: string;
  inbox_address: string;
  inbox_jwt: string;
  site_password: string;
  name_prefix: string;
  random_length: number;
  poll_interval: number;
  poll_timeout: number;
  max_retries: number;
  rate_limit_backoff: number;
}
export interface IcloudDraft {
  enabled: boolean;
  mode: string;
  poll_interval: number;
  poll_timeout: number;
  split_enabled: boolean;
  split_prefix: string;
  split_tag_length: number;
  split_max_per_base: number;
}
export interface MailDraft {
  provider: MailProvider;
  cf_temp_email: CfDraft;
  icloud: IcloudDraft;
}
export interface MailTestResult {
  ok: boolean;
  provider?: string;
  message: string;
  tested_at?: string;
}
export interface MailSnapshot {
  provider: MailProvider;
  cf_temp_email: Omit<CfDraft, "inbox_jwt" | "site_password"> & {
    has_inbox_jwt: boolean;
    has_site_password: boolean;
    custom_pool_count: number;
    custom_pool_items: PoolItem[];
    custom_pool_status_counts: Record<string, number>;
    custom_pool_summary: Record<string, number | boolean>;
  };
  icloud: IcloudDraft & {
    accounts_count: number;
    accounts_sample: string[];
    remaining_rounds: number;
    exhausted_roots: number;
  };
  updated_at?: string | null;
  test_status?: MailTestResult | null;
}
const CF_DEFAULTS: CfDraft = {
  enabled: true,
  base_url: "",
  domain: "",
  address_mode: "generated",
  inbox_address: "",
  inbox_jwt: "",
  site_password: "",
  name_prefix: "reg",
  random_length: 10,
  poll_interval: 4,
  poll_timeout: 180,
  max_retries: 3,
  rate_limit_backoff: 10,
};
const IC_DEFAULTS: IcloudDraft = {
  enabled: false,
  mode: "manual_pool",
  poll_interval: 4,
  poll_timeout: 180,
  split_enabled: false,
  split_prefix: "",
  split_tag_length: 6,
  split_max_per_base: 0,
};

export function createMailDraft(snapshot: MailSnapshot): MailDraft {
  // Explicit whitelist: counters, masked examples, and pool members never enter a configuration write.
  const cf = { ...CF_DEFAULTS };
  for (const key of Object.keys(cf) as (keyof CfDraft)[]) {
    if (
      key !== "inbox_jwt" &&
      key !== "site_password" &&
      key in snapshot.cf_temp_email
    ) {
      Object.assign(cf, { [key]: snapshot.cf_temp_email[key] });
    }
  }
  const ic = { ...IC_DEFAULTS };
  for (const key of Object.keys(ic) as (keyof IcloudDraft)[]) {
    if (key in snapshot.icloud)
      Object.assign(ic, { [key]: snapshot.icloud[key] });
  }
  return { provider: snapshot.provider, cf_temp_email: cf, icloud: ic };
}
function changed<T extends object>(draft: T, saved: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(draft).filter(
      ([key, value]) => value !== saved[key as keyof T],
    ),
  ) as Partial<T>;
}
export function buildMailPatch(draft: MailDraft, saved: MailDraft) {
  const cf = changed(draft.cf_temp_email, saved.cf_temp_email);
  for (const key of ["site_password", "inbox_jwt"] as const) {
    if (!cf[key]?.trim() || cf[key] === "••••••••") delete cf[key];
  }
  const icloud = changed(draft.icloud, saved.icloud);
  return {
    ...(draft.provider !== saved.provider ? { provider: draft.provider } : {}),
    ...(Object.keys(cf).length ? { cf_temp_email: cf } : {}),
    ...(Object.keys(icloud).length ? { icloud } : {}),
  };
}
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function httpUrl(value: string) {
  try {
    const url = new URL(value);
    return (
      ["http:", "https:"].includes(url.protocol) &&
      !!url.hostname &&
      !/\s/.test(value)
    );
  } catch {
    return false;
  }
}
export function validateMailProvider(
  provider: MailProvider,
  draft: MailDraft,
  snapshot: MailSnapshot,
): Record<string, string> {
  const errors: Record<string, string> = {};
  const config = draft[provider];
  const positive =
    provider === "cf_temp_email"
      ? [
          "random_length",
          "poll_interval",
          "poll_timeout",
          "max_retries",
          "rate_limit_backoff",
        ]
      : ["poll_interval", "poll_timeout", "split_tag_length"];
  for (const key of positive) {
    const value = config[key as keyof typeof config];
    if (typeof value !== "number" || !Number.isInteger(value) || value < 1)
      errors[key] = "请输入正整数";
  }
  if (provider === "cf_temp_email") {
    const cf = draft.cf_temp_email;
    if (!httpUrl(cf.base_url))
      errors.base_url = "请输入有效的 http(s) API 地址";
    if (!cf.domain.trim()) errors.domain = "域名不能为空";
    if (cf.address_mode === "custom_pool") {
      if (!snapshot.cf_temp_email.custom_pool_count)
        errors.pool = "请先导入 Cloudflare 自定义邮箱池";
      if (!EMAIL.test(cf.inbox_address))
        errors.inbox_address = "请输入有效的固定收件邮箱";
      if (!cf.inbox_jwt.trim() && !snapshot.cf_temp_email.has_inbox_jwt)
        errors.inbox_jwt = "请配置固定收件 JWT";
    }
  } else if (
    !Number.isInteger(draft.icloud.split_max_per_base) ||
    draft.icloud.split_max_per_base < 0
  ) {
    errors.split_max_per_base = "请输入非负整数，0 表示不限";
  }
  return errors;
}

export const IMPORT_MAX_BYTES = 256 * 1024;
export const IMPORT_MAX_ROWS = 5000;
export interface MailImportPreview {
  values: string[];
  duplicates: number;
  errors: string[];
}
export function parseMailImport(
  provider: MailProvider,
  text: string,
): MailImportPreview {
  const result: MailImportPreview = { values: [], duplicates: 0, errors: [] };
  if (new TextEncoder().encode(text).length > IMPORT_MAX_BYTES)
    return { ...result, errors: ["导入内容不能超过 256 KB"] };
  const lines =
    provider === "icloud" ? text.split(/\r?\n/) : text.split(/[\r\n,;]+/);
  if (lines.length > IMPORT_MAX_ROWS)
    return { ...result, errors: ["一次最多导入 5000 行"] };
  const seen = new Set<string>();
  lines.forEach((line, index) => {
    const value = line.trim();
    if (!value || value.startsWith("#")) return;
    const separator = value.indexOf("----");
    const email = (
      provider === "icloud"
        ? value.slice(0, separator < 0 ? value.length : separator)
        : value
    )
      .trim()
      .toLowerCase();
    const url = separator >= 0 ? value.slice(separator + 4).trim() : "";
    if (
      !EMAIL.test(email) ||
      (provider === "icloud" && (separator < 0 || !httpUrl(url)))
    ) {
      result.errors.push(
        `第 ${index + 1} 项格式错误${provider === "icloud" ? "，需要 邮箱----http(s)收信API" : "，需要有效邮箱地址"}`,
      );
    } else if (seen.has(email)) {
      result.duplicates++;
    } else {
      seen.add(email);
      // Email is canonicalized; URL tokens, case, separators and query strings must remain byte-for-byte unchanged.
      result.values.push(provider === "icloud" ? `${email}----${url}` : email);
    }
  });
  return result;
}
export function mailTime(value?: string | null): string {
  if (!value) return "暂无记录";
  const date = new Date(
    value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`,
  );
  return Number.isNaN(date.getTime())
    ? "暂无记录"
    : date.toLocaleString("zh-CN", { hour12: false });
}
