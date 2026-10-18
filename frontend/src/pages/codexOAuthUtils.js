export const MAX_OAUTH_COUNTRIES = 3;
export const OAUTH_FORM_STORAGE_KEY = "codex_oauth_params_v1";
export const OAUTH_RUNTIME_STORAGE_KEY = "codex_oauth_runtime_v1";
export const MAX_PERSISTED_OAUTH_LOGS = 800;
export const MAX_RENDERED_OAUTH_LOGS = 400;
export const OAUTH_LOG_POLL_VISIBLE_MS = 800;
export const OAUTH_LOG_POLL_HIDDEN_MS = 5000;

import { api } from "../api/index.js";


export const OAUTH_COUNTRY_OPTIONS = Object.freeze([
  { value: "PH", label: "菲律宾 PH · +63" },
  { value: "ID", label: "印尼 ID · +62" },
  { value: "GB", label: "英国 GB · +44" },
  { value: "SA", label: "沙特阿拉伯 SA · +966" },
  { value: "BR", label: "巴西 BR · +55" },
  { value: "CO", label: "哥伦比亚 CO · +57" },
  { value: "US", label: "美国 US · +1" },
  { value: "CA", label: "加拿大 CA · +1" },
  { value: "MX", label: "墨西哥 MX · +52" },
  { value: "AR", label: "阿根廷 AR · +54" },
  { value: "CL", label: "智利 CL · +56" },
  { value: "PE", label: "秘鲁 PE · +51" },
  { value: "EC", label: "厄瓜多尔 EC · +593" },
  { value: "IN", label: "印度 IN · +91" },
  { value: "MY", label: "马来西亚 MY · +60" },
  { value: "TH", label: "泰国 TH · +66" },
  { value: "VN", label: "越南 VN · +84" },
  { value: "SG", label: "新加坡 SG · +65" },
  { value: "TR", label: "土耳其 TR · +90" },
  { value: "DE", label: "德国 DE · +49" },
  { value: "FR", label: "法国 FR · +33" },
  { value: "ES", label: "西班牙 ES · +34" },
  { value: "IT", label: "意大利 IT · +39" },
  { value: "NL", label: "荷兰 NL · +31" },
  { value: "PL", label: "波兰 PL · +48" },
  { value: "SE", label: "瑞典 SE · +46" },
  { value: "NO", label: "挪威 NO · +47" },
  { value: "AU", label: "澳大利亚 AU · +61" },
  { value: "NZ", label: "新西兰 NZ · +64" },
  { value: "ZA", label: "南非 ZA · +27" },
  { value: "NG", label: "尼日利亚 NG · +234" },
  { value: "EG", label: "埃及 EG · +20" },
  { value: "MA", label: "摩洛哥 MA · +212" },
  { value: "AE", label: "阿联酋 AE · +971" },
  { value: "QA", label: "卡塔尔 QA · +974" },
  { value: "KW", label: "科威特 KW · +965" },
  { value: "OM", label: "阿曼 OM · +968" },
]);

const OAUTH_COUNTRY_VALUES = new Set(OAUTH_COUNTRY_OPTIONS.map((country) => country.value));
const OAUTH_COUNTRY_ALIASES = {
  PH: "philippines filipino +63",
  ID: "indonesia +62",
  GB: "uk united kingdom england britain +44",
  SA: "saudi arabia +966",
  US: "usa united states america +1",
  SG: "singapore +65",
};

export const DEFAULT_OAUTH_PARAMS = Object.freeze({
  headless: true,
  countries: ["PH", "ID", "GB"],
  max_price: 0.03,
  low_price_first: false,
  max_phone_attempts: 0,
  sms_poll_timeout: 60,
  sms_poll_interval: 4,
  concurrency: 3,
  auto_rebuild_failed: true,
});

export function codexOAuthConsoleLayout() {
  return {
    primary: ["command", "monitor", "settings", "accounts", "results"],
    logPlacement: "above-fold",
    accountsDefaultCollapsed: true,
    defaultAccountPageSize: 10,
  };
}

export const OAUTH_DIRECT_STAGES = Object.freeze([
  { key: "profile", label: "准备 profile" },
  { key: "open", label: "打开 OAuth" },
  { key: "select", label: "选择账号" },
  { key: "exchange", label: "直接授权 / token exchange" },
  { key: "write", label: "写回账号" },
  { key: "done", label: "完成" },
]);

export const OAUTH_PHONE_STAGES = Object.freeze([
  { key: "profile", label: "准备 profile" },
  { key: "open", label: "打开 OAuth" },
  { key: "select", label: "选择账号" },
  { key: "add-phone", label: "进入 add-phone" },
  { key: "auto-phone", label: "租号/填号/短信/交换" },
  { key: "write", label: "写回账号" },
  { key: "done", label: "完成" },
]);

export function oauthStagesForMode(mode = "direct") {
  return mode === "phone" ? [...OAUTH_PHONE_STAGES] : [...OAUTH_DIRECT_STAGES];
}

export function oauthStageIndex(mode, key) {
  return Math.max(0, oauthStagesForMode(mode).findIndex((stage) => stage.key === key));
}

export function formatOAuthErrorMessage(error, limit = 1200) {
  return String(error?.message || error || "未知错误").slice(0, limit);
}

export function isOAuthJobMissingError(error) {
  const message = String(error?.message || error || "");
  return /OAuth job 不存在|HTTP\s*404|404\s*\(Not Found\)|Not Found/i.test(message);
}

export function shouldAutoScrollOAuthLogs({
  running = false,
  scrollTop = 0,
  clientHeight = 0,
  scrollHeight = 0,
  threshold = 48,
} = {}) {
  if (running) return true;
  return Number(scrollHeight) - Number(scrollTop) - Number(clientHeight) <= Number(threshold);
}

export function shouldFollowOAuthLogTail({
  running = false,
  stickToBottom = true,
  scrollTop = 0,
  clientHeight = 0,
  scrollHeight = 0,
  threshold = 48,
} = {}) {
  if (running || stickToBottom) return true;
  return shouldAutoScrollOAuthLogs({ running: false, scrollTop, clientHeight, scrollHeight, threshold });
}

export function shouldPollOAuthBackendLogs({ pageMounted = true } = {}) {
  // 后台轮询已与页面挂载解耦：只要存在运行中的任务或后台 job，就持续收集日志，
  // 这样在注册工作台等其它模块停留时，OAuth 日志也会持续累积，切回时不丢历史。
  return Boolean(pageMounted);
}

export function oauthBackendLogPollDelay({ hidden = false } = {}) {
  return hidden ? OAUTH_LOG_POLL_HIDDEN_MS : OAUTH_LOG_POLL_VISIBLE_MS;
}

export function trimOAuthLogs(logs, limit = MAX_PERSISTED_OAUTH_LOGS) {
  const items = Array.isArray(logs) ? logs : [];
  const size = Math.max(0, Number(limit) || 0);
  return size > 0 ? items.slice(-size) : [];
}

export function visibleOAuthLogs(logs, limit = MAX_RENDERED_OAUTH_LOGS) {
  return trimOAuthLogs(logs, limit);
}

export function oauthLogScrollSignal(logs) {
  const items = Array.isArray(logs) ? logs : [];
  const last = items.at(-1);
  if (!last) return "";
  return String(last.id || last.backend_seq || `${items.length}:${last.time || ""}:${last.message || ""}`);
}

// ------------------------------------------------------------------
// OAuth 后端实时日志「后台轮询」：与页面挂载解耦，独立于当前路由运行。
// 日志写入 codexOAuthRuntime 单例（跨路由切换存活），因此切换模块不会暂停收集。
// ------------------------------------------------------------------
let oauthBackendLogTimer = null;
let oauthBackendLogActive = false;
let oauthBackendLogErrShown = false;

export function shouldPollOAuthBackendLogsNow(snapshot = {}) {
  return Boolean(snapshot?.running || snapshot?.backendJobId);
}

function oauthBackendLogPollingNeeded() {
  return shouldPollOAuthBackendLogsNow(codexOAuthRuntime.getSnapshot());
}

export async function pollOAuthBackendLogsOnce() {
  const snapshot = codexOAuthRuntime.getSnapshot();
  const after = Number(snapshot.backendLogSeq || 0);
  let data;
  try {
    data = await api.accounts.oauthLogs(after, 300);
  } catch (error) {
    if (!oauthBackendLogErrShown) {
      oauthBackendLogErrShown = true;
      codexOAuthRuntime.appendLog({
        id: `poll-err-${Date.now()}`,
        time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
        message: `后端实时日志轮询失败：${error?.message || error}`,
        level: "warning",
      });
    }
    return;
  }
  oauthBackendLogErrShown = false;
  const items = Array.isArray(data?.items) ? data.items : [];
  const latestSeq = Number(data?.latest_seq || after);
  if (!items.length) {
    if (latestSeq < after) {
      // 后端进程重启导致内存游标归零，重置以便下次从头补齐新日志流。
      codexOAuthRuntime.patch({ backendLogSeq: 0 });
    } else if (latestSeq > after) {
      codexOAuthRuntime.patch((previous) => ({
        backendLogSeq: Math.max(Number(previous.backendLogSeq || 0), latestSeq),
      }));
    }
    return;
  }
  codexOAuthRuntime.patch((previous) => {
    const existingIds = new Set((previous.logs || []).map((log) => log.id));
    const nextLogs = [...(previous.logs || [])];
    let nextSeq = Math.max(Number(previous.backendLogSeq || 0), latestSeq);
    for (const item of items) {
      const log = projectOAuthBackendLog(item);
      nextSeq = Math.max(nextSeq, Number(log.backend_seq || 0));
      if (!existingIds.has(log.id)) {
        existingIds.add(log.id);
        nextLogs.push(log);
      }
    }
    return { logs: trimOAuthLogs(nextLogs), backendLogSeq: nextSeq };
  });
}

export function startOAuthBackendLogPolling() {
  if (typeof window === "undefined") return;
  if (oauthBackendLogActive) return;
  oauthBackendLogActive = true;
  oauthBackendLogErrShown = false;
  const loop = async () => {
    if (!oauthBackendLogActive) return;
    // 空闲（无运行任务/无 job）时不请求，但循环常驻；Job 一启动即可自动续上。
    if (oauthBackendLogPollingNeeded()) {
      try {
        await pollOAuthBackendLogsOnce();
      } catch {
        // 单轮异常忽略，下一轮继续。
      }
    }
    const hidden = typeof document !== "undefined" && document.visibilityState === "hidden";
    oauthBackendLogTimer = globalThis.setTimeout(loop, oauthBackendLogPollDelay({ hidden }));
  };
  loop();
}

export function stopOAuthBackendLogPolling() {
  oauthBackendLogActive = false;
  if (oauthBackendLogTimer) {
    globalThis.clearTimeout(oauthBackendLogTimer);
    oauthBackendLogTimer = null;
  }
}

export function oauthBackendLogLevel(message = "") {
  const text = String(message || "").toLowerCase();
  if (/实时补位任务共用节点|节点切换/.test(text)) {
    // OAuth deliberately falls back to the current proxy when rotation fails;
    // that is operationally a warning unless a lower-level error is logged.
    if (/实时补位任务共用节点/.test(text) && /\bok=false\b/.test(text)) return "warning";
    if (/\bok=false\b/.test(text)) return "error";
    if (/\bok=true\b/.test(text)) return "success";
  }
  if (/授权页已打开|等待回调|等待授权码|等待验证码|timeout=\d+(\.\d+)?s?/.test(text)) return "info";
  if (/手机号风控|phone risk|provider unavailable/.test(text)) return "warning";
  if (/代理\/网络异常|proxy\/network|ns_error_net_reset|err_(?:connection|proxy|tunnel|timed_out)/.test(text)) return "warning";
  const receivedCode = /收到(?:手机|短信)?验证码|(?:手机|短信)验证码已收到/.test(text);
  const missingCode = /(?:未|没有|未能)收到(?:手机|短信)?验证码/.test(text);
  if (receivedCode && !missingCode) return "success";
  // 没有符合条件的库存是正常轮询状态，不应伪装成流程失败。
  if (/暂无符合条件的手机号|本轮所有国家均未租到手机号|未租到(?:满足[^，。:：]*的)?手机号|无号|没有可用(?:的)?手机号|no[_ -]?numbers?|no available numbers?|无 <= .*候选 provider/.test(text)) return "info";
  // 换号/重试是预期内的中间过程，标黄色警告，不当作红色失败
  if (/准备换号|继续换号|需要换号|换号尝试|切换新号|重试|切换/.test(text)) return "warning";
  if (/失败|错误|异常|超时|error\s*=\s*\S+|\berror\b(?!\s*=)|exception|failed|timeout|traceback/.test(text)) return "error";
  // `Indonesia` contains the substring `done`; only treat standalone `done`
  // as a terminal success marker.
  if (/成功|完成|已写回|已捕获|success|completed|\bdone\b/.test(text)) return "success";
  if (/等待|warning|retry|wait|pending/.test(text)) return "warning";
  return "info";
}

export function projectOAuthBackendLog(item = {}) {
  const seq = Number(item.seq || 0);
  const message = String(item.msg ?? item.message ?? "");
  return {
    id: `backend-${seq || `${Date.now()}-${Math.random()}`}`,
    time: String(item.ts || item.time || ""),
    message,
    level: oauthBackendLogLevel(message),
    source: "backend",
    backend_seq: seq,
  };
}

function normalizeOAuthRuntimeLogs(logs) {
  return (Array.isArray(logs) ? logs : []).map((log) => {
    if (!log || typeof log !== "object") return log;
    if (log.source !== "backend" && !Number.isFinite(Number(log.backend_seq))) return log;
    return { ...log, level: oauthBackendLogLevel(log.message) };
  });
}

const DEFAULT_OAUTH_RUNTIME = Object.freeze({
  running: false,
  activeAction: "",
  currentTarget: null,
  currentStage: -1,
  currentFlow: "direct",
  runStatus: "pending",
  backendJobId: "",
  targetCount: 0,
  results: [],
  logs: [],
  backendLogSeq: 0,
  concurrency: DEFAULT_OAUTH_PARAMS.concurrency,
  activeAccountIds: [],
  rebuildRound: 1,
  rebuildRootJobId: "",
  rebuildPending: false,
});

function loadOAuthRuntime(storage) {
  const target = getStorage(storage);
  if (!target?.getItem) return null;
  try {
    const raw = target.getItem(OAUTH_RUNTIME_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function persistOAuthRuntime(state, storage) {
  const target = getStorage(storage);
  if (!target?.setItem) return;
  const snapshot = {
    running: Boolean(state.running),
    activeAction: String(state.activeAction || ""),
    currentTarget: state.currentTarget ?? null,
    currentStage: Number.isFinite(Number(state.currentStage)) ? Number(state.currentStage) : -1,
    currentFlow: String(state.currentFlow || "direct"),
    runStatus: String(state.runStatus || "pending"),
    backendJobId: String(state.backendJobId || ""),
    targetCount: Number.isFinite(Number(state.targetCount)) ? Number(state.targetCount) : 0,
    results: Array.isArray(state.results) ? state.results : [],
    logs: trimOAuthLogs(state.logs),
    backendLogSeq: Number.isFinite(Number(state.backendLogSeq)) ? Number(state.backendLogSeq) : 0,
    concurrency: Number.isFinite(Number(state.concurrency)) ? Number(state.concurrency) : DEFAULT_OAUTH_PARAMS.concurrency,
    activeAccountIds: Array.isArray(state.activeAccountIds) ? state.activeAccountIds : [],
  };
  try {
    target.setItem(OAUTH_RUNTIME_STORAGE_KEY, JSON.stringify(snapshot));
  } catch {
    // localStorage may be unavailable or full; runtime display must continue in memory.
  }
}

export function createOAuthRuntimeStore(initial = {}, storage) {
  const persisted = loadOAuthRuntime(storage) || {};
  let state = {
    ...DEFAULT_OAUTH_RUNTIME,
    ...persisted,
    ...initial,
    results: Array.isArray(initial.results)
      ? [...initial.results]
      : Array.isArray(persisted.results) ? [...persisted.results] : [],
    logs: trimOAuthLogs(
      Array.isArray(initial.logs)
        ? normalizeOAuthRuntimeLogs(initial.logs)
        : normalizeOAuthRuntimeLogs(persisted.logs),
    ),
  };
  // A persisted running flag may describe a browser tab that was closed. The
  // active-job recovery effect will restore it from the backend when needed.
  if (!initial.running && persisted.running) {
    state = { ...state, running: false, activeAction: "", runStatus: "stopped" };
  }
  let abortController = null;
  const listeners = new Set();

  const emit = () => {
    const snapshot = {
      ...state,
      results: [...state.results],
      logs: [...state.logs],
    };
    listeners.forEach((listener) => listener(snapshot));
  };

  return {
    getSnapshot() {
      return {
        ...state,
        results: [...state.results],
        logs: [...state.logs],
        backendLogSeq: Number(state.backendLogSeq || 0),
        backendJobId: String(state.backendJobId || ""),
      };
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    patch(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      state = { ...state, ...next };
      persistOAuthRuntime(state, storage);
      emit();
    },
    appendLog(log) {
      state = { ...state, logs: trimOAuthLogs([...state.logs, log]) };
      persistOAuthRuntime(state, storage);
      emit();
    },
    clearLogs() {
      state = { ...state, logs: [] };
      persistOAuthRuntime(state, storage);
      emit();
    },
    setAbortController(controller) {
      abortController = controller;
    },
    clearAbortController(controller) {
      if (!controller || abortController === controller) abortController = null;
    },
    abort() {
      abortController?.abort();
    },
  };
}

export const codexOAuthRuntime = createOAuthRuntimeStore();
// 模块加载即武装后台日志轮询：与组件生命周期解耦，空闲不自请求，Job 结束后可 stop。
startOAuthBackendLogPolling();

export function isOAuthJobRunning(status) {
  return ["pending", "running", "stopping"].includes(String(status || ""));
}

export function oauthRunStatusFromJob(status) {
  const value = String(status || "pending");
  if (value === "success") return "success";
  if (value === "failed") return "failed";
  if (value === "stopped" || value === "canceled" || value === "stopping") return "stopped";
  if (value === "running" || value === "pending") return "running";
  return "pending";
}

export function projectOAuthJob(job = {}) {
  const status = String(job?.status || "pending");
  const projected = {
    backendJobId: String(job?.job_id || ""),
    running: isOAuthJobRunning(status),
    activeAction: isOAuthJobRunning(status) ? "codex-oauth" : "",
    runStatus: oauthRunStatusFromJob(status),
    currentTarget: job?.current_account_id ?? null,
    currentFlow: String(job?.current_flow || "direct"),
    currentStage: Number.isFinite(Number(job?.current_stage)) ? Number(job.current_stage) : -1,
    concurrency: Number.isFinite(Number(job?.concurrency)) ? Number(job.concurrency) : DEFAULT_OAUTH_PARAMS.concurrency,
    activeAccountIds: Array.isArray(job?.active_account_ids) ? job.active_account_ids.map(Number).filter(Number.isInteger) : [],
    rebuildRound: Number.isFinite(Number(job?.rebuild_round)) && Number(job.rebuild_round) > 0 ? Number(job.rebuild_round) : 1,
    rebuildRootJobId: String(job?.rebuild_root_job_id || ""),
    rebuildPending: Boolean(job?.rebuild_pending),
  };
  if (Array.isArray(job?.results)) {
    projected.results = job.results.map((item) => ({ ...projectOAuthResult(item), status: item.status || "success", error: item.error || "" }));
  }
  if (Array.isArray(job?.account_ids)) projected.targetCount = job.account_ids.length;
  return projected;
}

export function staleOAuthJobPatch() {
  return {
    running: false,
    activeAction: "",
    runStatus: "stopped",
    backendJobId: "",
    currentTarget: null,
    currentStage: -1,
    activeAccountIds: [],
  };
}

function numberOrDefault(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function integerInRange(value, fallback, min, max) {
  const parsed = Math.trunc(numberOrDefault(value, fallback));
  return Math.min(max, Math.max(min, parsed));
}

function normalizeCountryValue(country) {
  const raw = String(country || "").trim();
  if (!raw) return "";
  if (/^smsbower:\d+$/i.test(raw)) return raw.toLowerCase();
  if (/^\d+$/.test(raw)) return `smsbower:${raw}`;
  const iso = raw.toUpperCase();
  return OAUTH_COUNTRY_VALUES.has(iso) ? iso : "";
}

function normalizeCountries(value) {
  const source = Array.isArray(value) ? value : String(value ?? "").split(/[\s,，]+/);
  const countries = [...new Set(source.map(normalizeCountryValue).filter(Boolean))];
  return (countries.length ? countries : [...DEFAULT_OAUTH_PARAMS.countries]).slice(0, MAX_OAUTH_COUNTRIES);
}

export function toggleOAuthCountry(current, country, options = OAUTH_COUNTRY_OPTIONS) {
  const allowed = new Set((options || []).map((option) => normalizeCountryValue(option.value)).filter(Boolean));
  const normalized = normalizeCountries(current).filter((value) => !allowed.size || allowed.has(value));
  const value = normalizeCountryValue(country);
  if (!value || (allowed.size && !allowed.has(value))) return normalized;
  if (normalized.includes(value)) return normalized.filter((item) => item !== value);
  return [...normalized, value].slice(-MAX_OAUTH_COUNTRIES);
}

export function buildOAuthPayload(values = {}) {
  return {
    headless: values.headless ?? DEFAULT_OAUTH_PARAMS.headless,
    countries: normalizeCountries(values.countries ?? DEFAULT_OAUTH_PARAMS.countries),
    max_price: numberOrDefault(values.max_price, DEFAULT_OAUTH_PARAMS.max_price),
    low_price_first: Boolean(values.low_price_first ?? DEFAULT_OAUTH_PARAMS.low_price_first),
    max_phone_attempts: Math.trunc(numberOrDefault(values.max_phone_attempts, DEFAULT_OAUTH_PARAMS.max_phone_attempts)),
    sms_poll_timeout: numberOrDefault(values.sms_poll_timeout, DEFAULT_OAUTH_PARAMS.sms_poll_timeout),
    sms_poll_interval: numberOrDefault(values.sms_poll_interval, DEFAULT_OAUTH_PARAMS.sms_poll_interval),
    concurrency: integerInRange(values.concurrency, DEFAULT_OAUTH_PARAMS.concurrency, 1, 10),
    auto_rebuild_failed: Boolean(values.auto_rebuild_failed ?? DEFAULT_OAUTH_PARAMS.auto_rebuild_failed),
  };
}

export function createOAuthFormValues(values = {}) {
  const payload = buildOAuthPayload(values);
  return {
    headless: payload.headless,
    countries: payload.countries,
    max_price: String(payload.max_price),
    low_price_first: payload.low_price_first,
    max_phone_attempts: String(payload.max_phone_attempts),
    sms_poll_timeout: String(payload.sms_poll_timeout),
    sms_poll_interval: String(payload.sms_poll_interval),
    concurrency: String(payload.concurrency),
    auto_rebuild_failed: payload.auto_rebuild_failed,
  };
}

function getStorage(storage) {
  if (storage) return storage;
  if (typeof window !== "undefined") {
    try {
      return window.localStorage;
    } catch {
      return null;
    }
  }
  return null;
}

export function saveOAuthForm(values = {}, storage) {
  const target = getStorage(storage);
  const form = createOAuthFormValues(values);
  if (target?.setItem) target.setItem(OAUTH_FORM_STORAGE_KEY, JSON.stringify(form));
  return form;
}

export function loadSavedOAuthForm(storage) {
  const target = getStorage(storage);
  if (!target?.getItem) return null;
  const raw = target.getItem(OAUTH_FORM_STORAGE_KEY);
  if (!raw) return null;
  try {
    return createOAuthFormValues(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function getOAuthTargets(accounts, selectedIds, typedId) {
  const byId = new Map((accounts || []).map((account) => [Number(account.id), account]));
  const eligibleIds = new Set(
    (accounts || []).filter((account) => isOAuthCandidate(account)).map((account) => Number(account.id)),
  );
  const selected = [...new Set((selectedIds || []).map(Number))].filter((id) => eligibleIds.has(id));
  if (selected.length) return selected;

  const id = Number.parseInt(String(typedId || "").trim(), 10);
  if (Number.isInteger(id) && id > 0 && byId.has(id)) {
    // 手动输入的账号存在但不合格时返回空，由调用方提示阻止原因，不发送任务。
    return eligibleIds.has(id) ? [id] : [];
  }
  if (Number.isInteger(id) && id > 0) return [];
  return (accounts || [])
    .filter(isOAuthCandidate)
    .map((account) => Number(account.id))
    .filter((accountId) => Number.isInteger(accountId) && accountId > 0);
}

// 找出用户明确指定（勾选或手输）但不符合 OAuth 资格的账号，用于前端提示。
export function findBlockedOAuthTargets(accounts, selectedIds, typedId) {
  const byId = new Map((accounts || []).map((account) => [Number(account.id), account]));
  const explicitIds = (selectedIds || []).length
    ? (selectedIds || []).map(Number)
    : [Number.parseInt(String(typedId || "").trim(), 10)];
  const blocked = [];
  for (const id of explicitIds) {
    if (!Number.isInteger(id) || id <= 0) continue;
    const account = byId.get(id);
    if (account && !isOAuthCandidate(account)) blocked.push(account);
  }
  return blocked;
}

export function oauthBlockMessage(account = {}) {
  if (account?.oauth_block_reason) return account.oauth_block_reason;
  const provider = String(account?.mail_provider || "unknown").trim().toLowerCase();
  if (!provider || provider === "unknown") return "该账号邮箱来源未知，不能进入 Codex OAuth";
  if (provider !== "gmail") return "该账号不是 Gmail 来源，不能进入 Codex OAuth";
  if (String(account?.status || "").trim().toLowerCase() === "cooling") return "该账号仍在冷却期，不能进入 Codex OAuth";
  if (!account?.profile_path) return "该账号缺少 profile，不能进入 Codex OAuth";
  if (account?.has_refresh_token) return "该账号已有 refresh_token，不能进入 Codex OAuth";
  return "该账号不能进入 Codex OAuth";
}

export function getOAuthPendingCount(targetCount, results = []) {
  const normalizedTargetCount = Number.isFinite(Number(targetCount))
    ? Math.max(0, Math.trunc(Number(targetCount)))
    : 0;
  const successCount = (results || []).filter((result) => result?.status === "success").length;
  return Math.max(0, normalizedTargetCount - successCount);
}

export function isOAuthCandidate(account = {}) {
  // 优先使用后端统一策略给出的 oauth_eligible；字段缺失时默认 fail closed，
  // 绝不把来源未知/旧数据账号当成候选（前端不做邮箱域名推断）。
  if (typeof account?.oauth_eligible !== "boolean") return false;
  return account.oauth_eligible;
}

export function oauthMailProvider(account = {}) {
  return String(account?.mail_provider || "unknown").trim().toLowerCase() || "unknown";
}

export function oauthRowStatusLabel(account = {}) {
  if (isOAuthCandidate(account)) return "OAuth 候选";
  const provider = oauthMailProvider(account);
  if (provider === "unknown") return "来源未知，已跳过";
  if (provider !== "gmail") return "非 Gmail，已跳过";
  if (String(account.status || "").trim().toLowerCase() === "cooling") return "冷却中，已跳过";
  if (!account.profile_path) return "缺少 profile，已跳过";
  if (account.has_refresh_token) return "已有 refresh_token，已跳过";
  return "已跳过";
}

export function summarizeOAuthAccounts(accounts = []) {
  return (accounts || []).reduce((summary, account) => {
    summary.total += 1;
    if (account.profile_path) summary.withProfile += 1;
    else summary.withoutProfile += 1;
    if (account.has_refresh_token) summary.withRefreshToken += 1;
    if (isOAuthCandidate(account)) summary.eligible += 1;
    return summary;
  }, {
    total: 0,
    eligible: 0,
    withProfile: 0,
    withRefreshToken: 0,
    withoutProfile: 0,
  });
}

export function filterOAuthAccounts(accounts = [], { query = "", onlyEligible = true } = {}) {
  const normalizedQuery = String(query || "").trim().toLowerCase();
  return (accounts || []).filter((account) => {
    if (onlyEligible && !isOAuthCandidate(account)) return false;
    if (!normalizedQuery) return true;
    return `${account.id} ${account.email || ""} ${account.profile_path || ""} ${account.plan_type || ""}`
      .toLowerCase()
      .includes(normalizedQuery);
  });
}

export function paginateOAuthAccounts(accounts = [], page = 1, pageSize = 20) {
  const source = Array.isArray(accounts) ? accounts : [];
  const size = Number.isInteger(Number(pageSize)) && Number(pageSize) > 0 ? Number(pageSize) : 20;
  const total = source.length;
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, Number.isInteger(Number(page)) ? Number(page) : 1), pages);
  const start = (current - 1) * size;
  const rows = source.slice(start, start + size);
  return {
    rows,
    page: current,
    pageSize: size,
    pages,
    total,
    from: total ? start + 1 : 0,
    to: start + rows.length,
  };
}

export function filterOAuthCountryOptions(options = [], query = "") {
  const normalizedQuery = String(query || "").trim().toLowerCase();
  if (!normalizedQuery) return options || [];
  return (options || []).filter((option) => {
    const value = String(option?.value || "");
    const label = String(option?.label || option?.name || "");
    const alias = OAUTH_COUNTRY_ALIASES[value.toUpperCase()] || "";
    return `${value} ${label} ${alias}`.toLowerCase().includes(normalizedQuery);
  });
}

export function projectOAuthResult(result = {}) {
  return {
    id: result.id,
    email: result.email || "",
    phone: result.phone || "",
    has_access_token: Boolean(result.has_access_token),
    has_refresh_token: Boolean(result.has_refresh_token),
    token_expires_at: result.token_expires_at || null,
    completed_at: result.completed_at || null,
    plan_type: result.plan_type || "",
    profile_path: result.profile_path || "",
    error_type: result.error_type || "",
  };
}

export function shouldFallbackToAutoPhone(error) {
  const text = String(error?.message || error || "").toLowerCase();
  return text.includes("add-phone")
    || text.includes("phone number required")
    || text.includes("手机验证")
    || text.includes("auto-phone-from-profile");
}
