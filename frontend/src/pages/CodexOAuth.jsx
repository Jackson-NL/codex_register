import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ClipboardList,
  Loader2,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Square,
  Terminal,
  XCircle,
} from "lucide-react";
import {
  Badge,
  Button,
  Checkbox,
  Empty,
  ErrorState,
  Input,
  Loading,
  PageHeader,
  Panel,
  Pagination,
  StageStepper,
  Switch,
} from "../components/ui";
import { useApp } from "../context/AppContext";
import { api } from "../api";
import {
  DEFAULT_OAUTH_PARAMS,
  MAX_OAUTH_COUNTRIES,
  OAUTH_COUNTRY_OPTIONS,
  buildOAuthPayload,
  codexOAuthConsoleLayout,
  codexOAuthRuntime,
  createOAuthFormValues,
  filterOAuthAccounts,
  filterOAuthCountryOptions,
  findBlockedOAuthTargets,
  formatOAuthErrorMessage,
  getOAuthPendingCount,
  isOAuthCandidate,
  isOAuthJobMissingError,
  loadSavedOAuthForm,
  oauthBlockMessage,
  oauthMailProvider,
  oauthLogScrollSignal,
  oauthRowStatusLabel,
  oauthStageIndex,
  oauthStagesForMode,
  visibleOAuthLogs,
  paginateOAuthAccounts,
  saveOAuthForm,
  summarizeOAuthAccounts,
  toggleOAuthCountry,
  getOAuthTargets,
  projectOAuthResult,
  projectOAuthJob,
  startOAuthBackendLogPolling,
  stopOAuthBackendLogPolling,
  shouldAutoScrollOAuthLogs,
  shouldFollowOAuthLogTail,
  shouldFallbackToAutoPhone,
  staleOAuthJobPatch,
} from "./codexOAuthUtils";

const INITIAL_FORM = createOAuthFormValues(DEFAULT_OAUTH_PARAMS);
const CONSOLE_LAYOUT = codexOAuthConsoleLayout();

function statusMeta(status) {
  if (status === "success") return { color: "success", label: "成功", icon: CheckCircle2 };
  if (status === "failed") return { color: "danger", label: "失败", icon: XCircle };
  if (status === "stopped") return { color: "warning", label: "已停止", icon: Square };
  if (status === "pending") return { color: "neutral", label: "待执行", icon: ClipboardList };
  return { color: "info", label: "运行中", icon: Loader2 };
}

function timestamp() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function formatOAuthResultTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
}

function resultRow(result, status = "success", error = "") {
  return { ...projectOAuthResult(result), status, error };
}

function MetricCard({ label, value, sub, tone = "slate" }) {
  const toneClass = {
    blue: "border-blue-100 bg-blue-50/70 text-blue-700",
    emerald: "border-emerald-100 bg-emerald-50/70 text-emerald-700",
    amber: "border-amber-100 bg-amber-50/70 text-amber-700",
    slate: "border-slate-200 bg-white text-slate-700",
  }[tone];
  return (
    <div className={`rounded-lg border px-3 py-2.5 ${toneClass}`}>
      <div className="text-[11px] font-medium opacity-70">{label}</div>
      <div className="tnum mt-1 text-xl font-semibold leading-none">{value}</div>
      {sub && <div className="mt-1 text-[11px] opacity-70">{sub}</div>}
    </div>
  );
}

function CountryMultiSelect({ value, onChange, options = OAUTH_COUNTRY_OPTIONS, disabled, t = (value) => value }) {
  const [open, setOpen] = useState(false);
  const [countryQuery, setCountryQuery] = useState("");
  const ref = useRef(null);
  const selected = Array.isArray(value) ? value : [];
  const optionMap = useMemo(() => new Map(options.map((option) => [option.value, option])), [options]);
  const selectedOptions = selected.map((item) => optionMap.get(item) || { value: item, label: item });
  const visibleOptions = useMemo(() => filterOAuthCountryOptions(options, countryQuery), [options, countryQuery]);

  useEffect(() => {
    const close = (event) => { if (ref.current && !ref.current.contains(event.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const chooseCountry = (country) => {
    onChange(toggleOAuthCountry(selected, country, options));
  };

  return (
    <div className="relative" ref={ref}>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="block text-xs font-medium text-slate-600">{t("国家优先级")}</span>
        <span className="text-[11px] text-slate-400">{t(`已载入 ${options.length} 个 · 最多 ${MAX_OAUTH_COUNTRIES} 个`)}</span>
      </div>
      <button
        type="button"
        disabled={disabled}
        aria-expanded={open}
        onClick={() => setOpen((next) => !next)}
        className="input flex min-h-[48px] w-full items-center justify-between gap-2 bg-white text-left disabled:opacity-50"
      >
        <span className="flex min-w-0 flex-1 flex-wrap gap-1.5">
          {selectedOptions.length === 0 ? (
            <span className="text-slate-400">{t("请选择国家")}</span>
          ) : selectedOptions.map((option, index) => (
            <span key={`${option.value}-${index}`} className="inline-flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-700">
              <span className="tnum text-blue-500">{index + 1}</span>{t(option.label)}
            </span>
          ))}
        </span>
        <ChevronDown size={14} className="shrink-0 text-slate-400" />
      </button>
      <div className="mt-1 text-[11px] leading-relaxed text-slate-400">{t("先试第 1 个国家；同一轮价位候选会并发取号；超过 3 个时自动保留最新 3 个。")}</div>
      {open && (
        <div className="absolute z-40 mt-1 w-full overflow-hidden rounded-lg border border-slate-200 bg-white shadow-xl">
          <div className="border-b border-slate-100 p-2">
            <div className="relative">
              <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                autoFocus
                value={countryQuery}
                onChange={(event) => setCountryQuery(event.target.value)}
                placeholder={t("搜索国家 / 区号 / SMSBower id")}
                className="input h-9 pl-8 text-xs"
              />
            </div>
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {visibleOptions.length === 0 ? (
              <div className="px-3 py-8 text-center text-xs text-slate-400">{t("没有匹配国家")}</div>
            ) : visibleOptions.map((option) => {
              const checked = selected.includes(option.value);
              const blocked = !checked && selected.length >= MAX_OAUTH_COUNTRIES;
              return (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => chooseCountry(option.value)}
                  className={`flex min-h-[40px] w-full items-center justify-between gap-3 px-3 py-2 text-left text-[13px] transition-colors hover:bg-slate-50 ${checked ? "text-blue-700" : "text-slate-700"} ${blocked ? "opacity-70" : ""}`}
                  title={blocked ? t("已选择 3 个，继续选择会自动移除最早的国家") : undefined}
                >
                  <span className="min-w-0 truncate">{t(option.label)}</span>
                  <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border text-[10px] ${checked ? "border-blue-600 bg-blue-600 text-white" : "border-slate-300 text-transparent"}`}>✓</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default function CodexOAuth() {
  const { toast, t } = useApp();
  const [accounts, setAccounts] = useState([]);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [accountsError, setAccountsError] = useState("");
  const [query, setQuery] = useState("");
  const [onlyEligible, setOnlyEligible] = useState(true);
  const [selectedIds, setSelectedIds] = useState([]);
  const [accountPage, setAccountPage] = useState(1);
  const [accountPageSize, setAccountPageSize] = useState(CONSOLE_LAYOUT.defaultAccountPageSize);
  const [accountsOpen, setAccountsOpen] = useState(!CONSOLE_LAYOUT.accountsDefaultCollapsed);
  const [accountId, setAccountId] = useState("");
  const [form, setForm] = useState(() => loadSavedOAuthForm() || INITIAL_FORM);
  const [countryOptions, setCountryOptions] = useState(OAUTH_COUNTRY_OPTIONS);
  const [runtime, setRuntime] = useState(() => codexOAuthRuntime.getSnapshot());
  const mountedRef = useRef(false);
  const logPanelRef = useRef(null);
  const logStickRef = useRef(true);
  const [logFollowing, setLogFollowing] = useState(true);
  const { running, activeAction, currentTarget, currentStage, currentFlow, runStatus, results, logs, backendJobId, targetCount, concurrency, activeAccountIds, rebuildRound, rebuildPending } = runtime;
  const renderedLogs = useMemo(() => visibleOAuthLogs(logs), [logs]);
  const hiddenLogCount = Math.max(0, logs.length - renderedLogs.length);
  const logScrollSignal = useMemo(() => oauthLogScrollSignal(logs), [logs]);

  const scrollOAuthLogToBottom = useCallback(() => {
    const panel = logPanelRef.current;
    if (!panel) return;
    panel.scrollTop = panel.scrollHeight;
  }, []);

  const updateOAuthLogStickiness = useCallback(() => {
    const panel = logPanelRef.current;
    if (!panel) return;
    const atBottom = shouldAutoScrollOAuthLogs({
      running: false,
      scrollTop: panel.scrollTop,
      clientHeight: panel.clientHeight,
      scrollHeight: panel.scrollHeight,
    });
    logStickRef.current = atBottom;
    setLogFollowing(atBottom);
  }, []);

  const loadAccounts = useCallback(async () => {
    setLoadingAccounts(true);
    setAccountsError("");
    try {
      const data = await api.accounts.list();
      setAccounts(Array.isArray(data) ? data : []);
    } catch (error) {
      setAccountsError(error?.message || "账号列表加载失败");
    } finally {
      setLoadingAccounts(false);
    }
  }, []);

  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  useEffect(() => {
    mountedRef.current = true;
    const unsubscribe = codexOAuthRuntime.subscribe(setRuntime);
    setRuntime(codexOAuthRuntime.getSnapshot());
    return () => {
      mountedRef.current = false;
      unsubscribe();
    };
  }, []);

  useLayoutEffect(() => {
    const panel = logPanelRef.current;
    if (!panel || logs.length === 0) return undefined;
    const follow = shouldFollowOAuthLogTail({
      running,
      stickToBottom: logStickRef.current,
      scrollTop: panel.scrollTop,
      clientHeight: panel.clientHeight,
      scrollHeight: panel.scrollHeight,
    });
    if (!follow) return undefined;

    let settleFrame = null;
    let settleTimer = null;
    scrollOAuthLogToBottom();
    const frame = window.requestAnimationFrame(() => {
      scrollOAuthLogToBottom();
      // A wrapped log row can change the scroll height after the first layout.
      // Follow once more so the newest line remains visible after remounts.
      settleFrame = window.requestAnimationFrame(() => {
        scrollOAuthLogToBottom();
      });
      settleTimer = window.setTimeout(scrollOAuthLogToBottom, 80);
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (settleFrame !== null) window.cancelAnimationFrame(settleFrame);
      if (settleTimer !== null) window.clearTimeout(settleTimer);
    };
  }, [logScrollSignal, running, scrollOAuthLogToBottom]);

  const appendLog = useCallback((message, level = "info") => {
    codexOAuthRuntime.appendLog({ id: `${Date.now()}-${Math.random()}`, time: timestamp(), message, level });
  }, []);

  const applyJobSnapshot = useCallback((job) => {
    if (!job) return;
    codexOAuthRuntime.patch(projectOAuthJob(job));
  }, []);


  useEffect(() => {
    let alive = true;
    api.accounts.oauthJobActive()
      .then((job) => {
        if (!alive) return;
        if (!job) {
          const snapshot = codexOAuthRuntime.getSnapshot();
          if (!snapshot.backendJobId) return;
          api.accounts.oauthJob(snapshot.backendJobId)
            .then((persistedJob) => {
              if (!alive) return;
              applyJobSnapshot(persistedJob);
              if (persistedJob?.running) startOAuthBackendLogPolling();
            })
            .catch((error) => {
              if (!alive) return;
              if (isOAuthJobMissingError(error)) {
                codexOAuthRuntime.patch(staleOAuthJobPatch());
                appendLog(`后台 OAuth 任务已不存在，保留历史运行结果：job_${snapshot.backendJobId}`, "warning");
                return;
              }
              appendLog(`历史 OAuth 任务恢复失败，保留当前结果：${formatOAuthErrorMessage(error)}`, "warning");
            });
          return;
        }
        applyJobSnapshot(job);
        startOAuthBackendLogPolling();
      })
      .catch((error) => {
        if (alive) appendLog(`后台 OAuth 任务状态恢复失败：${formatOAuthErrorMessage(error)}`, "warning");
      });
    return () => { alive = false; };
  }, [appendLog, applyJobSnapshot]);

  useEffect(() => {
    if (!backendJobId) return undefined;
    let stopped = false;
    let timer = null;
    const tick = async () => {
      try {
        const job = await api.accounts.oauthJob(backendJobId);
        if (stopped) return;
        applyJobSnapshot(job);
        if (job?.running) {
          timer = window.setTimeout(tick, 1200);
          return;
        }
        // 本轮已结束：若失败账号自动补跑，后端已重建下一轮 job（新 job_id）。
        // 有活跃 job 则接管跟随（可能是补跑下一轮，也可能是用户手动新任务）；
        // 否则若旧 job 仍标记 rebuild_pending（冷却等待中），继续轮询直至下一轮出现。
        const active = await api.accounts.oauthJobActive();
        if (stopped) return;
        if (active) {
          applyJobSnapshot(active);
          timer = window.setTimeout(tick, 1200);
          return;
        }
        const refresh = await api.accounts.oauthJob(backendJobId);
        if (stopped) return;
        if (refresh?.rebuild_pending) {
          timer = window.setTimeout(tick, 2000);
          return;
        }
        if (mountedRef.current) loadAccounts();
      } catch (error) {
        if (isOAuthJobMissingError(error)) {
          codexOAuthRuntime.patch(staleOAuthJobPatch());
          appendLog(`后台 OAuth 任务已不存在，已清除运行标记并保留结果：job_${backendJobId}`, "warning");
          return;
        }
        appendLog(`后台 OAuth 任务状态轮询失败：${formatOAuthErrorMessage(error)}`, "warning");
        if (!stopped) timer = window.setTimeout(tick, 3000);
      }
    };
    tick();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [appendLog, applyJobSnapshot, backendJobId, loadAccounts, running]);

  const seedBackendLogCursor = useCallback(async () => {
    try {
      const data = await api.accounts.oauthLogs(0, 1);
      const latestSeq = Number(data?.latest_seq || 0);
      codexOAuthRuntime.patch((previous) => ({
        backendLogSeq: Math.max(Number(previous.backendLogSeq || 0), latestSeq),
      }));
    } catch (error) {
      appendLog(`后端实时日志游标初始化失败：${formatOAuthErrorMessage(error)}`, "warning");
    }
  }, [appendLog]);

  // 后台日志轮询已在 codexOAuthUtils 模块加载时武装（常驻、空闲不自请求、与组件生命周期解耦），
  // 这里不再依赖 appendLog/mountedRef 去启停，避免重渲染干扰轮询。

  // 任务结束后停止后台轮询，但保留 runtime 中的最终结果和历史 job id。
  useEffect(() => {
    if (!running) stopOAuthBackendLogPolling();
  }, [running]);

  useEffect(() => {
    let alive = true;
    api.accounts.oauthCountries()
      .then((data) => {
        if (!alive || !Array.isArray(data) || data.length === 0) return;
        setCountryOptions(data.map((item) => {
          const rawValue = String(item.value);
          const value = /^\d+$/.test(rawValue) ? `smsbower:${rawValue}` : rawValue;
          return { value, label: item.label || item.name || value };
        }));
      })
      .catch((error) => appendLog(`国家列表加载失败，使用内置列表：${formatOAuthErrorMessage(error)}`, "warning"));
    return () => { alive = false; };
  }, [appendLog]);

  const accountSummary = useMemo(() => summarizeOAuthAccounts(accounts), [accounts]);
  const filteredAccounts = useMemo(() => filterOAuthAccounts(accounts, { query, onlyEligible }), [accounts, onlyEligible, query]);
  const accountPager = useMemo(() => paginateOAuthAccounts(filteredAccounts, accountPage, accountPageSize), [filteredAccounts, accountPage, accountPageSize]);
  const visibleAccountRows = accountPager.rows;

  useEffect(() => {
    setAccountPage(1);
  }, [query, onlyEligible]);

  const allVisibleSelected = visibleAccountRows.length > 0 && visibleAccountRows.every((account) => selectedIds.includes(account.id));
  const someVisibleSelected = visibleAccountRows.some((account) => selectedIds.includes(account.id));
  const selectedVisibleCount = visibleAccountRows.filter((account) => selectedIds.includes(account.id)).length;

  const setField = (field, value) => setForm((previous) => ({ ...previous, [field]: value }));

  const saveOAuthParams = () => {
    try {
      const saved = saveOAuthForm(form);
      setForm(saved);
      toast("OAuth 参数已保存", "success");
    } catch (error) {
      toast(`保存 OAuth 参数失败: ${error?.message || error}`, "error");
    }
  };

  const resetOAuthParams = () => {
    setForm(INITIAL_FORM);
    toast("OAuth 参数已恢复默认，点击保存后下次生效", "info");
  };

  const toggleSelected = (id, checked) => {
    setSelectedIds((previous) => checked ? [...new Set([...previous, id])] : previous.filter((value) => value !== id));
  };

  const toggleAllVisible = (checked) => {
    if (!checked) {
      setSelectedIds((previous) => previous.filter((id) => !visibleAccountRows.some((account) => account.id === id)));
      return;
    }
    setSelectedIds((previous) => [...new Set([...previous, ...visibleAccountRows.map((account) => account.id)])]);
  };

  const selectAccount = (account) => {
    setAccountId(String(account.id));
    setSelectedIds([]);
  };

  const setStage = (flow, stageKey, id, message) => {
    codexOAuthRuntime.patch({
      currentFlow: flow,
      currentTarget: id,
      currentStage: oauthStageIndex(flow, stageKey),
    });
    if (message) appendLog(`[acc_${id}] ${message}`);
  };

  const runForTarget = async (id, payload, signal) => {
    let flow = "direct";
    setStage("direct", "profile", id, `准备 profile；headless=${payload.headless}`);
    setStage("direct", "open", id, "打开 OAuth 授权页并复用当前 profile");
    setStage("direct", "select", id, "确认当前 profile 中的登录账号");
    let response;
    try {
      setStage("direct", "exchange", id, "直接 OAuth 授权并等待 token exchange");
      response = await api.accounts.refreshOAuth(id, payload, { signal });
    } catch (error) {
      if (!shouldFallbackToAutoPhone(error)) throw error;
      flow = "phone";
      appendLog(`[acc_${id}] 直接 OAuth 返回 add-phone：${formatOAuthErrorMessage(error)}`, "warning");
      setStage("phone", "add-phone", id, "进入 add-phone 分支");
      setStage(
        "phone",
        "auto-phone",
        id,
        `启动手机号补 OAuth：countries=${payload.countries.join(",")} max_price=${payload.max_price} low_price_first=${payload.low_price_first} max_phone_attempts=${payload.max_phone_attempts} sms_timeout=${payload.sms_poll_timeout}s sms_interval=${payload.sms_poll_interval}s；后端将在同一浏览器会话内执行租号、填号、等短信、提交验证码和 token exchange`,
      );
      response = await api.accounts.oauthAutoPhone(id, payload, { signal });
    }
    setStage(flow, "write", id, "写回账号 OAuth 字段");
    const safeResult = projectOAuthResult({ ...response, completed_at: response.completed_at || new Date().toISOString() });
    setStage(flow, "done", id, `完成：email=${safeResult.email || "—"} access_token=${safeResult.has_access_token ? "yes" : "no"} refresh_token=${safeResult.has_refresh_token ? "yes" : "no"} expires=${safeResult.token_expires_at || "—"} plan=${safeResult.plan_type || "—"}`);
    return safeResult;
  };

  const runOAuth = async () => {
    if (codexOAuthRuntime.getSnapshot().running) return;
    // 前端先按后端资格拦截一次；后端仍会强制拒绝，这里只做提前提示。
    const blockedAccounts = findBlockedOAuthTargets(accounts, selectedIds, accountId);
    if (blockedAccounts.length) {
      const message = blockedAccounts.map(oauthBlockMessage).join("；");
      toast(message, "warning");
      appendLog(`已阻止启动：${message}`, "warning");
      return;
    }
    const targets = getOAuthTargets(accounts, selectedIds, accountId);
    if (!targets.length) {
      toast("请输入有效 account_id，或从账号列表选择账号", "warning");
      return;
    }

    const payload = buildOAuthPayload(form);
    await seedBackendLogCursor();
    startOAuthBackendLogPolling();
    codexOAuthRuntime.patch({
      running: true,
      activeAction: "codex-oauth",
      runStatus: "running",
      backendJobId: "",
      currentFlow: "direct",
      currentStage: -1,
      currentTarget: null,
      targetCount: targets.length,
      concurrency: payload.concurrency,
      activeAccountIds: [],
      results: [],
    });
    appendLog(`开始 Codex 授权 OAuth：${targets.length} 个账号；targets=${targets.map((id) => `acc_${id}`).join(",")}`);
    appendLog("流程：后端 job 执行；先直接 OAuth 授权，如果返回 add-phone，则在同一浏览器会话内手机号补 OAuth");
    appendLog(`参数：headless=${payload.headless} concurrency=${payload.concurrency} countries=${payload.countries.join(",")} max_price=${payload.max_price} low_price_first=${payload.low_price_first} max_phone_attempts=${payload.max_phone_attempts} sms_poll_timeout=${payload.sms_poll_timeout}s sms_poll_interval=${payload.sms_poll_interval}s`);

    try {
      const job = await api.accounts.startOAuthJob({ ...payload, account_ids: targets });
      applyJobSnapshot(job);
      appendLog(`后台 Codex OAuth 任务已创建：job_${job.job_id}`, "success");
    } catch (error) {
      const message = formatOAuthErrorMessage(error);
      try {
        const existing = JSON.parse(message);
        if (existing?.job_id) {
          applyJobSnapshot(existing);
          appendLog(`已有后台 Codex OAuth 任务在运行：job_${existing.job_id}，已接管并恢复停止按钮`, "warning");
          toast("已有 Codex OAuth 任务在运行，已接管当前任务", "warning");
          return;
        }
      } catch {
        // 非 JSON 错误按普通启动失败处理。
      }
      codexOAuthRuntime.patch({ running: false, activeAction: "", runStatus: "failed", backendJobId: "" });
      appendLog(`创建后台 Codex OAuth 任务失败：${message}`, "error");
      toast(`Codex OAuth 启动失败：${message}`, "error");
    }
  };

  const stopOAuth = async () => {
    const snapshot = codexOAuthRuntime.getSnapshot();
    const jobId = snapshot.backendJobId || backendJobId;
    if (!snapshot.running && !jobId) return;
    codexOAuthRuntime.abort();
    if (!jobId) {
      codexOAuthRuntime.patch({ running: false, activeAction: "", runStatus: "stopped" });
      appendLog("已停止当前页面等待", "warning");
      return;
    }
    codexOAuthRuntime.patch({ runStatus: "stopped" });
    appendLog(`正在请求后端停止 Codex OAuth：job_${jobId}`, "warning");
    try {
      const job = await api.accounts.cancelOAuthJob(jobId);
      applyJobSnapshot(job);
      toast("Codex OAuth 已发送停止请求", "warning");
    } catch (error) {
      if (isOAuthJobMissingError(error)) {
        codexOAuthRuntime.patch(staleOAuthJobPatch());
        appendLog(`后端已没有这个 Codex OAuth 任务，已本地停止并清除旧状态：job_${jobId}`, "warning");
        toast("旧 Codex OAuth 任务已清除", "warning");
        return;
      }
      appendLog(`停止 Codex OAuth 失败：${formatOAuthErrorMessage(error)}`, "error");
      toast(`停止失败：${formatOAuthErrorMessage(error)}`, "error");
    }
  };

  const selectedCount = selectedIds.length;
  const autoTargetCount = accountSummary.eligible;
  const targetLabel = selectedCount ? `已选择 ${selectedCount} 个账号` : accountId.trim() ? `单账号 acc_${accountId.trim()}` : `自动候选 ${autoTargetCount} 个`;
  const runStatusMeta = statusMeta(runStatus);
  const StatusIcon = runStatusMeta.icon;
  const payloadPreview = buildOAuthPayload(form);
  const activeStages = useMemo(() => oauthStagesForMode(currentFlow), [currentFlow]);
  const successCount = results.filter((result) => result.status === "success").length;
  const failureCount = results.filter((result) => result.status === "failed").length;
  const pendingTargetCount = targetCount > 0 ? targetCount : getOAuthTargets(accounts, selectedIds, accountId).length;
  const pendingCount = getOAuthPendingCount(pendingTargetCount, results);
  const activeLabel = activeAccountIds?.length ? activeAccountIds.map((id) => `acc_${id}`).join(", ") : currentTarget ? `acc_${currentTarget}` : "—";

  return (
    <div className="space-y-3">
      <PageHeader
        title="Codex OAuth"
        subtitle="一个入口：能直接授权就直接授权；需要手机号时自动补手机号后继续授权"
        badge={<Badge color="info" dot>OAuth 操作台</Badge>}
        extra={<Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={loadAccounts} loading={loadingAccounts}>刷新账号</Button>}
      />

      <Panel className="sticky top-0 z-20 border-blue-100 shadow-sm" pad={false}>
        <div className="flex flex-wrap items-center justify-between gap-3 bg-white px-3 py-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-blue-50 text-blue-600">
              <StatusIcon size={16} className={runStatus === "running" ? "animate-spin" : ""} />
            </div>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-semibold text-slate-800">Codex 授权 OAuth</span>
                <Badge color={runStatusMeta.color} dot>{runStatusMeta.label}</Badge>
                {rebuildRound > 1 && <Badge color="info" dot>补跑第 {rebuildRound} 轮</Badge>}
                <Badge color="neutral">{targetLabel}</Badge>
              </div>
              <div className="mt-1 flex flex-wrap gap-1.5 text-[11px] text-slate-500">
                <span className="rounded bg-slate-100 px-1.5 py-0.5">国家 {payloadPreview.countries.join(" → ")}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5">价格 ≤ {payloadPreview.max_price}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{t(payloadPreview.low_price_first ? "低价优先" : "到码优先")}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{t(`短信 ${payloadPreview.sms_poll_timeout}s/${payloadPreview.sms_poll_interval}s`)}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{t(`并发 ${payloadPreview.concurrency}`)}</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5">{t(payloadPreview.headless ? "无头" : "有头")}</span>
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" icon={<Save size={13} />} onClick={saveOAuthParams} disabled={running}>保存参数</Button>
            <Button variant="dangerSoft" icon={<Square size={13} />} onClick={stopOAuth} disabled={!running}>停止</Button>
            <Button size="lg" icon={<RefreshCw size={15} />} onClick={runOAuth} loading={running} disabled={running}>Codex 授权 OAuth</Button>
          </div>
        </div>
      </Panel>

      <div className="grid gap-3 xl:grid-cols-[minmax(0,1.2fr)_minmax(340px,0.8fr)]">
        <Panel
          title="实时打印日志"
          extra={(
            <div className="flex items-center gap-2">
              {running && <Badge color="success" dot>{t("运行中 · 自动滚动到最新")}</Badge>}
              {!running && !logFollowing && <Button variant="ghost" size="sm" icon={<ChevronDown size={12} />} onClick={() => { logStickRef.current = true; setLogFollowing(true); scrollOAuthLogToBottom(); }}>回到底部</Button>}
              <Button variant="ghost" size="sm" icon={<Terminal size={12} />} onClick={() => codexOAuthRuntime.clearLogs()}>清空</Button>
            </div>
          )}
          pad={false}
          className={`overflow-hidden ${running ? "sticky top-[88px] z-10 border-blue-200 shadow-xl ring-1 ring-blue-100" : ""}`}
        >
          <div ref={logPanelRef} onScroll={updateOAuthLogStickiness} className={`${running ? "max-h-[calc(100vh-190px)]" : "max-h-[540px]"} min-h-[360px] overflow-y-auto bg-[#0d1117] px-3 py-2`}>
            {logs.length === 0 ? <div className="flex min-h-[344px] items-center justify-center text-[11px] text-slate-600">暂无 OAuth 日志；点击右上角按钮开始</div> : (
              <>
                {hiddenLogCount > 0 && (
                  <div className="sticky top-0 z-10 mb-1 rounded bg-slate-900/95 px-2 py-1 font-mono text-[11px] text-slate-500">
                    已隐藏较早 {hiddenLogCount} 行，仅渲染最近 {renderedLogs.length} 行；完整历史可查后端 OAuth 日志。
                  </div>
                )}
                {renderedLogs.map((log) => (
                  <div key={log.id} className="flex items-start gap-2 py-px font-mono text-[11.5px] leading-[1.7]">
                    <span className={`mt-[7px] h-1 w-1 shrink-0 rounded-full ${log.level === "error" ? "bg-red-500" : log.level === "success" ? "bg-emerald-500" : log.level === "warning" ? "bg-amber-500" : "bg-slate-500"}`} />
                    <span className="shrink-0 text-slate-600">{log.time}</span>
                    <span className={`min-w-0 whitespace-pre-wrap break-all ${log.level === "error" ? "text-red-400" : log.level === "success" ? "text-emerald-400" : log.level === "warning" ? "text-amber-400" : "text-slate-300"}`}>{log.message}</span>
                  </div>
                ))}
              </>
            )}
          </div>
        </Panel>

        <div className="space-y-3">
          <Panel title="运行状态" extra={<span className="text-[11px] text-slate-400">{t(`活跃账号 ${activeLabel} · ${concurrency} 并发`)}</span>}>
            <div className="overflow-x-auto pb-1">
              <div className="min-w-[460px]"><StageStepper compact stages={activeStages} current={currentStage} status={runStatus === "running" ? "running" : runStatus === "success" ? "success" : runStatus === "failed" ? "failed" : "pending"} /></div>
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2">
              <MetricCard label="成功" value={successCount} tone="emerald" />
              <MetricCard label="失败" value={failureCount} tone="amber" />
              <MetricCard label="待处理" value={pendingCount} tone="blue" />
            </div>
          </Panel>
          <Panel title="授权参数" extra={<Badge color="neutral">保存后复用</Badge>}>
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap gap-3">
                  <Switch checked={form.headless} onChange={(value) => setField("headless", value)} disabled={running} label="无头模式" />
                  <Switch checked={form.low_price_first} onChange={(value) => setField("low_price_first", value)} disabled={running} label="接码低价优先" />
                  <Switch checked={form.auto_rebuild_failed !== false} onChange={(value) => setField("auto_rebuild_failed", value)} disabled={running} label="失败自动补跑下一轮(跑完为止)" />
                </div>
                <div className="flex gap-2">
                  <Button variant="secondary" size="sm" icon={<Save size={13} />} onClick={saveOAuthParams} disabled={running}>保存</Button>
                  <Button variant="ghost" size="sm" icon={<RotateCcw size={13} />} onClick={resetOAuthParams} disabled={running}>默认</Button>
                </div>
              </div>
              <CountryMultiSelect value={form.countries} options={countryOptions} onChange={(countries) => setField("countries", countries)} disabled={running} t={t} />
              <div className="grid gap-2 sm:grid-cols-2">
                <Input label="价格上限" type="number" min="0" step="0.001" value={form.max_price} onChange={(event) => setField("max_price", event.target.value)} />
                <Input label="换号次数" type="number" min="0" step="1" value={form.max_phone_attempts} onChange={(event) => setField("max_phone_attempts", event.target.value)} hint="0 = 成功为止" />
                <Input label="短信超时" type="number" min="0" step="1" value={form.sms_poll_timeout} onChange={(event) => setField("sms_poll_timeout", event.target.value)} />
                <Input label="轮询间隔" type="number" min="0" step="1" value={form.sms_poll_interval} onChange={(event) => setField("sms_poll_interval", event.target.value)} />
                <Input label="并发数" type="number" min="1" max="10" step="1" value={form.concurrency} onChange={(event) => setField("concurrency", event.target.value)} hint={t("1–10；按批次共用代理节点")} />
              </div>
            </div>
          </Panel>

        </div>
      </div>

      <Panel
        title="账号队列"
        extra={(
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] text-slate-400">{accountsOpen ? `显示 ${accountPager.from}-${accountPager.to} / 筛选 ${filteredAccounts.length}` : `已折叠 · OAuth 候选 ${accountSummary.eligible} / 总数 ${accountSummary.total}`}</span>
            <Button variant="ghost" size="sm" icon={<ChevronDown size={13} className={accountsOpen ? "rotate-180 transition-transform" : "transition-transform"} />} onClick={() => setAccountsOpen((open) => !open)}>{accountsOpen ? "收起" : "展开"}</Button>
          </div>
        )}
        pad={false}
      >
        {!accountsOpen ? (
          <div className="grid gap-2 p-3 sm:grid-cols-2 lg:grid-cols-5">
            <MetricCard label="账号总数" value={accountSummary.total} sub="账号管理同源" />
            <MetricCard label="OAuth 候选" value={accountSummary.eligible} sub="Gmail 且符合条件" tone="blue" />
            <MetricCard label="已有 token" value={accountSummary.withRefreshToken} sub="默认跳过" tone="emerald" />
            <MetricCard label="无 profile" value={accountSummary.withoutProfile} sub="需先补 profile" tone="amber" />
            <MetricCard label="当前页选择" value={selectedVisibleCount} sub={`总选择 ${selectedCount}`} />
          </div>
        ) : (
          <>
            <div className="border-b border-slate-200 p-3">
              <div className="grid gap-3 lg:grid-cols-[minmax(180px,0.45fr)_minmax(260px,1fr)_auto]">
                <Input
                  label="单个 account_id"
                  value={accountId}
                  onChange={(event) => { setAccountId(event.target.value); setSelectedIds([]); }}
                  placeholder="例如 123"
                  inputMode="numeric"
                  icon={<Search size={14} />}
                />
                <Input
                  label="搜索账号"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="ID / email / profile_path / plan"
                  icon={<Search size={14} />}
                />
                <div className="flex items-end">
                  <Button variant="secondary" icon={<ClipboardList size={13} />} onClick={() => setSelectedIds((previous) => [...new Set([...previous, ...visibleAccountRows.map((account) => account.id)])])} disabled={running || visibleAccountRows.length === 0}>选择当前页</Button>
                </div>
              </div>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
                <Switch checked={onlyEligible} onChange={setOnlyEligible} disabled={running} label="只看 OAuth 候选" />
                <div className="flex flex-wrap gap-2 text-[11px] text-slate-500">
                  <span className="rounded bg-slate-100 px-2 py-1">不手动选择时自动拿候选账号</span>
                  <span className="rounded bg-slate-100 px-2 py-1">候选 = Gmail 来源 + 有 profile + 无 refresh_token</span>
                </div>
              </div>
            </div>

            {loadingAccounts && <Loading rows={5} cols={4} label="加载账号列表…" />}
            {!loadingAccounts && accountsError && <ErrorState message={accountsError} onRetry={loadAccounts} />}
            {!loadingAccounts && !accountsError && filteredAccounts.length === 0 && (
              <Empty title="没有匹配账号" desc={onlyEligible ? "当前只显示 OAuth 候选；关闭筛选可以查看全部账号" : "调整搜索条件后再试"} />
            )}
            {!loadingAccounts && !accountsError && filteredAccounts.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[860px] text-left">
                  <thead>
                    <tr className="border-b border-slate-200 bg-slate-50/70">
                      <th className="w-10 px-3 py-2"><Checkbox checked={allVisibleSelected} indeterminate={!allVisibleSelected && someVisibleSelected} onChange={toggleAllVisible} /></th>
                      <th className="th">ID</th>
                      <th className="th">email</th>
                      <th className="th">邮箱来源</th>
                      <th className="th">profile</th>
                      <th className="th">OAuth</th>
                      <th className="th">expires</th>
                      <th className="w-16 px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {visibleAccountRows.map((account) => {
                      const eligible = isOAuthCandidate(account);
                      return (
                        <tr key={account.id} className={`tr-row ${selectedIds.includes(account.id) ? "bg-blue-50/60" : ""}`}>
                          <td className="td"><Checkbox checked={selectedIds.includes(account.id)} onChange={(checked) => toggleSelected(account.id, checked)} /></td>
                          <td className="td mono text-slate-700">{account.id}</td>
                          <td className="td max-w-[190px] truncate text-slate-600" title={account.email || ""}>{account.email || "—"}</td>
                          <td className="td mono text-xs text-slate-500">{oauthMailProvider(account)}</td>
                          <td className="td max-w-[220px] truncate mono text-slate-500" title={account.profile_path || ""}>{account.profile_path || "—"}</td>
                          <td className="td">
                            <Badge color={eligible ? "info" : "neutral"} dot>{oauthRowStatusLabel(account)}</Badge>
                          </td>
                          <td className="td tnum text-xs text-slate-500">{account.token_expires_at || "—"}</td>
                          <td className="td"><Button variant="ghost" size="sm" icon={<ClipboardList size={12} />} onClick={() => selectAccount(account)} title="填入单账号">使用</Button></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {!loadingAccounts && !accountsError && filteredAccounts.length > 0 && (
              <Pagination
                page={accountPager.page}
                pages={accountPager.pages}
                total={accountPager.total}
                pageSize={accountPager.pageSize}
                onPage={setAccountPage}
                onPageSize={(size) => { setAccountPageSize(size); setAccountPage(1); }}
              />
            )}
          </>
        )}
      </Panel>

      <Panel title="运行结果" extra={<span className="text-[11px] text-slate-400">结果区下沉；日志优先展示</span>} pad={false}>
        {results.length === 0 ? <Empty title="暂无 OAuth 结果" desc="开始后这里只汇总最终账号状态" /> : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1040px] text-left">
              <thead><tr className="border-b border-slate-200 bg-slate-50/70"><th className="th">状态</th><th className="th">成功时间</th><th className="th">account_id</th><th className="th">email</th><th className="th">access_token</th><th className="th">refresh_token</th><th className="th">手机号</th><th className="th">plan_type</th></tr></thead>
              <tbody>{results.map((result, index) => {
                const meta = statusMeta(result.status);
                const errorMeta = result.error_type === "proxy_network"
                  ? { color: "warning", label: "代理/网络异常" }
                  : meta;
                return <tr key={`${result.id}-${index}`} className="tr-row">
                  <td className="td"><Badge color={errorMeta.color} dot>{errorMeta.label}</Badge>{result.error && <div className={`mt-1 max-w-[220px] text-[11px] ${result.error_type === "proxy_network" ? "text-amber-600" : "text-red-600"}`}>{result.error}</div>}</td>
                  <td className="td tnum whitespace-nowrap text-slate-600">{formatOAuthResultTime(result.completed_at)}</td>
                  <td className="td mono text-slate-700">{result.id || "—"}</td>
                  <td className="td text-slate-600">{result.email || "—"}</td>
                  <td className="td"><Badge color={result.has_access_token ? "success" : "neutral"}>{result.has_access_token ? "有" : "无"}</Badge></td>
                  <td className="td"><Badge color={result.has_refresh_token ? "success" : "neutral"}>{result.has_refresh_token ? "有" : "无"}</Badge></td>
                  <td className="td mono text-slate-600">{result.phone || "—"}</td>
                  <td className="td text-slate-600">{result.plan_type || "—"}</td>
                </tr>;
              })}</tbody>
            </table>
          </div>
        )}
      </Panel>

    </div>
  );
}
