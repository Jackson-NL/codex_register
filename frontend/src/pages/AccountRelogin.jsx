import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, CheckCircle2, ChevronDown, Copy, Database, Eye, History, KeyRound, List, LogIn, Plus, RefreshCw, RotateCcw, Save, Settings2, ShieldCheck, Square, Terminal, UserRound, Users } from "lucide-react";
import { Badge, Button, Input, Modal } from "../components/ui";
import { useApp } from "../context/AppContext";
import { api } from "../api";
import LocalAccountPicker from "./LocalAccountPicker";
import {
  buildReloginPayload, clearReloginSecrets, parseReloginAccounts,
  projectReloginJob, readReloginJobId, reloginJobActive, RELOGIN_STAGES,
  RELOGIN_STATUS, safeReloginError, saveReloginJobId,
} from "./accountReloginUtils";
import "./AccountRelogin.css";

const INITIAL_FORM = { mode: "single", email: "", password: "", totp_secret: "", batch: "", proxy: "", concurrency: 1, timeout_s: 90 };

function jobStorage() {
  try { return window.sessionStorage; } catch { return null; }
}

function timeLabel(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString();
}

function Status({ status }) {
  const meta = RELOGIN_STATUS[status] || { color: "neutral", label: "未开始" };
  return <Badge color={meta.color} dot>{meta.label}</Badge>;
}

export default function AccountRelogin() {
  const { toast, t } = useApp();
  const [form, setForm] = useState(INITIAL_FORM);
  const [jobId, setJobId] = useState(() => readReloginJobId(jobStorage()));
  const [job, setJob] = useState(null);
  const [history, setHistory] = useState([]);
  const [busy, setBusy] = useState(false);
  const [itemBusy, setItemBusy] = useState(null);
  const [error, setError] = useState("");
  const [showBatch, setShowBatch] = useState(false);
  // 异常账号自动入队状态（只读，不含任何凭据）
  const [autoQueue, setAutoQueue] = useState(null);
  // 从本地账号选号重登
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickedIds, setPickedIds] = useState([]);
  const [pickedEmails, setPickedEmails] = useState([]);
  // 凭据查看弹框：{ title, fields: [{ label, value }] } —— 用户自己决定是否复制
  const [credView, setCredView] = useState(null);
  const [credLoading, setCredLoading] = useState(false);
  const logEnd = useRef(null);
  // 最近一次提交的登录资料（仅本组件内存），用于「重试失败账号」；服务端从不保留密码/2FA
  const lastRecordsRef = useRef(null);
  const parsed = useMemo(() => parseReloginAccounts(form), [form]);
  const active = reloginJobActive(job?.status);
  const locked = busy || active || Boolean(jobId && !job);
  const hasInput = Boolean(form.password || form.totp_secret || form.batch);
  const rtCount = job?.items.filter((item) => item.has_refresh_token).length || 0;
  const atOnlyCount = job?.items.filter((item) => item.has_access_token && !item.has_refresh_token).length || 0;
  const savedCount = job?.items.filter((item) => item.saved_account_id).length || 0;

  const reloadHistory = useCallback(async () => {
    try {
      const response = await api.accountRelogin.jobs();
      setHistory((Array.isArray(response?.items) ? response.items : []).slice(0, 20).map(projectReloginJob).filter(Boolean));
    } catch (failure) { setError(safeReloginError(failure)); }
  }, []);

  useEffect(() => { reloadHistory(); }, [reloadHistory]);

  // 异常账号自动入队状态：低频轮询即可（后端默认 5 分钟一轮），失败静默不打扰。
  useEffect(() => {
    let disposed = false;
    const load = () => {
      api.accountRelogin.autoQueue()
        .then((snapshot) => { if (!disposed) setAutoQueue(snapshot || null); })
        .catch(() => { if (!disposed) setAutoQueue(null); });
    };
    load();
    const timer = setInterval(load, 30000);
    return () => { disposed = true; clearInterval(timer); };
  }, []);

  useEffect(() => {
    if (!jobId) return undefined;
    let disposed = false;
    let timer;
    let failures = 0;
    const poll = async () => {
      try {
        const response = await api.accountRelogin.job(jobId);
        if (disposed) return;
        const snapshot = projectReloginJob(response);
        if (!snapshot || !snapshot.id) throw new Error("Invalid job response");
        setJob(snapshot);
        setError("");
        failures = 0;
        if (reloginJobActive(snapshot.status)) timer = setTimeout(poll, document.hidden ? 5000 : 1200);
        else reloadHistory();
      } catch (failure) {
        if (disposed) return;
        setError(safeReloginError(failure));
        if (failure?.status === 404) {
          saveReloginJobId(null, jobStorage());
          setJobId(null);
          setJob(null);
          return;
        }
        failures += 1;
        timer = setTimeout(poll, Math.min(10000, 1200 * 2 ** Math.min(failures, 3)));
      }
    };
    poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [jobId, reloadHistory]);

  useEffect(() => {
    if (active && logEnd.current?.closest("details")?.open) {
      const log = logEnd.current.parentElement;
      log.scrollTop = log.scrollHeight;
    }
  }, [active, job?.logs.length]);

  const change = (key) => (event) => setForm((previous) => ({ ...previous, [key]: event.target.value }));

  // 从本地账号选号重登：只提交 id，密码由后端从库读取（不经过浏览器）。
  const startFromLocal = async (emails, ids) => {
    if (locked || !ids.length) return;
    setBusy(true);
    setError("");
    try {
      const response = await api.accountRelogin.createJobFromAccounts({
        account_ids: ids,
        proxy: String(form.proxy || "").trim(),
        concurrency: Number(form.concurrency) || 1,
        timeout_s: Number(form.timeout_s) || 90,
      });
      const created = projectReloginJob(response);
      if (!created?.id) throw new Error("Invalid job response");
      setJob(created);
      setJobId(created.id);
      saveReloginJobId(created.id, jobStorage());
      setPickedIds([]);
      setPickedEmails([]);
      reloadHistory();
      // skipped 只存在于原始响应（投影后的快照不带这个字段）
      const skipped = Array.isArray(response?.skipped) ? response.skipped : [];
      toast(
        skipped.length
          ? `已提交 ${ids.length - skipped.length} 个账号，跳过 ${skipped.length} 个`
          : `已提交 ${ids.length} 个本地账号重登`,
        skipped.length ? "warning" : "info",
      );
    } catch (failure) { setError(safeReloginError(failure)); }
    finally { setBusy(false); }
  };

  const start = async () => {
    if (locked || parsed.errors.length || !parsed.accounts.length) return;
    setBusy(true);
    setError("");
    try {
      const payload = buildReloginPayload(form);
      lastRecordsRef.current = payload.accounts; // 会话内存持有，供失败重试
      const created = projectReloginJob(await api.accountRelogin.createJob(payload));
      if (!created?.id) throw new Error("Invalid job response");
      setJob(created);
      setJobId(created.id);
      saveReloginJobId(created.id, jobStorage());
      setForm(clearReloginSecrets);
      setShowBatch(false);
      reloadHistory();
      toast("重登任务已提交，敏感输入已清空", "info");
    } catch (failure) { setError(safeReloginError(failure)); }
    finally { setBusy(false); }
  };

  // 重试失败账号：把本轮 failed 的账号按原登录资料重新提交为新任务（手动触发，不自动重试）
  const retryFailed = async () => {
    if (locked || !job || busy) return;
    const records = lastRecordsRef.current;
    if (!records?.length) {
      toast("登录资料已随页面刷新丢失，请重新输入后提交", "warning");
      return;
    }
    const failedEmails = new Set(
      job.items.filter((item) => item.status === "failed").map((item) => item.email),
    );
    const failed = records.filter((account) => failedEmails.has(account.email));
    if (!failed.length) {
      toast("本轮没有可重试的失败账号", "info");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = projectReloginJob(await api.accountRelogin.createJob({
        accounts: failed,
        proxy: String(form.proxy || "").trim(),
        concurrency: Number(form.concurrency) || 1,
        timeout_s: Number(form.timeout_s) || 90,
      }));
      if (!created?.id) throw new Error("Invalid job response");
      lastRecordsRef.current = failed; // 新一轮的可重试集合只含本轮账号
      setJob(created);
      setJobId(created.id);
      saveReloginJobId(created.id, jobStorage());
      reloadHistory();
      toast(`已重试 ${failed.length} 个失败账号（建议先在 Clash 里更换出口节点）`, "info");
    } catch (failure) { setError(safeReloginError(failure)); }
    finally { setBusy(false); }
  };

  const failedCount = job?.items.filter((item) => item.status === "failed").length || 0;
  const canRetry = Boolean(job && !active && failedCount > 0 && lastRecordsRef.current?.length);

  const stop = async () => {
    if (!jobId || busy) return;
    setBusy(true);
    try {
      setJob(projectReloginJob(await api.accountRelogin.cancel(jobId)));
      toast("已请求停止重登任务", "info");
      reloadHistory();
    } catch (failure) { setError(safeReloginError(failure)); }
    finally { setBusy(false); }
  };

  // 拉取完整凭据并弹框展示；是否复制由用户自己决定（不再点了就写剪贴板）。
  const viewCredentials = async (item, kind) => {
    if (itemBusy || !jobId) return;
    setItemBusy(item.id);
    setCredLoading(true);
    try {
      const credentials = await api.accountRelogin.credentials(jobId, item.id);
      const fields = [];
      if (kind === "web") {
        // 网页会话 = 网页 AT + session token（NextAuth 会话）
        fields.push({ label: t("网页 AT"), value: credentials.web_access_token || "" });
        fields.push({ label: t("session"), value: credentials.session_token || "" });
      } else if (kind === "refresh_token") {
        fields.push({ label: t("RT（Refresh Token）"), value: credentials.refresh_token || "" });
      } else {
        fields.push({ label: t("AT（Access Token）"), value: credentials.access_token || "" });
      }
      if (!fields.some((f) => f.value)) {
        toast("该类型凭据不可用", "warning");
        return;
      }
      setCredView({
        title: kind === "web" ? t("session") : kind === "refresh_token" ? t("RT") : t("AT"),
        email: item.email,
        fields,
      });
    } catch (failure) {
      toast(failure?.status ? safeReloginError(failure) : "读取凭据失败", "error");
    } finally { setItemBusy(null); setCredLoading(false); }
  };

  // 弹框里的「复制」按钮：只复制当前这一项
  const copyField = async (value) => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value);
      toast("已复制，请勿公开分享", "success");
    } catch {
      toast("复制失败，请检查浏览器剪贴板权限", "error");
    }
  };

  const saveAccount = async (item) => {
    if (itemBusy || !jobId) return;
    setItemBusy(item.id);
    try {
      const result = await api.accountRelogin.save(jobId, item.id);
      setJob((previous) => previous ? { ...previous, items: previous.items.map((row) => row.id === item.id ? { ...row, saved_account_id: result.account_id, save_error: "" } : row) } : previous);
      toast("凭据已保存到账号管理", "success");
    } catch (failure) { toast(safeReloginError(failure), "error"); }
    finally { setItemBusy(null); }
  };

  const selectJob = (selected) => {
    setJob(selected);
    setJobId(selected.id);
    setError("");
    saveReloginJobId(selected.id, jobStorage());
  };

  const newTask = () => {
    setJobId(null);
    setJob(null);
    setError("");
    saveReloginJobId(null, jobStorage());
  };

  return (
    <div className="relogin-workspace">
      <header className="relogin-page-head">
        <div>
          <p className="relogin-eyebrow">ACCOUNT REAUTHORIZE</p>
          <h1>{t("账号重登")}</h1>
          <p className="relogin-description">{t("邮箱 / 密码 / 2FA · 纯协议登录 · RT 优先，AT 兜底")}</p>
        </div>
        <div className="relogin-page-state"><span className={active ? "relogin-live-dot is-active" : "relogin-live-dot"} />{t(active ? "正在协议重登" : "HTTP 协议")}</div>
      </header>

      {error && <div role="alert" className="relogin-error">{t(error)}</div>}

      {/* 异常账号自动入队：只读状态，不含任何凭据 */}
      {autoQueue && (
        <div className="relogin-small-tag" role="status" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", padding: "6px 10px" }}>
          <span>{t("异常账号自动重登")}：{autoQueue.enabled ? t("已开启") : t("未启用")}</span>
          {autoQueue.enabled && (
            <>
              <span>{t("每")} {Math.round((autoQueue.interval_seconds || 0) / 60)} {t("分钟扫描")}</span>
              <span>{t("每轮最多")} {autoQueue.batch_size} {t("个")}</span>
              <span>{t("失败")} {autoQueue.max_attempts} {t("次后跳过")}</span>
              {Array.isArray(autoQueue.tracked) && autoQueue.tracked.length > 0 && (
                <span>{t("跟踪中")} {autoQueue.tracked.length} {t("个")}</span>
              )}
            </>
          )}
        </div>
      )}

      {/* 凭据查看弹框：显示完整值，复制与否由用户决定 */}
      <Modal
        open={Boolean(credView)}
        onClose={() => setCredView(null)}
        title={credView ? `${credView.title} · ${credView.email || ""}` : ""}
        width={760}
        footer={<div className="flex justify-end gap-2"><Button variant="secondary" size="sm" onClick={() => setCredView(null)}>{t("关闭")}</Button></div>}
      >
        <div className="space-y-3 p-1">
          {credView?.fields.map((field) => (
            <div key={field.label} className="space-y-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-medium text-slate-600">{field.label}</span>
                <span className="text-[10px] text-slate-400">{field.value ? `${field.value.length} ${t("字符")}` : t("不可用")}</span>
              </div>
              <textarea
                readOnly
                className="mono w-full resize-y rounded border border-slate-200 bg-slate-50 p-2 text-[11px] leading-relaxed text-slate-700"
                rows={field.value && field.value.length > 120 ? 6 : 2}
                value={field.value || ""}
                aria-label={field.label}
                spellCheck={false}
                onFocus={(event) => event.target.select()}
              />
              <div className="flex justify-end">
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<Copy size={11} />}
                  disabled={!field.value}
                  onClick={() => copyField(field.value)}
                >
                  {t("复制")}
                </Button>
              </div>
            </div>
          ))}
          <p className="text-[10px] leading-5 text-slate-400">{t("完整凭据仅在本机内存中短暂存在，请勿截图或公开分享。关闭后不会自动复制。")}</p>
        </div>
      </Modal>

      <LocalAccountPicker
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        busyEmails={new Set((job?.items || []).filter((item) => reloginJobActive(item.status)).map((item) => item.email))}
        onPicked={(emails, ids) => { setPickedEmails(emails); setPickedIds(ids); }}
      />

      <div className="relogin-grid">
        <section className="relogin-card relogin-input-card" aria-labelledby="relogin-input-title">
          <header className="relogin-card-head">
            <div className="relogin-heading-group">
              <span className="relogin-section-icon"><KeyRound size={18} strokeWidth={1.7} /></span>
              <div><p className="relogin-eyebrow">ACCOUNT INPUT</p><h2 id="relogin-input-title">{t("登录凭据")}</h2></div>
            </div>
            <span className="relogin-small-tag">{t("最多 20 个")}</span>
          </header>

          <div className="relogin-input-body">
            <div role="group" aria-label={t("输入方式")} className="relogin-mode-switch">
              {[["single", "单个账号", UserRound, "邮箱 · 密码 · 2FA"], ["batch", "批量输入", List, "每行一条登录资料"]].map(([mode, label, Icon, description]) => (
                <button key={mode} type="button" disabled={locked} aria-pressed={form.mode === mode} onClick={() => setForm((previous) => ({ ...previous, mode }))} className={form.mode === mode ? "is-selected" : ""}>
                  <Icon size={17} strokeWidth={1.7} aria-hidden="true" />
                  <span><span className="relogin-mode-label">{t(label)}</span><span className="relogin-mode-hint" aria-hidden="true">{t(description)}</span></span>
                </button>
              ))}
            </div>

            <div className="relogin-credential-fields">
              {/* 从本地账号库选号：正常账号也可选，凭据由后端读取 */}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<Users size={14} />}
                  disabled={locked}
                  onClick={() => setPickerOpen(true)}
                >
                  {t("从本地账号选择")}
                </Button>
                {pickedIds.length > 0 && (
                  <>
                    <span className="text-[11px] text-slate-500">
                      {t("已选")} <b className="tnum text-slate-800">{pickedIds.length}</b> {t("个本地账号")}
                    </span>
                    <Button
                      variant="primary"
                      size="sm"
                      icon={<LogIn size={13} />}
                      loading={busy}
                      disabled={locked}
                      onClick={() => startFromLocal(pickedEmails, pickedIds)}
                    >
                      {t("重登所选账号")}
                    </Button>
                    <button type="button" className="text-[11px] text-slate-400 underline-offset-2 hover:text-slate-600 hover:underline" onClick={() => { setPickedIds([]); setPickedEmails([]); }}>
                      {t("清除选择")}
                    </button>
                  </>
                )}
              </div>

              {form.mode === "single" ? <>
                <Input label="邮箱" type="email" name="relogin-email" autoComplete="off" placeholder="name@example.com" icon={<UserRound size={15} />} value={form.email} onChange={change("email")} disabled={locked} maxLength={254} />
                <Input label="密码" type="password" name="relogin-password" autoComplete="off" placeholder={t("请输入账号密码")} icon={<KeyRound size={15} />} value={form.password} onChange={change("password")} disabled={locked} maxLength={512} />
                <Input label="2FA 密钥（可选）" type="password" name="relogin-totp" autoComplete="off" spellCheck={false} placeholder="Base32 TOTP secret" icon={<ShieldCheck size={15} />} value={form.totp_secret} onChange={change("totp_secret")} disabled={locked} maxLength={140} hint="填写长期 TOTP 密钥，不是六位验证码；未启用 2FA 可留空。" />
              </> : <>
                <label className="relogin-batch-label"><span>{t("每行一个账号，最多 20 个")}</span>
                  <textarea className="input relogin-batch-input" aria-label={t("批量登录凭据")} autoComplete="off" spellCheck={false} maxLength={18000} value={form.batch} onChange={change("batch")} disabled={locked} placeholder={"email----password----TOTP_SECRET\nemail----password"} style={showBatch ? undefined : { WebkitTextSecurity: "disc" }} />
                </label>
                <label className="relogin-show-input"><input type="checkbox" checked={showBatch} onChange={(event) => setShowBatch(event.target.checked)} />{t("显示批量输入内容")}</label>
                <p className="relogin-field-note">{t("格式：邮箱----密码----2FA密钥；未启用 2FA 时可省略第三项。请勿包含分隔符 ----。")}</p>
              </>}
            </div>

            {hasInput && parsed.errors.length > 0 && <div role="alert" className="relogin-validation">{parsed.errors.slice(0, 3).map((message) => <div key={message}>{t(message)}</div>)}</div>}

            <details className="relogin-advanced">
              <summary><span><Settings2 size={14} />{t("连接设置")}</span><span className="relogin-summary-meta">{t("代理 / 超时")}<ChevronDown size={13} /></span></summary>
              <div className="relogin-advanced-fields">
                <Input label="代理（可选）" autoComplete="off" placeholder="留空使用后端默认代理" value={form.proxy} onChange={change("proxy")} disabled={locked} maxLength={2048} />
                <Input label="单账号超时（秒）" type="number" min={30} max={180} step={1} value={form.timeout_s} onChange={change("timeout_s")} disabled={locked} />
              </div>
            </details>

            <div className="relogin-protocol-note"><ShieldCheck size={16} strokeWidth={1.7} /><div><strong>{t("RT 优先，AT 兜底")}</strong><p>{t("成功后自动保存到账号管理；密码与2FA密钥不落库。任务详情临时保留，服务重启后清空；不会自动使用浏览器处理额外验证。")}</p></div></div>
          </div>

          <div className="relogin-submit-bar">
            <div className="relogin-execution-mode">
              <label htmlFor="relogin-concurrency">{t("执行模式")}</label>
              <select id="relogin-concurrency" aria-label={t("并发数（1–3）")} value={form.concurrency} onChange={change("concurrency")} disabled={locked}>
                <option value={1}>{t("顺序重登 · 1 个账号")}</option>
                <option value={2}>{t("并发重登 · 2 个账号")}</option>
                <option value={3}>{t("并发重登 · 3 个账号")}</option>
              </select>
            </div>
            <p className="relogin-field-note">{t("每个账号独立会话，同一邮箱不重复执行。额外验证会明确提示。")}</p>
            <div className="relogin-submit-actions">
              {active ? <Button className="relogin-start" variant="danger" icon={<Square size={15} />} loading={busy} onClick={stop}>停止任务</Button>
                : <Button className="relogin-start" variant="success" icon={<LogIn size={16} />} loading={busy} disabled={locked || parsed.errors.length > 0 || !parsed.accounts.length} onClick={start}>开始重登</Button>}
              <button type="button" className="relogin-clear" disabled={locked || !hasInput} onClick={() => { setForm(clearReloginSecrets); setShowBatch(false); }}>{t("清空输入")}</button>
            </div>
            <div className="relogin-input-footer"><span><Database size={12} />{t("成功自动入库")}</span><span>{t("敏感输入提交后清空")}</span></div>
          </div>
        </section>

        <section className="relogin-card relogin-results-card" aria-labelledby="relogin-results-title">
          <header className="relogin-card-head">
            <div><p className="relogin-eyebrow">TASK RESULTS</p><h2 id="relogin-results-title">{t("重登结果")}</h2></div>
            <div className="relogin-result-actions">
              <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={reloadHistory}>刷新任务</Button>
              {canRetry && (
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<RotateCcw size={13} />}
                  loading={busy}
                  onClick={retryFailed}
                  title="把本轮失败账号重新提交为新任务；建议先更换出口节点再重试（challenge 类失败多为出口 IP 被风控）"
                >
                  重试失败账号 ({failedCount})
                </Button>
              )}
              {job && !active && <Button variant="secondary" size="sm" icon={<Plus size={13} />} onClick={newTask}>新建任务</Button>}
            </div>
          </header>

          <div className="relogin-metrics" role="group" aria-label={t("重登结果统计")}>
            {[["登录成功", job?.succeeded || 0, "success"], ["已取得 RT", rtCount, ""], ["仅取得 AT", atOnlyCount, ""], ["已入库", savedCount, "success"]].map(([label, value, color]) => (
              <div key={label} className={`relogin-metric ${color}`}><span>{t(label)}</span><strong>{String(value).padStart(2, "0")}</strong></div>
            ))}
          </div>

          <div className="relogin-result-context"><span>{t("本次账号")} <strong>{job?.total || 0}</strong><span className="relogin-context-divider">/</span>{t("已完成")} <strong>{job?.completed || 0}</strong></span><Status status={job?.status} /></div>

          <div className="relogin-result-table-wrap">
            <table className="relogin-result-table">
              <caption className="sr-only">{t("重登结果")}</caption>
              <thead><tr>{["邮箱 / 阶段", "凭据", "状态 / 入库", "操作"].map((title) => <th key={title} scope="col">{t(title)}</th>)}</tr></thead>
              <tbody>{job?.items.map((item) => <tr key={item.id}>
                <td><div className="relogin-account-email" data-no-translate>{item.email}</div><div className="relogin-account-stage">{t(RELOGIN_STAGES[item.stage] || "等待执行")}</div>
                  {item.error && <p className="relogin-row-error" data-no-translate>{item.error}</p>}
                  {item.warning && <p className="relogin-row-warning" data-no-translate>{item.warning}</p>}
                  {item.save_error && <p className="relogin-row-error" data-no-translate>自动保存失败：{item.save_error}</p>}
                </td>
                <td><div className="relogin-token-badges"><Badge color={item.has_refresh_token ? "success" : "neutral"}>RT {item.has_refresh_token ? "✓" : "—"}</Badge><Badge color={item.has_access_token ? "success" : "neutral"}>AT {item.has_access_token ? "✓" : "—"}</Badge></div><div className="relogin-account-stage">{t(item.has_web_session ? "session ✓" : "session —")}</div>{item.has_access_token && <div className="relogin-account-stage">{t(item.token_source === "oauth" ? "OAuth 凭据" : "网页 AT")}</div>}</td>
                <td><Status status={item.status} />{item.saved_account_id ? <div className="relogin-save-state"><CheckCircle2 size={12} />{t("已入库")} <span className="tnum">#{item.saved_account_id}</span></div> : item.has_access_token && <div className={item.save_error ? "relogin-row-error" : "relogin-account-stage"}>{t(item.save_error ? "未入库" : "等待保存")}</div>}</td>
                <td><div className="relogin-row-actions">
                  {item.has_access_token && <Button size="sm" variant="secondary" icon={<Eye size={11} />} disabled={Boolean(itemBusy)} onClick={() => viewCredentials(item, "access_token")}>AT</Button>}
                  {item.has_refresh_token && <Button size="sm" variant="secondary" icon={<Eye size={11} />} disabled={Boolean(itemBusy)} onClick={() => viewCredentials(item, "refresh_token")}>RT</Button>}
                  {item.has_web_session && <Button size="sm" variant="secondary" icon={<Eye size={11} />} disabled={Boolean(itemBusy)} onClick={() => viewCredentials(item, "web")}>session</Button>}
                  {item.has_access_token && <Button size="sm" variant={item.saved_account_id ? "ghost" : "secondary"} icon={item.saved_account_id ? <CheckCircle2 size={11} /> : <Save size={11} />} loading={itemBusy === item.id} disabled={Boolean(itemBusy) || Boolean(item.saved_account_id)} onClick={() => saveAccount(item)}>{item.saved_account_id ? "已保存" : item.save_error ? "重试保存" : "保存账号"}</Button>}
                </div></td>
              </tr>)}</tbody>
            </table>
            {!job?.items.length && <div className="relogin-empty-state"><span className="relogin-empty-icon"><LogIn size={29} strokeWidth={1.3} /></span><h3>{t("等待重登账号")}</h3><p>{t("在左侧填写登录资料，结果将在这里显示。")}</p><span className="relogin-empty-flow">{t("验证账号")}<ArrowRight size={12} />{t("获取凭据")}<ArrowRight size={12} />{t("自动入库")}</span></div>}
          </div>

          <details className="relogin-log-panel">
            <summary><span><Terminal size={14} />{t("执行日志")}<span className="relogin-summary-count">{job?.logs.length || 0}</span></span><span className="relogin-summary-meta">{t("不含凭据明文")}<ChevronDown size={13} /></span></summary>
            <div role="log" aria-label={t("重登执行日志")} aria-live="polite" className="relogin-log-content" data-no-translate>
              {job?.logs.length ? job.logs.map((line, index) => <div key={`${index}-${line.time}`}><time>{timeLabel(line.time)}</time>{line.message}</div>) : <div>{t("等待任务开始")}</div>}
              <div ref={logEnd} />
            </div>
          </details>

          <details className="relogin-history-panel">
            <summary><span><History size={14} />{t("最近任务")}<span className="relogin-summary-count">{history.length}</span></span><span className="relogin-summary-meta">{t("仅当前服务内存")}<ChevronDown size={13} /></span></summary>
            <div className="relogin-history-list">{history.length ? history.slice(0, 5).map((entry) => <button type="button" key={entry.id} aria-pressed={entry.id === jobId} onClick={() => selectJob(entry)} className={entry.id === jobId ? "is-selected" : ""}><span><strong>{entry.total} {t("个账号")}</strong><span>{entry.completed} {t("已完成")} · {timeLabel(entry.created_at)}</span></span><Status status={entry.status} /></button>) : <p>{t("暂无任务；服务重启后记录会清空")}</p>}</div>
          </details>

          <footer className="relogin-results-footer"><span className="tnum">{job?.total || 0} {t("个账号")}<span className="relogin-context-divider">·</span>{t("已入库")} {savedCount}</span><span><span className="relogin-live-dot" />{t("临时任务")}</span></footer>
        </section>
      </div>
    </div>
  );
}
