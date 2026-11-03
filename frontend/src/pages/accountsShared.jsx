// 账号管理页共享常量与小组件（从 Accounts.jsx 拆出，逻辑不变）
import { Loader2, Copy, Check } from "lucide-react";
import { fmtAgo } from "../mock/data";

export const PLAN_COLOR = { free: "neutral", plus: "info", pro: "primary", team: "warning", enterprise: "danger" };

// 凭据状态仅由 access token 是否存在及过期时间判断。
export const credState = (a) => {
  if (!a.has_access_token) return { label: "无凭证", color: "neutral" };
  if (a.token_expires_at && new Date(a.token_expires_at).getTime() <= Date.now()) return { label: "已过期", color: "danger" };
  return { label: "凭据有效", color: "success" };
};

// 账号状态仅两态：正常（active/cooling 自动恢复）与异常（paused/unhealthy/disabled 等一切不可用）
export const ACC_STATUS = {
  active: ["success", "正常"],
  cooling: ["success", "正常"],
};
export const accStatus = (a) =>
  ACC_STATUS[a.status] || ["danger", "异常"];

export const shortValue = (value, empty = "—") => value || empty;

export const tokenExpiryText = (a) => {
  if (!a.token_expires_at) return "未记录过期时间";
  const t = new Date(a.token_expires_at).getTime();
  if (Number.isNaN(t)) return "过期时间异常";
  if (t <= Date.now()) return `已于 ${fmtAgo(t)} 过期`;
  const s = Math.max(0, Math.floor((t - Date.now()) / 1000));
  if (s < 60) return `${s} 秒后过期`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟后过期`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时后过期`;
  return `${Math.floor(s / 86400)} 天后过期`;
};

export const credentialFilter = (a) => {
  const state = credState(a).label;
  if (state === "凭据有效") return "alive";
  if (state === "已过期") return "problem";
  return "missing";
};

// 筛选器键值定义：AccountsToolbar 与 Accounts 编排共享
export const FILTER_DEFS = {
  health: [
    { value: "all", label: "全部状态" },
    { value: "__normal", label: "正常" },
    { value: "__abnormal", label: "异常" },
  ],
  cred: [
    { value: "all", label: "全部凭据" },
    { value: "alive", label: "凭据有效" },
    { value: "problem", label: "已过期" },
    { value: "missing", label: "无凭证" },
  ],
  sub2api: [
    { value: "all", label: "全部 Sub2API" },
    { value: "not_uploaded", label: "未上传" },
    { value: "uploaded", label: "已上传" },
    { value: "partial", label: "部分上传" },
    { value: "error", label: "上传异常" },
    { value: "token_error", label: "No access token" },
  ],
  rt: [
    { value: "all", label: "全部 RT" },
    { value: "with_rt", label: "有 RT" },
    { value: "no_rt", label: "无 RT" },
  ],
};

export const StatPill = ({ label, value, tone = "slate", onClick, active, helper }) => {
  const tones = {
    blue: "border-blue-200 bg-blue-50 text-blue-700",
    emerald: "border-emerald-200 bg-emerald-50 text-emerald-700",
    amber: "border-amber-200 bg-amber-50 text-amber-700",
    red: "border-red-200 bg-red-50 text-red-700",
    slate: "border-slate-200 bg-white text-slate-700",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={`min-w-[128px] rounded-lg border px-3 py-2 text-left transition hover:-translate-y-px hover:shadow-sm ${tones[tone]} ${active ? "ring-2 ring-blue-200" : ""}`}
      aria-pressed={active ? "true" : "false"}
    >
      <div className="text-[11px] font-medium opacity-75">{label}</div>
      <div className="tnum mt-1 text-xl font-semibold leading-none">{value}</div>
      {helper && <div className="mt-1 truncate text-[11px] opacity-70">{helper}</div>}
    </button>
  );
};

export const CredentialLine = ({ label, masked, copyState, onCopy }) => {
  const hasValue = Boolean(masked);
  return (
    <div className={`flex min-w-0 items-center gap-2 rounded-md border px-2 py-1 ${hasValue ? "border-slate-200 bg-white" : "border-slate-100 bg-slate-50"}`}>
      <span className={`w-6 shrink-0 rounded px-1 py-0.5 text-center text-[10px] font-semibold ${hasValue ? "bg-blue-50 text-blue-600" : "bg-slate-100 text-slate-400"}`}>{label}</span>
      <span className={`mono min-w-0 flex-1 truncate ${hasValue ? "text-slate-700" : "text-slate-300"}`} title={hasValue ? `${label} 已保存（脱敏）` : `${label} 缺失`}>
        {shortValue(masked)}
      </span>
      {hasValue && <CopyBtn state={copyState} title={`复制 ${label}`} onCopy={onCopy} />}
    </div>
  );
};

// 复制按钮：busy 转圈 → done 打勾 1.6s → 恢复默认
export const CopyBtn = ({ state, title, onCopy }) => (
  <button
    type="button"
    className={`inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition-colors ${state === "done" ? "bg-emerald-50 text-emerald-600" : state === "busy" ? "cursor-wait text-slate-300" : "text-slate-400 hover:bg-slate-100 hover:text-slate-700"}`}
    title={state === "done" ? "已复制" : title}
    aria-label={state === "done" ? "已复制" : title}
    disabled={state === "busy"}
    onClick={(e) => { e.stopPropagation(); onCopy(); }}
  >
    {state === "busy" ? <Loader2 size={12} className="animate-spin" /> : state === "done" ? <Check size={12} /> : <Copy size={12} />}
  </button>
);

// localStorage 视图持久化
const VIEWS_STORAGE_KEY = "accountops-accounts-views";
export const loadSavedViews = () => {
  try {
    const raw = localStorage.getItem(VIEWS_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
};
export const persistSavedViews = (views) => {
  try { localStorage.setItem(VIEWS_STORAGE_KEY, JSON.stringify(views)); } catch { /* 存储不可用时静默 */ }
};
