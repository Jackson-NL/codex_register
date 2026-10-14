import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  UserPlus, Loader2, CheckCircle2, XCircle, Clock, ExternalLink, MonitorUp, Bug,
  Mail, RefreshCw, Ban, Copy, Check, Layers,
} from "lucide-react";
import {
  Panel, Button, Badge, Input, Switch, Modal, PageHeader, StageStepper,
  RiskBanner, Tabs, IconBtn, ProgressBar, SearchInput, Pagination,
} from "../components/ui";
import LiveLogBox from "../components/LiveLogBox";
import { api } from "../api";
import { useApp } from "../context/AppContext";
import { fmtTime } from "../mock/data";
import {
  REGISTER_LAYOUT, REGISTER_STAGES, buildRegisterPayload, filterRegisterRecords,
  advanceRegisterStage, formatRegDuration, formatRegistrationCopy, getRegistrationRecordSummary, isRunningStatus, isTaskMissingError,
  normalizeRegisterStatus, normalizeRegisterConfig, paginateRecords, parseRegisterResult, readStoredRegisterConfig, saveStoredRegisterConfig,
  pickDisplayRegister,
} from "./registerUtils";

const STATUS_META = {
  pending: ["info", "排队中"],
  running: ["info", "注册中"],
  success: ["success", "已完成"],
  failed: ["danger", "失败"],
  canceled: ["neutral", "已停止"],
  debug_waiting: ["warning", "调试暂停"],
  cooling: ["warning", "冷却中"],
};

const GMAIL_STATUS_META = {
  active: ["success", "活跃"],
  expired: ["neutral", "已过期"],
  waiting_code: ["warning", "等待验证码"],
  max_reached: ["danger", "次数已尽"],
  released: ["neutral", "已释放"],
};

// 从邮箱配置响应提取 iCloud 剩余轮数预测（非 icloud / 无数据时返回 null）
function readIcloudRounds(data) {
  const icloud = data?.icloud;
  if (!icloud) return null;
  return {
    remaining: Number(icloud.remaining_rounds) || 0,
    pool: Number(icloud.pool_size) || 0,
    exhausted: Number(icloud.exhausted_roots) || 0,
    split: !!icloud.split_enabled,
  };
}

// 失败原因归类与恢复建议
const FAIL_PATTERNS = [
  { re: /验证码|otp|code/i, label: "验证码" },
  { re: /风控|Cloudflare|挑战|challenge|turnstile/i, label: "风控拦截" },
  { re: /代理|proxy|不可达|连接/i, label: "代理失败" },
  { re: /浏览器|browser|Camoufox|页面/i, label: "浏览器异常" },
  { re: /session|access_token|网页登录状态|登录状态|会话/i, label: "会话提取" },
  { re: /2FA|totp|TOTP/i, label: "2FA 绑定" },
  { re: /邮箱|email|Gmail|gmail/i, label: "邮箱问题" },
  { re: /重试|超时|timeout/i, label: "超时重试" },
];
const RECOVERY = {
  "验证码": "检查 Gmail 订单剩余次数与 SMSBower 状态，等待验证码到达后重试；连续超时可租新 Gmail。",
  "风控拦截": "降低注册频率：延长冷却时间、更换代理出口，等待 45-90 秒降温后重试。",
  "代理失败": "到代理池一键测试连通性，更换可用代理后重试。",
  "浏览器异常": "检查浏览器 profile 是否被占用，确认 Camoufox 可正常启动后重试。",
  "会话提取": "检查网页登录 session/access_token 是否成功提取；注册成功后 refresh_token/id_token 由独立授权模块补齐。",
  "2FA 绑定": "确认 TOTP 绑定接口可用，或暂时关闭「绑定 2FA」后重试。",
  "邮箱问题": "检查 Gmail 订单状态，必要时释放当前会话并租新 Gmail。",
  "超时重试": "已自动重试仍失败，建议降低并发或更换代理后手动重试。",
  "其他失败": "查看下方实时日志定位具体错误后重试。",
};
function classifyError(err = "") {
  if (!err) return "未知原因";
  for (const p of FAIL_PATTERNS) {
    if (p.re.test(err)) return p.label;
  }
  return "其他失败";
}

function formatCountdown(totalSeconds) {
  const total = Math.max(0, Math.floor(totalSeconds || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  let language = "zh-CN";
  try { language = localStorage.getItem("accountops-language") || language; } catch { /* storage unavailable */ }
  if (language === "en-US") {
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
    return `${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
  }
  if (h > 0) return `${h}小时${String(m).padStart(2, "0")}分`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const POLL_INTERVAL = 2000;
const DEFAULT_PROXY = import.meta.env.VITE_DEFAULT_PROXY || "http://127.0.0.1:7890";

function StatusItem({ label, value, strong }) {
  const { t } = useApp();
  return (
    <span className="flex items-center gap-1.5 text-slate-500">
      <span className="text-slate-400">{t(label)}</span>
      <span className={`${strong ? "mono font-medium text-slate-700" : "text-slate-600"} max-w-[180px] truncate`}>{typeof value === "string" ? t(value) : value}</span>
    </span>
  );
}

// 明文凭据展示（邮箱/密码/TOTP）：明文 + 悬停复制
function PlainSecret({ label, value, className = "" }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* 剪贴板不可用 */ }
  };
  return (
    <div className={`flex items-center gap-1.5 ${className}`}>
      <span className="mono min-w-0 break-all text-[13px] text-slate-800">{value || "—"}</span>
      {value && (
        <button type="button" onClick={copy} title={`复制${label}`}
          className="shrink-0 rounded p-0.5 text-slate-300 opacity-0 transition-opacity hover:bg-slate-200/60 hover:text-slate-600 group-hover:opacity-100">
          {copied ? <Check size={12} className="text-emerald-600" /> : <Copy size={12} />}
        </button>
      )}
    </div>
  );
}

export default function Register() {
  const { toast, t } = useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const presetGmail = location.state?.filter?.gmail; // 从仪表盘 Gmail 会话卡跳转时预启用
  const [savedRegisterConfig] = useState(() => readStoredRegisterConfig());
  const [proxy, setProxy] = useState(() => savedRegisterConfig?.proxy ?? DEFAULT_PROXY);
  const [headless, setHeadless] = useState(() => savedRegisterConfig?.headless ?? true);
  const [debugMode, setDebugMode] = useState(() => savedRegisterConfig?.debugMode ?? false);
  const [debugTrace, setDebugTrace] = useState(() => savedRegisterConfig?.debugTrace ?? false);
  const [bind2FA, setBind2FA] = useState(() => savedRegisterConfig?.bind2FA ?? true);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [debugReleaseArmed, setDebugReleaseArmed] = useState(false);
  const [active, setActive] = useState(null); // 正在运行/最近关注的注册任务
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCreds, setShowCreds] = useState(false);
  const [credDrawer, setCredDrawer] = useState(null); // 历史记录查看凭据弹窗
  const [stageIdx, setStageIdx] = useState(-1); // 当前阶段（由日志推导）
  const stageRef = useRef(-1);
  // 批量注册
  const [batchActive, setBatchActive] = useState(null);
  const [batchTarget, setBatchTarget] = useState(() => savedRegisterConfig?.batchTarget ?? 10);
  const [batchConcurrency, setBatchConcurrency] = useState(() => savedRegisterConfig?.batchConcurrency ?? 2);
  // 模式切换：单次 / 批量
  const [mode, setMode] = useState(() => savedRegisterConfig?.mode ?? "single");
  // 当前关注的注册记录（点击记录时日志切换）
  const [focusedReg, setFocusedReg] = useState(null);
  // 注册记录：分页 / 筛选 / 搜索
  const [recPage, setRecPage] = useState(1);
  const [recPageSize, setRecPageSize] = useState(10);
  const [recStatus, setRecStatus] = useState("all");
  const [recQ, setRecQ] = useState("");
  const [copiedId, setCopiedId] = useState(null);
  const [updatedAt, setUpdatedAt] = useState(null);
  // Gmail 别名模式
  const [gmailEnabled, setGmailEnabled] = useState(() => savedRegisterConfig?.gmailEnabled ?? !!presetGmail);
  const [gmailSession, setGmailSession] = useState(null);
  const [gmailRenting, setGmailRenting] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [gmailNow, setGmailNow] = useState(Date.now());
  // 非 Gmail 的邮箱来源 Provider（cf_temp_email / icloud），从邮箱配置读取并支持就地切换
  const [mailProvider, setMailProvider] = useState("cf_temp_email");
  const [mailProviderLoading, setMailProviderLoading] = useState(true);
  // iCloud 剩余轮数预测：非分裂=未用池邮箱数；分裂=未耗尽基础邮箱数
  const [icloudRounds, setIcloudRounds] = useState(null);
  // 终态计数快照：变化才刷新剩余轮数（避免每 2s 轮询都拉全量配置）
  const mailRoundsKeyRef = useRef("");
  useEffect(() => {
    let mounted = true;
    api.mailConfig
      .get()
      .then((data) => {
        if (!mounted) return;
        setMailProvider(data?.provider || "cf_temp_email");
        setIcloudRounds(readIcloudRounds(data));
      })
      .catch(() => {})
      .finally(() => { if (mounted) setMailProviderLoading(false); });
    return () => { mounted = false; };
  }, []);
  const switchMailProvider = async (provider) => {
    if (running) return;
    try {
      const data = await api.mailConfig.save({ provider });
      setMailProvider(provider);
      setIcloudRounds(readIcloudRounds(data));
      setGmailEnabled(false);
      toast(`邮箱来源已切换为 ${provider === "icloud" ? "iCloud 邮箱池" : "Cloudflare 临时邮箱"}`, "success");
    } catch (error) {
      const firstLine = String(error?.message || "保存失败").split("\n")[0];
      toast(`${firstLine}（可在「邮箱配置」页配置后重试）`, "error");
    }
  };
  const timerRef = useRef(null);
  const batchTimerRef = useRef(null);
  const batchActiveIdRef = useRef(null);
  const gmailExpiredNoticeRef = useRef("");
  const missingNoticeRef = useRef({});

  useEffect(() => {
    saveStoredRegisterConfig(normalizeRegisterConfig({
      proxy,
      headless,
      debugMode,
      debugTrace,
      bind2FA,
      mode,
      batchTarget,
      batchConcurrency,
      gmailEnabled,
    }));
  }, [proxy, headless, bind2FA, debugMode, debugTrace, mode, batchTarget, batchConcurrency, gmailEnabled]);

  const changeDebugMode = (enabled) => {
    setDebugMode(enabled);
  };
  const changeDebugTrace = (enabled) => {
    setDebugTrace(enabled);
  };

  useEffect(() => {
    batchActiveIdRef.current = batchActive?.id || null;
  }, [batchActive?.id]);

  // 任务不存在提示只打印一次
  const noticeTaskMissing = (label) => {
    if (missingNoticeRef.current[label]) return;
    missingNoticeRef.current[label] = true;
    toast(`${label} 任务不存在，已清除旧状态`, "info");
  };

  // 加载历史 + 恢复进行中的任务（含批量任务）
  const loadHistory = useCallback(async () => {
    try {
      const list = await api.registrations.list({ limit: 50 });
      setHistory(list);
      setHistoryLoading(false);
      setUpdatedAt(Date.now());
      // 终态计数变化（有注册成功/失败）→ 刷新 iCloud 剩余轮数展示
      const terminalKey = list.filter((r) => r.status === "success" || r.status === "failed").length;
      if (terminalKey !== mailRoundsKeyRef.current) {
        mailRoundsKeyRef.current = terminalKey;
        api.mailConfig
          .icloudRounds()
          .then((data) => setIcloudRounds({
            remaining: Number(data.remaining_rounds) || 0,
            pool: Number(data.pool_size) || 0,
            exhausted: Number(data.exhausted_roots) || 0,
            split: !!data.split,
          }))
          .catch(() => {});
      }
      // 优先恢复当前关注的批次；批次可能尚未创建第一条 registration，不能只靠历史记录找回。
      const latestWithBatch = list.find((r) => r.batch_id) || null;
      const trackedBatchId = batchActiveIdRef.current;
      const recoverBatchId = trackedBatchId || latestWithBatch?.batch_id || null;
      if (recoverBatchId) {
        try {
          const b = await api.batches.get(recoverBatchId);
          const registrations = b.registrations || [];
          const orphanedTerminalBatch = trackedBatchId === b.id
            && b.status !== "running"
            && registrations.length === 0;
          if (orphanedTerminalBatch) {
            setBatchActive(null);
            setActive((prev) => (prev && isRunningStatus(prev.status) ? null : prev));
            setFocusedReg((prev) => (prev && isRunningStatus(prev.status) ? null : prev));
          } else {
            setBatchActive((prev) => {
              if (trackedBatchId === b.id || (!prev && b.status === "running")) {
                return { ...b, registrations };
              }
              return prev;
            });
          }
        } catch (e) {
          if (isTaskMissingError(e)) {
            setBatchActive((prev) => (prev?.id === recoverBatchId ? null : prev));
            setActive((prev) => (prev && isRunningStatus(prev.status) ? null : prev));
          }
        }
      }
      const running = list.find((r) => isRunningStatus(r.status));
      setActive((prev) => {
        if (running) return running;
        if (prev && isRunningStatus(prev.status)) return null;
        return prev;
      });
    } catch (e) {
      if (isTaskMissingError(e)) {
        setActive(null);
        setBatchActive(null);
        noticeTaskMissing("当前任务");
      } else {
        setError(e.message);
      }
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    loadHistory();
    const id = setInterval(loadHistory, 2000);
    return () => {
      clearInterval(timerRef.current);
      clearInterval(batchTimerRef.current);
      clearInterval(id);
    };
  }, [loadHistory]);

  // 轮询 active 任务状态（404/任务不存在 → 清理并停止轮询）
  useEffect(() => {
    if (!active || !isRunningStatus(active.status)) return undefined;
    timerRef.current = setInterval(async () => {
      try {
        const fresh = await api.registrations.get(active.id);
        setActive(fresh);
        if (!isRunningStatus(fresh.status)) {
          clearInterval(timerRef.current);
          setShowCreds(false);
          loadHistory();
        }
      } catch (e) {
        if (isTaskMissingError(e)) {
          clearInterval(timerRef.current);
          noticeTaskMissing(`reg_${active.id}`);
          setActive((prev) => (prev?.id === active.id ? null : prev));
          setFocusedReg((prev) => (prev?.id === active.id ? null : prev));
          loadHistory();
        }
        /* 网络抖动忽略，下轮重试 */
      }
    }, POLL_INTERVAL);
    return () => clearInterval(timerRef.current);
  }, [active?.id, active?.status, loadHistory]);

  // 轮询批量注册进度（404/任务不存在 → 清理）
  useEffect(() => {
    if (!batchActive || batchActive.status !== "running") return undefined;
    batchTimerRef.current = setInterval(async () => {
      try {
        const fresh = await api.batches.get(batchActive.id);
        setBatchActive(fresh);
        const runningReg = fresh.registrations?.find((r) => isRunningStatus(r.status));
        if (runningReg) setActive(runningReg);
        if (fresh.status !== "running") {
          clearInterval(batchTimerRef.current);
          if (!(fresh.registrations || []).length) setBatchActive(null);
          setActive((prev) => (prev && isRunningStatus(prev.status) ? null : prev));
          loadHistory();
        }
      } catch (e) {
        if (isTaskMissingError(e)) {
          clearInterval(batchTimerRef.current);
          noticeTaskMissing(`batch_${batchActive.id}`);
          setBatchActive(null);
          setActive((prev) => (prev && isRunningStatus(prev.status) ? null : prev));
          loadHistory();
        }
        /* 网络抖动忽略 */
      }
    }, 1000);
    return () => clearInterval(batchTimerRef.current);
  }, [batchActive?.id, batchActive?.status, loadHistory]);

  const startRegistration = async () => {
    if (!proxy.trim()) { toast("请填写代理地址", "warning"); return; }
    if (gmailEnabled && batchActive?.status === "running" && batchActive.gmail_mode) {
      toast("已有 Gmail 订单批量正在运行", "warning", { detail: `batch_${batchActive.id} 结束或停止后才能再次启动` });
      return;
    }
    setStarting(true);
    setError("");
    setShowCreds(false);
    setActive(null);
    setFocusedReg(null);
    stageRef.current = -1;
    setStageIdx(-1);
    try {
      if (gmailEnabled) {
        const batch = await api.batches.create({
          // Gmail 单次模式只完成当前一个主邮箱订单，内部仍按 alias/base/alias 串行。
          target: 1,
          concurrency: 1,
          proxy: proxy.trim(),
          headless: headless,
          debug_mode: debugMode,
          debug_trace: debugTrace,
          bind_totp: bind2FA,
          gmail_mode: true,
        });
        setBatchActive({ ...batch, registrations: [] });
        toast("Gmail 订单注册已启动", "success", { detail: "自动租/复用 Gmail · 跑完当前主邮箱订单" });
        loadHistory();
        return;
      }

      const body = buildRegisterPayload({ mode: "single", proxy, headless, bind2FA, debugMode, debugTrace });
      const res = await api.registrations.create(body);
      setActive(res);
      toast("注册任务已创建", "success", { detail: `reg_${res.id} · 约需 5-8 分钟` });
      loadHistory();
    } catch (e) {
      toast(`创建失败: ${e.message}`, "error");
    } finally {
      setStarting(false);
    }
  };

  const startBatch = async () => {
    if (!proxy.trim()) { toast("请填写代理地址", "warning"); return; }
    if (gmailEnabled && batchActive?.status === "running" && batchActive.gmail_mode) {
      toast("已有 Gmail 订单批量正在运行", "warning", { detail: `batch_${batchActive.id} 结束或停止后才能再次启动` });
      return;
    }
    setStarting(true);
    setActive(null);
    setFocusedReg(null);
    stageRef.current = -1;
    setStageIdx(-1);
    try {
      const batch = await api.batches.create(buildRegisterPayload({
        mode: "batch", proxy, headless, bind2FA, debugMode, debugTrace, target: batchTarget, concurrency: batchConcurrency,
        gmailMode: gmailEnabled,
      }));
      setBatchActive({ ...batch, registrations: [] });
      toast(gmailEnabled ? "Gmail 批量注册已启动" : "批量注册已启动", "success", {
        detail: gmailEnabled ? `Gmail 会话池 · 串行 · 目标 ${batchTarget} 个` : `目标 ${batchTarget} 个 · 并发 ${batchConcurrency}`,
      });
      loadHistory();
    } catch (e) {
      toast(`批量注册启动失败: ${e.message}`, "error");
    } finally {
      setStarting(false);
    }
  };

  const cancelBatch = async () => {
    if (!batchActive) return;
    setStopping(true);
    try {
      await api.batches.cancel(batchActive.id);
      const fresh = await api.batches.get(batchActive.id);
      setBatchActive(fresh);
      setActive((prev) => (prev && isRunningStatus(prev.status) ? { ...prev, status: "canceled" } : prev));
      stageRef.current = -1;
      setStageIdx(-1);
      toast("批量注册已停止", "info");
      loadHistory();
    } catch (e) {
      if (isTaskMissingError(e)) {
        setBatchActive(null);
        setActive((prev) => (prev && isRunningStatus(prev.status) ? { ...prev, status: "canceled" } : prev));
        stageRef.current = -1;
        setStageIdx(-1);
        noticeTaskMissing(`batch_${batchActive.id}`);
        loadHistory();
      } else {
        toast(`停止失败: ${e.message}`, "error");
      }
    } finally {
      setStopping(false);
    }
  };

  const cancelCurrentRegistration = async () => {
    const target = active && isRunningStatus(active.status) ? active : null;
    if (!target) return;
    setStopping(true);
    try {
      await api.registrations.cancel(target.id);
      const fresh = await api.registrations.get(target.id);
      setActive(fresh);
      stageRef.current = -1;
      setStageIdx(-1);
      toast(`reg_${target.id} 已停止`, "info");
      loadHistory();
    } catch (e) {
      if (isTaskMissingError(e)) {
        setActive((prev) => (prev?.id === target.id ? { ...prev, status: "canceled" } : prev));
        setFocusedReg((prev) => (prev?.id === target.id ? { ...prev, status: "canceled" } : prev));
        stageRef.current = -1;
        setStageIdx(-1);
        noticeTaskMissing(`reg_${target.id}`);
        loadHistory();
      } else {
        toast(`停止失败: ${e.message}`, "error");
      }
    } finally {
      setStopping(false);
    }
  };

  // Gmail 别名模式
  const rentGmail = async () => {
    if (batchActive?.status === "running" && batchActive.gmail_mode) {
      toast("Gmail 订单运行中，不能手动租新 Gmail", "warning");
      return;
    }
    setGmailRenting(true);
    try {
      const session = await api.gmailSessions.rent();
      setGmailSession(session);
      toast("Gmail 租用成功", "success", { detail: session.base_email });
    } catch (e) {
      toast(`租号失败: ${e.message}`, "error");
    } finally {
      setGmailRenting(false);
    }
  };

  const releaseGmail = async () => {
    if (!gmailSession) return;
    try {
      await api.gmailSessions.release(gmailSession.id);
      setGmailSession(null);
      setGmailAlias(null);
      toast("Gmail 已释放", "info");
    } catch (e) {
      toast(`释放失败: ${e.message}`, "error");
    }
  };

  const expireGmail = async () => {
    if (!gmailSession) return;
    try {
      const s = await api.gmailSessions.expire(gmailSession.id);
      setGmailSession(s);
      toast("会话已标记过期", "info");
    } catch (e) {
      toast(`操作失败: ${e.message}`, "error");
    }
  };

  const prepareNextCode = async () => {
    if (!gmailSession) return;
    if (batchActive?.status === "running" && batchActive.gmail_mode) {
      toast("Gmail 订单运行中，下一码由批量流程自动准备", "warning");
      return;
    }
    setPreparing(true);
    try {
      const s = await api.gmailSessions.prepareNextCode(gmailSession.id);
      setGmailSession(s);
      toast("已准备下一轮验证码", "success");
    } catch (e) {
      if (/Maximum number of codes reached/.test(e.message)) {
        setGmailSession((s) => ({ ...s, status: "expired", expired_reason: "达到最大验证码次数" }));
        toast("该订单已达到最大验证码次数，已标记过期", "warning");
      } else {
        toast(`准备失败: ${e.message}`, "error");
      }
    } finally {
      setPreparing(false);
    }
  };

  const loadActiveGmail = useCallback(async () => {
    if (!gmailEnabled) return;
    try {
      const s = await api.gmailSessions.active();
      setGmailSession((prev) => {
        if (s) return s;
        return prev;
      });
      if (!s) {
        setGmailSession((prev) => {
          if (prev?.status === "active") return { ...prev, status: "expired", expired_reason: prev.expired_reason || "订单状态同步中…" };
          return prev;
        });
        try {
          const sessions = await api.gmailSessions.list();
          setGmailSession((prev) => {
            if (!prev) return prev;
            const matched = sessions.find((item) => item.id === prev.id);
            return matched || prev;
          });
        } catch {
          // 历史回查失败时保留“同步中”，下一轮轮询继续修正。
        }
      }
    } catch {
      // 网络抖动忽略，下一轮继续同步
    }
  }, [gmailEnabled]);

  // 加载 Gmail 会话状态（页面加载只读）；开始注册时会按需自动租号。
  useEffect(() => {
    if (!gmailEnabled) return undefined;
    loadActiveGmail();
    const id = setInterval(loadActiveGmail, 10000);
    return () => clearInterval(id);
  }, [gmailEnabled, loadActiveGmail]);

  // Gmail 订单倒计时 tick；超时后触发一次热同步
  useEffect(() => {
    if (!gmailEnabled || !gmailSession?.expires_at || gmailSession.status !== "active") return undefined;
    const id = setInterval(() => setGmailNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [gmailEnabled, gmailSession?.id, gmailSession?.expires_at, gmailSession?.status]);

  useEffect(() => {
    gmailExpiredNoticeRef.current = "";
  }, [gmailSession?.id]);

  // 由日志推导当前阶段（单调递增）；每条日志内部优先使用显式 [stage:*] 标记。
  const handleLogs = useCallback((lines) => {
    const max = advanceRegisterStage(stageRef.current, lines);
    if (max !== stageRef.current) {
      stageRef.current = max;
      setStageIdx(max);
    }
  }, []);

  // ---------------- 派生数据 ----------------
  const historyRows = useMemo(() => history.map((r) => {
    const res = parseRegisterResult(r);
    return {
      ...r,
      email: res?.email || "",
      account_id: res?.account_id || r.account_id || "",
      totp: res?.totp_secret || "",
      duration: formatRegDuration(r.created_at, r.finished_at),
    };
  }), [history]);

  const batchIsRunning = batchActive?.status === "running";
  const displayReg = pickDisplayRegister({ focusedReg, batchActive, active, historyRows });
  const displayResult = parseRegisterResult(displayReg);
  const displayActive = !!displayReg && isRunningStatus(displayReg.status);

  const stopTargetReg = active && isRunningStatus(active.status) ? active : null;
  const canStop = (batchActive?.status === "running") || !!stopTargetReg;
  const running = canStop;
  const debugWaiting = stopTargetReg?.status === "debug_waiting"
    || !!batchActive?.registrations?.some((r) => r.status === "debug_waiting");

  useEffect(() => {
    if (!debugWaiting) setDebugReleaseArmed(false);
  }, [debugWaiting]);

  // 调试抓包：有头调试时轮询截图/HAR（供我/你实时监督）
  const debugRegId = displayReg?.id && (displayReg?.debug_trace || displayReg?.debug_mode) ? displayReg.id : (batchActive?.debug_trace ? (displayReg?.id || null) : null);
  const debugActive = !!(debugRegId && (displayReg?.debug_trace || batchActive?.debug_trace) && (displayActive || batchIsRunning));
  const [debugHar, setDebugHar] = useState([]);
  const [debugImgTick, setDebugImgTick] = useState(0);
  useEffect(() => {
    if (!debugActive || !debugRegId) return undefined;
    let alive = true;
    const tick = async () => {
      try {
        const data = await api.registrations.debugHar(debugRegId, { limit: 100 });
        if (alive && Array.isArray(data?.items)) setDebugHar(data.items.slice(-80));
      } catch { /* best-effort */ }
      if (alive) setDebugImgTick((v) => v + 1);
    };
    tick();
    const id = setInterval(tick, 3000);
    return () => { alive = false; clearInterval(id); };
  }, [debugActive, debugRegId]);

  // 实时日志框展示的任务：批量进行中时优先显示批量里的当前任务，否则显示单次任务
  const logRegId = displayReg?.id || null;
  const logBatchId = batchActive?.id || null;
  const logActive = displayActive || batchIsRunning;

  // 状态归一化：批量 > 单次 > 终态 > 空闲
  const statusState = normalizeRegisterStatus(stopTargetReg, batchActive);

  // 失败归类与建议
  const failReason = displayReg?.status === "failed" ? classifyError(displayReg.error) : "";
  const failSuggestion = RECOVERY[failReason] || RECOVERY["其他失败"];
  const gmailMeta = GMAIL_STATUS_META[gmailSession?.status] || GMAIL_STATUS_META.active;
  const gmailRemaining = gmailSession ? Math.max(0, (gmailSession.max_aliases ?? 3) - (gmailSession.alias_counter ?? 0)) : 0;
  const gmailExpiresAt = gmailSession?.expires_at ? new Date(gmailSession.expires_at).getTime() : 0;
  const gmailSecondsLeft = gmailExpiresAt ? Math.max(0, Math.ceil((gmailExpiresAt - gmailNow) / 1000)) : 0;
  const gmailBatchRunning = gmailEnabled && batchActive?.status === "running" && !!batchActive.gmail_mode;

  // 顶部状态条计数（最近 50 条）
  const countSuccess = historyRows.filter((r) => r.status === "success").length;
  const countFailed = historyRows.filter((r) => r.status === "failed").length;
  const countRunning = historyRows.filter((r) => isRunningStatus(r.status)).length;

  // 批量进度
  const batchStarted = (batchActive?.succeeded || 0) + (batchActive?.failed || 0)
    + (batchActive?.registrations?.filter((r) => isRunningStatus(r.status)).length || 0);
  const batchProgress = batchActive?.gmail_mode
    ? (batchActive.gmail_orders_completed || 0)
    : batchStarted;
  const batchPct = batchActive?.target ? Math.min(100, (batchProgress / batchActive.target) * 100) : 0;
  const batchRunningRegs = batchActive?.registrations?.filter((r) => isRunningStatus(r.status)) || [];

  // 注册记录：筛选 + 分页
  const filteredRecords = useMemo(
    () => filterRegisterRecords(historyRows, { status: recStatus, q: recQ }),
    [historyRows, recStatus, recQ],
  );
  const recPageData = useMemo(
    () => paginateRecords(filteredRecords, recPage, recPageSize),
    [filteredRecords, recPage, recPageSize],
  );
  useEffect(() => setRecPage(1), [recStatus, recQ, history.length]);

  useEffect(() => {
    stageRef.current = -1;
    setStageIdx(-1);
  }, [displayReg?.id, displayReg?.status]);

  useEffect(() => {
    if (!gmailSession || gmailSession.status !== "active" || !gmailExpiresAt) return;
    if (gmailExpiresAt > gmailNow) return;
    if (gmailExpiredNoticeRef.current === String(gmailSession.id)) return;
    gmailExpiredNoticeRef.current = String(gmailSession.id);
    toast("Gmail 订单已超时，正在同步会话状态", "warning");
    loadActiveGmail();
  }, [gmailSession?.id, gmailSession?.status, gmailExpiresAt, gmailNow, loadActiveGmail, toast]);

  const focusReg = (r) => {
    setActive(r);
    setFocusedReg(r);
    setShowCreds(false);
    setRecPage(1);
  };

  const copyRecordResult = async (r) => {
    const text = formatRegistrationCopy(r);
    try {
      await navigator.clipboard.writeText(text);
      setCopiedId(r.id);
      setTimeout(() => setCopiedId(null), 1500);
    } catch {
      toast("复制失败，浏览器拒绝了剪贴板", "error");
    }
  };

  const onStopClick = () => {
    if (debugWaiting) {
      if (!debugReleaseArmed) {
        setDebugReleaseArmed(true);
        toast("浏览器仍在调试暂停；再次点击“确认关闭浏览器”才会结束调试", "warning");
        return;
      }
      releaseDebug();
    }
    else if (batchActive?.status === "running") cancelBatch();
    else cancelCurrentRegistration();
  };

  const releaseDebug = async () => {
    const targets = batchActive?.registrations?.filter((r) => r.status === "debug_waiting") || [];
    if (!targets.length && stopTargetReg?.status === "debug_waiting") targets.push(stopTargetReg);
    if (!targets.length) return;
    setStopping(true);
    try {
      await Promise.all(targets.map((target) => api.registrations.releaseDebug(target.id)));
      setDebugReleaseArmed(false);
      toast(targets.length > 1 ? "已结束调试，正在关闭浏览器" : `reg_${targets[0].id} 已结束调试，正在关闭浏览器`, "info");
      loadHistory();
    } catch (e) {
      if (!isTaskMissingError(e)) toast(`结束调试失败: ${e.message}`, "error");
    } finally {
      setStopping(false);
    }
  };

  const gmailActionsDisabled = running || gmailBatchRunning;

  const registrationRecordsPanel = (
    <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
      <div className="border-b border-slate-100 px-3 py-2">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-medium text-slate-700">注册记录</span>
          <span className="text-xs text-slate-400">共 {history.length} 次</span>
        </div>
        <div className="mt-2 flex gap-2">
          <select
            className="input h-7 min-w-0 flex-1 px-2 py-0 text-xs"
            value={recStatus}
            onChange={(e) => setRecStatus(e.target.value)}
          >
            <option value="all">全部</option>
            <option value="running">运行中</option>
            <option value="debug_waiting">调试暂停</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
            <option value="canceled">已停止</option>
          </select>
          <SearchInput value={recQ} onChange={setRecQ} placeholder="搜索任务 / 邮箱 / 账号" className="min-w-0 flex-[2]" />
        </div>
      </div>
      <div className="max-h-[520px] overflow-y-auto">
        {historyLoading ? (
          <div className="space-y-2 p-3">
            {Array.from({ length: 5 }, (_, index) => <div key={index} className="h-12 animate-pulse rounded bg-slate-100" />)}
          </div>
        ) : recPageData.items.length === 0 ? (
          <div className="px-3 py-8 text-center text-xs text-slate-400">
            <div>还没有注册记录</div>
            <div className="mt-1 text-[11px] text-slate-300">点击上方按钮发起浏览器自动注册</div>
          </div>
        ) : (
          <div className="divide-y divide-slate-100">
            {recPageData.items.map((r) => {
              const [statusColor, statusLabel] = STATUS_META[r.status] || ["neutral", r.status || "未知"];
              return (
                <div key={r.id} className="cursor-pointer px-3 py-2.5 transition-colors hover:bg-blue-50/50" onClick={() => focusReg(r)}>
                  <div className="flex items-center gap-2">
                    <span className="mono text-xs font-medium text-slate-700">reg_{r.id}</span>
                    <Badge color={statusColor} dot>{statusLabel}</Badge>
                    <span className="ml-auto text-[11px] text-slate-400">{fmtTime(r.finished_at || r.created_at)}</span>
                  </div>
                  <div className="mt-1 flex min-w-0 items-center gap-2">
                    <span className={`min-w-0 flex-1 truncate text-xs ${r.status === "failed" ? "text-red-600" : "text-slate-600"}`} title={getRegistrationRecordSummary(r)}>
                      {getRegistrationRecordSummary(r)}
                    </span>
                    <div className="flex shrink-0 items-center gap-1" onClick={(e) => e.stopPropagation()}>
                      <Button variant="ghost" size="sm" onClick={() => focusReg(r)}>日志</Button>
                      <Button variant="ghost" size="sm" icon={copiedId === r.id ? <Check size={12} className="text-emerald-600" /> : <Copy size={12} />}
                        onClick={() => copyRecordResult(r)}>
                        {copiedId === r.id ? "已复制" : "复制"}
                      </Button>
                      {r.status === "success" && <Button variant="ghost" size="sm" onClick={() => setCredDrawer(r)}>凭据</Button>}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <Pagination
        page={recPageData.page}
        pages={recPageData.pages}
        total={recPageData.total}
        pageSize={recPageData.pageSize}
        onPage={setRecPage}
        onPageSize={(size) => { setRecPageSize(size); setRecPage(1); }}
      />
    </div>
  );

  return (
    <div className="space-y-2.5">
      <PageHeader
        title="注册工作台"
        subtitle="浏览器自动注册 / 邮箱 / 验证码 / 2FA / 保存账号"
        badge={<Badge color={statusState.color} dot>{statusState.label}</Badge>}
        extra={
          <>
            <span className="text-xs text-slate-400">{updatedAt ? `更新于 ${fmtTime(updatedAt)}` : "同步中…"}</span>
            <IconBtn icon={<RefreshCw size={14} />} title="刷新状态" onClick={() => { setError(""); loadHistory(); }} />
            {canStop && (
              <Button
                variant="danger"
                size="sm"
                icon={stopping ? <Loader2 size={13} className="animate-spin" /> : <Ban size={13} />}
                onClick={onStopClick}
                loading={stopping}
                title={debugWaiting ? (debugReleaseArmed ? "确认结束调试并关闭浏览器" : "先确认，再结束调试") : batchActive?.status === "running" ? `停止批量 batch_${batchActive.id}` : `停止注册 reg_${stopTargetReg?.id}`}
              >
                {stopping ? (debugWaiting ? "正在结束调试…" : "正在停止…") : debugWaiting ? (debugReleaseArmed ? "确认关闭浏览器" : "结束调试") : "停止"}
              </Button>
            )}
          </>
        }
      />

      {error && (
        <div className="flex items-center gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[13px] text-red-700">
          <span>数据加载失败：{error}</span>
          <Button variant="ghost" size="sm" className="ml-auto" onClick={loadHistory}>重试</Button>
        </div>
      )}

      {/* 顶部状态条 */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5 rounded-md border border-slate-200 bg-white px-3 py-2 text-xs">
        <StatusItem label="当前任务" value={statusState.taskLabel || "无"} strong />
        <StatusItem label="模式" value={mode === "batch" || batchActive?.status === "running" ? "批量" : "单次"} />
        <StatusItem label="代理" value={proxy || "未设置"} />
        <StatusItem label="邮箱来源" value={gmailEnabled ? "Gmail 会话池" : mailProvider === "icloud" ? "iCloud 邮箱池" : "CF 临时邮箱"} />
        <StatusItem label="headless" value={headless ? "是" : "否"} />
        <StatusItem label="bind_2fa" value={bind2FA ? "是" : "否"} />
        <div className="ml-auto flex items-center gap-2">
          <Badge color="success" dot>{t(`成功 ${countSuccess}`)}</Badge>
          <Badge color="danger" dot>{t(`失败 ${countFailed}`)}</Badge>
          {statusState.active && <Badge color="info" dot>{t(`运行中 ${Math.max(countRunning, 1)}`)}</Badge>}
        </div>
      </div>

      {/* 主体两列：左控制 / 右监控 */}
      <div className={`grid grid-cols-1 gap-3 ${REGISTER_LAYOUT.leftColClass}`}>
        {/* ==================== 左侧：参数与操作区 ==================== */}
        <div className="min-w-0 space-y-3">
          <Panel title="控制面板" pad={false}
            extra={batchActive?.status === "running" ? <Badge color="info" dot>batch_{batchActive.id}</Badge> : null}>
            <Tabs
              size="sm"
              tabs={[
                { key: "single", label: "单次注册" },
                { key: "batch", label: "批量注册" },
              ]}
              active={mode}
              onChange={running ? undefined : setMode}
            />
            <div className="space-y-3 p-3">
              {mode === "single" ? (
                <>
                  <Input label="代理地址" value={proxy} onChange={(e) => setProxy(e.target.value)} placeholder={DEFAULT_PROXY} disabled={running} />
                  <div className="flex flex-wrap items-center gap-3">
                    <Switch checked={headless} onChange={setHeadless} label="无头模式" disabled={running} />
                    <Switch checked={debugMode} onChange={changeDebugMode} label="调试模式" disabled={running} />
                    <Switch checked={debugTrace} onChange={changeDebugTrace} label="抓包调试（截图+HAR）" disabled={running} />
                    <Switch checked={bind2FA} onChange={setBind2FA} label="绑定 2FA" disabled={running} />
                  </div>
                  {debugMode && <div className="flex items-start gap-1.5 rounded bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700"><Bug size={13} className="mt-0.5 shrink-0" />流程失败时保留浏览器（需有头才可见），点击“结束调试”后才会关闭。</div>}
                  {debugTrace && <div className="flex items-start gap-1.5 rounded bg-blue-50 px-2.5 py-2 text-[11px] text-blue-700">抓包调试：无头/有头均可，自动截图+HAR+Trace，完成后可下载 trace.zip 用 trace.playwright.dev 回放。</div>}
                  <Button
                    size="lg"
                    className="w-full"
                    icon={(starting || gmailRenting) ? <Loader2 size={15} className="animate-spin" /> : <UserPlus size={15} />}
                    onClick={startRegistration}
                    disabled={starting || gmailRenting || gmailBatchRunning}
                  >
                    {gmailBatchRunning ? `Gmail 订单运行中（batch_${batchActive.id}）` : starting ? "启动中…" : gmailRenting ? "租号中…" : gmailEnabled ? "跑完当前 Gmail 订单" : "开始注册"}
                  </Button>
                </>
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <Input label={gmailEnabled ? "主邮箱目标数量" : "目标数量"} type="number" min={1} max={100} value={batchTarget}
                      onChange={(e) => setBatchTarget(Math.max(1, parseInt(e.target.value) || 1))} disabled={running} />
                    <Input label="并发数量" type="number" min={1} max={5} value={gmailEnabled ? 1 : batchConcurrency}
                      onChange={(e) => setBatchConcurrency(Math.min(5, Math.max(1, parseInt(e.target.value) || 1)))}
                      disabled={running || gmailEnabled}
                      hint={gmailEnabled ? "Gmail 模式强制串行" : undefined} />
                  </div>
                  <Input label="代理地址" value={proxy} onChange={(e) => setProxy(e.target.value)} placeholder={DEFAULT_PROXY} disabled={running} />
                  <div className="flex flex-wrap items-center gap-3">
                    <Switch checked={headless} onChange={setHeadless} label="无头模式" disabled={running} />
                    <Switch checked={debugMode} onChange={changeDebugMode} label="调试模式" disabled={running} />
                    <Switch checked={debugTrace} onChange={changeDebugTrace} label="抓包调试" disabled={running} />
                    <Switch checked={bind2FA} onChange={setBind2FA} label="绑定 2FA" disabled={running} />
                  </div>
                  {debugMode && <div className="flex items-start gap-1.5 rounded bg-amber-50 px-2.5 py-2 text-[11px] text-amber-700"><Bug size={13} className="mt-0.5 shrink-0" />流程失败时保留浏览器，点击“结束调试”后才会关闭。</div>}
                  {debugTrace && <div className="flex items-start gap-1.5 rounded bg-blue-50 px-2.5 py-2 text-[11px] text-blue-700">抓包调试：已开启截图/HAR/Trace，无头有头均可。</div>}
                  <Button className="w-full" size="lg" icon={starting ? <Loader2 size={15} className="animate-spin" /> : <Layers size={15} />}
                    onClick={startBatch} disabled={starting || gmailBatchRunning}>
                    {starting ? "启动中…" : gmailBatchRunning ? `Gmail 订单运行中（batch_${batchActive.id}）` : gmailEnabled ? `启动 Gmail 批量（目标 ${batchTarget}）` : "启动批量注册"}
                  </Button>
                </>
              )}
            </div>

            {/* 邮箱来源：Gmail 会话池 / CF 临时邮箱 / iCloud 邮箱池 */}
            <div className="border-t border-slate-100 px-3 py-2.5">
              <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
                <Mail size={13} />邮箱来源
              </div>
              <div className="mt-2 grid grid-cols-3 gap-1.5">
                {[
                  { key: "gmail", label: "Gmail 会话池", active: gmailEnabled, onClick: () => setGmailEnabled(true) },
                  { key: "cf", label: "CF 临时邮箱", active: !gmailEnabled && mailProvider !== "icloud", onClick: () => switchMailProvider("cf_temp_email") },
                  { key: "icloud", label: "iCloud 邮箱池", active: !gmailEnabled && mailProvider === "icloud", onClick: () => switchMailProvider("icloud") },
                ].map((item) => (
                  <button
                    key={item.key}
                    type="button"
                    disabled={running || mailProviderLoading}
                    onClick={item.onClick}
                    className={`rounded-md border px-2 py-1.5 text-[12px] font-medium transition-colors ${
                      item.active
                        ? "border-blue-500 bg-blue-50/60 text-blue-700"
                        : "border-slate-200 bg-white text-slate-500 hover:border-slate-300 disabled:opacity-50"
                    }`}
                    title={item.key === "icloud" && !item.active ? "切到 iCloud 邮箱池（需在邮箱配置页启用并导入邮箱池）" : undefined}
                  >
                    {item.label}{item.active && <span className="ml-1">✓</span>}
                  </button>
                ))}
              </div>
              {!gmailEnabled && mailProvider === "icloud" && (
                <div className="mt-2 flex items-center justify-between rounded-md bg-slate-50 px-2.5 py-2 text-xs">
                  <span className="text-slate-500">
                    剩余轮数
                    <span className="ml-1 text-[10px] text-slate-400">
                      {icloudRounds?.split ? "别名模式 · 按基础邮箱计" : "原邮箱模式 · 按池邮箱计"}
                    </span>
                  </span>
                  <span className="tnum text-[13px] font-semibold">
                    <span className={(icloudRounds?.remaining ?? 0) > 0 ? "text-emerald-600" : "text-red-500"}>
                      {icloudRounds?.remaining ?? "—"}
                    </span>
                    {icloudRounds != null && (
                      <span className="ml-1 text-[10px] font-normal text-slate-400">
                        / {icloudRounds.pool}
                        {icloudRounds.exhausted > 0 && ` · 耗尽 ${icloudRounds.exhausted}`}
                      </span>
                    )}
                  </span>
                </div>
              )}
              {gmailEnabled && (
                <div className="mt-2 space-y-2">
                  {!gmailSession ? (
                    <div className="flex items-center justify-between rounded-md bg-slate-50 px-2.5 py-2 text-xs text-slate-400">
                      <span>暂无活跃 Gmail 会话</span>
                      <Button size="sm" icon={gmailRenting ? <Loader2 size={12} className="animate-spin" /> : <Mail size={12} />}
                        onClick={rentGmail} disabled={gmailRenting || gmailActionsDisabled}>
                        {gmailRenting ? "租号中…" : "租用 Gmail"}
                      </Button>
                    </div>
                  ) : (
                    <>
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <span className="mono truncate text-xs text-slate-700" title={gmailSession.base_email}>{gmailSession.base_email}</span>
                          <Badge color={gmailMeta[0]} dot>{gmailMeta[1]}</Badge>
                        </div>
                        <IconBtn icon={<RefreshCw size={12} />} title="手动刷新 session" onClick={loadActiveGmail} />
                      </div>
                      <div className="grid grid-cols-3 gap-1.5 text-center">
                        <div className="rounded bg-slate-50 py-1.5">
                          <div className="tnum text-[13px] font-semibold text-slate-700">{gmailSession.alias_counter ?? 0}<span className="text-[10px] text-slate-400">/{gmailSession.max_aliases ?? 3}</span></div>
                          <div className="text-[10px] text-slate-400">已用别名</div>
                        </div>
                        <div className="rounded bg-slate-50 py-1.5">
                          <div className={`tnum text-[13px] font-semibold ${gmailRemaining > 0 ? "text-emerald-600" : "text-red-500"}`}>{gmailRemaining}</div>
                          <div className="text-[10px] text-slate-400">剩余次数</div>
                        </div>
                        <div className="rounded bg-slate-50 py-1.5">
                          <div className={`tnum text-[13px] font-semibold ${gmailSecondsLeft > 0 && gmailSession.status === "active" ? "text-slate-700" : "text-slate-400"}`}>
                            {gmailSession.status === "active" ? (gmailExpiresAt ? (gmailSecondsLeft > 0 ? formatCountdown(gmailSecondsLeft) : "超时") : "—") : "—"}
                          </div>
                          <div className="text-[10px] text-slate-400">倒计时</div>
                        </div>
                      </div>
                      {gmailSession.expired_reason && (
                        <div className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700">过期原因：{gmailSession.expired_reason}</div>
                      )}
                      <div className="flex flex-wrap gap-1.5">
                        {gmailSession.status === "active" ? (
                          <>
                            <Button size="sm" variant="secondary" icon={preparing ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                              onClick={prepareNextCode} disabled={preparing || gmailActionsDisabled}>准备下一码</Button>
                            <Button size="sm" variant="ghost" className="text-red-500" icon={<Ban size={12} />} onClick={expireGmail} disabled={gmailActionsDisabled}>过期</Button>
                            <Button size="sm" variant="ghost" className="text-red-500" icon={<XCircle size={12} />} onClick={releaseGmail} disabled={gmailActionsDisabled}>释放</Button>
                          </>
                        ) : (
                          <Button size="sm" icon={<Mail size={12} />} onClick={() => { setGmailSession(null); rentGmail(); }} disabled={gmailActionsDisabled}>租新 Gmail</Button>
                        )}
                      </div>
                    </>
                  )}
                </div>
              )}
            </div>
          </Panel>

          {/* 批量进度（运行时） */}
          {batchActive?.status === "running" && (
            <Panel title="批量进度" pad={false} extra={<Badge color="info" dot>batch_{batchActive.id}</Badge>}>
              <div className="space-y-2 p-3">
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>{batchActive.gmail_mode ? "主邮箱进度：" : "进度："}<b className="tnum text-slate-700">{batchProgress}</b> / {batchActive.target}</span>
                  <span className="tnum">{t(`成功 ${batchActive.succeeded} · 失败 ${batchActive.failed} · 运行中 ${batchRunningRegs.length}`)}</span>
                </div>
                <ProgressBar value={batchPct} color="bg-emerald-500" height="h-1.5" />
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] text-slate-400">{batchActive.gmail_mode ? "Gmail 会话池模式" : "普通模式"} · 并发 {batchActive.concurrency}</span>
                  <Button variant="danger" size="sm" icon={stopping ? <Loader2 size={13} className="animate-spin" /> : <Ban size={13} />}
                    onClick={cancelBatch} disabled={stopping}>
                    {stopping ? "正在停止…" : "停止"}
                  </Button>
                </div>
                {batchRunningRegs.length > 0 && (
                  <div className="max-h-28 space-y-1 overflow-y-auto">
                    {batchRunningRegs.map((r) => (
                      <button key={r.id} type="button"
                        className="flex w-full items-center gap-2 rounded bg-slate-50 px-2 py-1 text-xs hover:bg-blue-50"
                        onClick={() => focusReg(r)}>
                        <span className="mono text-slate-600">reg_{r.id}</span>
                        <Badge color={r.status === "running" ? "info" : "neutral"} dot>{r.status === "running" ? "注册中" : "排队中"}</Badge>
                        <span className="ml-auto text-[11px] text-slate-400">{fmtTime(r.created_at)}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </Panel>
          )}
          {registrationRecordsPanel}
        </div>

        {/* ==================== 右侧：阶段 + 日志 + 记录 ==================== */}
        <div className="min-w-0 space-y-3">
          {/* 注册流程阶段 */}
          <Panel title="注册流程阶段" pad={false}>
            {statusState.active && displayReg ? (
              <div className="overflow-x-auto p-3">
                <StageStepper stages={REGISTER_STAGES} current={stageIdx >= 0 ? stageIdx : 0} status="running" />
                <div className="mt-2.5 flex items-center gap-3 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500">
                  <span className="mono text-[13px] font-medium text-slate-700">{displayReg ? `reg_${displayReg.id}` : "reg_—"}</span>
                  <span>{displayReg?.status === "pending" ? t("排队中…") : t(`当前阶段：${REGISTER_STAGES[Math.max(0, stageIdx)]?.label || "准备中"}`)}</span>
                  <span className="ml-auto flex items-center gap-1"><Clock size={12} />{fmtTime(displayReg?.created_at)}</span>
                </div>
              </div>
            ) : statusState.active ? (
              <div className="flex items-center gap-3 px-3 py-3 text-xs text-slate-500">
                <Loader2 size={14} className="animate-spin text-blue-500" />
                <span className="mono text-[13px] font-medium text-slate-700">batch_{batchActive?.id || "—"}</span>
                <span>正在同步当前注册任务，浏览器尚未开始或任务列表暂未返回</span>
                <span className="ml-auto text-slate-400">批量进度实时刷新中</span>
              </div>
            ) : batchActive && batchActive.status !== "running" ? (
              /* 批量终态摘要：不再高亮运行态 */
              <div className="flex items-center gap-3 px-3 py-3 text-xs">
                <Badge color={batchActive.status === "completed" ? "success" : "neutral"} dot>
                  {batchActive.status === "completed" ? "批量已完成" : "批量已停止"}
                </Badge>
                <span className="text-slate-600">
                  {batchActive.gmail_mode ? "主邮箱完成" : "成功"} <b className="tnum">{batchProgress}</b> / {batchActive.target} · 注册成功 <b className="tnum">{batchActive.succeeded}</b> · 失败 <b className="tnum">{batchActive.failed}</b>
                </span>
                <span className="ml-auto text-slate-400">{fmtTime(batchActive.finished_at || batchActive.created_at)}</span>
                <Button variant="ghost" size="sm" onClick={() => setBatchActive(null)}>关闭</Button>
              </div>
            ) : displayReg?.status === "success" ? (
              <div className="p-3">
                <StageStepper stages={REGISTER_STAGES} current={REGISTER_STAGES.length} status="success" />
                <div className="mt-2.5 flex items-center gap-3 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                  <CheckCircle2 size={14} />
                  <span className="mono text-[13px] font-medium">reg_{displayReg.id}</span>
                  <span>注册完成，已写入账号管理</span>
                  <span className="ml-auto flex items-center gap-1"><Clock size={12} />{fmtTime(displayReg.finished_at || displayReg.created_at)}</span>
                </div>
              </div>
            ) : displayReg?.status === "failed" ? (
              <div className="p-3">
                <StageStepper stages={REGISTER_STAGES} current={stageIdx >= 0 ? stageIdx : 0} status="failed" />
                <div className="mt-2.5 flex items-center gap-3 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">
                  <XCircle size={14} />
                  <span className="mono text-[13px] font-medium">reg_{displayReg.id}</span>
                  <span>已失败，停留阶段：{stageIdx >= 0 ? REGISTER_STAGES[stageIdx]?.label : "未识别"}</span>
                  <span className="ml-auto flex items-center gap-1"><Clock size={12} />{fmtTime(displayReg.finished_at || displayReg.created_at)}</span>
                </div>
              </div>
            ) : displayReg?.status === "canceled" ? (
              <div className="flex items-center gap-3 px-3 py-3 text-xs">
                <Badge color="neutral" dot>已停止</Badge>
                <span className="mono text-slate-700">reg_{displayReg.id}</span>
                <span className="text-slate-400">已停止，不再显示运行中阶段</span>
                <span className="ml-auto flex items-center gap-1 text-slate-400"><Clock size={12} />{fmtTime(displayReg.finished_at || displayReg.created_at)}</span>
              </div>
            ) : (
              /* 空闲态：紧凑，不占大块区域 */
              <div className="flex items-center gap-2 px-3 py-3 text-xs text-slate-400">
                <MonitorUp size={13} />
                <span>当前无任务运行</span>
                <span className="text-slate-300">· 从左侧「控制面板」发起注册后，这里会展示 11 个阶段的实时进度</span>
              </div>
            )}
          </Panel>

          {/* 失败归类 + 建议 */}
          {displayReg?.status === "failed" && (
            <RiskBanner level="danger" title={t(`注册失败（${t(failReason)}）`)}
              actionText="换新邮箱重试" onAction={startRegistration}>
              <div className="mono break-all">{displayReg.error}</div>
              <div className="mt-1">{t("恢复建议：")}{t(failSuggestion)}</div>
            </RiskBanner>
          )}

          {/* 成功结果（邮箱/密码/TOTP 明文展示） */}
          {displayReg?.status === "success" && displayResult && (
            <div className="overflow-hidden rounded-md border border-emerald-200 bg-emerald-50/50">
              <div className="flex items-center gap-2 border-b border-emerald-100 px-3 py-2">
                <CheckCircle2 size={15} className="text-emerald-600" />
                <span className="mono text-[13px] font-medium text-slate-700">reg_{displayReg.id}</span>
                <Badge color="success" dot>注册成功</Badge>
                <span className="ml-auto text-xs text-slate-400">{fmtTime(displayReg.created_at)}</span>
              </div>
              <div className="space-y-2.5 px-3 py-2.5">
                <div className="group flex items-center gap-2">
                  <span className="w-16 shrink-0 text-[11px] text-slate-400">邮箱</span>
                  <PlainSecret label="邮箱" value={displayResult.email} />
                  {displayReg.account_id && (
                    <Button variant="ghost" size="sm" className="ml-auto text-xs" onClick={() => navigate("/accounts")}>
                      前往账号管理 →
                    </Button>
                  )}
                </div>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                  <div className="group rounded bg-white/80 px-2 py-1.5">
                    <div className="text-[11px] text-slate-400">密码</div>
                    <PlainSecret label="密码" value={displayResult.temp_email_password || ""} className="mt-0.5" />
                  </div>
                  <div className="group rounded bg-white/80 px-2 py-1.5">
                    <div className="text-[11px] text-slate-400">TOTP Secret</div>
                    <PlainSecret label="TOTP Secret" value={displayResult.totp_secret || ""} className="mt-0.5" />
                    {displayResult.totp_secret && (
                      <a href={`https://2fa.show/2fa/${displayResult.totp_secret}`} target="_blank" rel="noreferrer"
                        className="mt-1 inline-flex items-center gap-1 text-[11px] text-blue-600 hover:underline">
                        在线生成动态码 <ExternalLink size={10} />
                      </a>
                    )}
                  </div>
                  <div className="rounded bg-white/80 px-2 py-1.5">
                    <div className="text-[11px] text-slate-400">账号标识 / 计划</div>
                    <div className="mono text-xs text-slate-700">{displayResult.account_id || displayResult.user_id || "—"}</div>
                    <div className="mt-1 text-[11px] text-slate-400">计划：{displayResult.plan_type || "free"}</div>
                  </div>
                </div>
                <div className="rounded-md bg-amber-50/70 px-2 py-1 text-[11px] text-amber-700">
                  凭据已明文展示，悬停可复制；请勿在共享屏幕时停留本页。
                </div>
              </div>
            </div>
          )}

          {/* 实时日志：监控核心，首屏优先 */}
          <LiveLogBox
            regId={logRegId}
            batchId={logBatchId}
            active={logActive}
            height={REGISTER_LAYOUT.logMinHeight}
            onLogs={handleLogs}
            title={batchIsRunning && !logRegId
              ? `实时日志 · batch_${logBatchId}`
              : logActive
                ? `实时日志 · reg_${logRegId}`
                : `执行日志 · ${logRegId ? `reg_${logRegId}` : logBatchId ? `batch_${logBatchId}` : "-"}`}
          />
          {/* 调试抓包面板：仅抓包调试时显示，供我/你实时监督 */}
          {debugActive && debugRegId && (
            <Panel title={`调试抓包 · reg_${debugRegId}`} extra={<span className="text-[11px] text-slate-400">2s截图 · 3s HAR · 脱敏后</span>} pad={false}>
              <div className="grid gap-3 p-3 lg:grid-cols-[1.2fr_0.8fr]">
                <div className="overflow-hidden rounded border border-slate-200 bg-slate-950">
                  <div className="flex items-center justify-between border-b border-slate-800 px-2 py-1 text-[11px] text-slate-400">
                    <span>实时截图（有头浏览器）</span>
                    <a href={api.registrations.debugScreenshotUrl(debugRegId)} target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">新窗打开</a>
                  </div>
                  <img
                    key={debugImgTick}
                    src={`${api.registrations.debugScreenshotUrl(debugRegId)}?t=${debugImgTick}`}
                    alt="debug screenshot"
                    className="w-full object-contain"
                    style={{ maxHeight: 420 }}
                    onError={(e) => { e.currentTarget.style.display = "none"; }}
                  />
                  <div className="px-2 py-1 text-[11px] text-slate-500">失败时浏览器会定格在 debug_waiting，可点“结束调试”放行；截图每2s刷新</div>
                </div>
                <div className="flex min-h-[260px] flex-col overflow-hidden rounded border border-slate-200">
                  <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50 px-2 py-1">
                    <span className="text-xs font-medium text-slate-600">HAR 抓包（最近80条）</span>
                    <div className="flex gap-1">
                      <a href={api.registrations.debugTraceUrl(debugRegId)} target="_blank" rel="noreferrer" className="rounded border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50">下载Trace</a>
                      <Button variant="ghost" size="sm" onClick={async () => { const d = await api.registrations.debugHar(debugRegId, { limit: 500 }); setDebugHar((d?.items||[]).slice(-80)); }}>刷新</Button>
                    </div>
                  </div>
                  <div className="flex-1 overflow-auto bg-white p-2 font-mono text-[11px] leading-4">
                    {debugHar.length === 0 ? <span className="text-slate-400">暂无请求（需开启“抓包调试”后才会记录）</span> :
                      debugHar.map((h, i) => (
                        <div key={i} className={`flex gap-2 py-0.5 ${h.type==="response" ? (h.status>=400?"text-red-600":"text-emerald-600") : "text-slate-700"}`}>
                          <span className="shrink-0 text-slate-400">{h.ts}</span>
                          <span className="shrink-0 rounded bg-slate-100 px-1">{h.method || h.type}</span>
                          <span className="truncate" title={h.url}>{h.url}</span>
                          {h.status && <span className="shrink-0">{h.status}</span>}
                        </div>
                      ))
                    }
                  </div>
                </div>
              </div>
            </Panel>
          )}

        </div>
      </div>

      {/* 凭据详情弹窗（历史记录查看凭据，受控脱敏） */}
      <Modal open={!!credDrawer} onClose={() => setCredDrawer(null)}
        title={credDrawer ? `凭据详情 · reg_${credDrawer.id}` : ""}>
        {credDrawer && (() => {
          const res = parseRegisterResult(credDrawer);
          if (!res) return <div className="text-xs text-slate-400">无凭据信息</div>;
          return (
            <div className="space-y-2.5">
              <div className="group rounded-md bg-slate-50 px-3 py-2">
                <div className="text-[11px] text-slate-400">邮箱</div>
                <PlainSecret label="邮箱" value={res.email} className="mt-0.5" />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div className="group rounded-md bg-slate-50 px-3 py-2">
                  <div className="text-[11px] text-slate-400">密码</div>
                  <PlainSecret label="密码" value={res.temp_email_password || ""} className="mt-1" />
                </div>
                <div className="group rounded-md bg-slate-50 px-3 py-2">
                  <div className="text-[11px] text-slate-400">TOTP Secret</div>
                  <PlainSecret label="TOTP Secret" value={res.totp_secret || ""} className="mt-1" />
                  {res.totp_secret && (
                    <a href={`https://2fa.show/2fa/${res.totp_secret}`} target="_blank" rel="noreferrer"
                      className="mt-1 inline-flex items-center gap-1 text-[11px] text-blue-600 hover:underline">
                      在线生成动态码 <ExternalLink size={10} />
                    </a>
                  )}
                </div>
              </div>
              <div className="rounded-md bg-slate-50 px-3 py-2">
                <div className="text-[11px] text-slate-400">账号标识 / 计划</div>
                <div className="mt-0.5 text-[13px] text-slate-800">{res.account_id || res.user_id || "—"}</div>
                <div className="mt-0.5 text-[11px] text-slate-400">计划：{res.plan_type || "free"}</div>
              </div>
              <div className="rounded-md bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
                凭据已明文展示，悬停「复制」图标可复制到剪贴板；请勿在共享屏幕时查看本窗口。
              </div>
              {credDrawer.account_id && (
                <Button variant="secondary" size="sm" onClick={() => { navigate("/accounts"); toast("已跳转账号管理", "info"); }}>
                  在账号管理查看 →
                </Button>
              )}
            </div>
          );
        })()}
      </Modal>
    </div>
  );
}
