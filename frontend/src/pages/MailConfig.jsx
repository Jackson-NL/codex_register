import { useEffect, useMemo, useState } from "react";
import {
  AtSign, CheckCircle2, Cloud, Eye, EyeOff, Inbox, KeyRound, Loader2, Mail,
  RefreshCw, RotateCcw, Save, ShieldCheck, Users, XCircle, Upload,
  Trash2, LockKeyhole, ClipboardPaste, ListChecks,
} from "lucide-react";
import { api } from "../api";
import { useApp } from "../context/AppContext";
import { Badge, Button, Input, Panel, Select, Switch } from "../components/ui";
import { fmtTime } from "../mock/data";

const MASK = "••••••••";

const DEFAULT_FORM = {
  provider: "cf_temp_email",
  cf_temp_email: {
    enabled: true,
    base_url: "https://temp-api.708651.xyz",
    domain: "708651.xyz",
    address_mode: "generated",
    custom_pool: "",
    custom_pool_count: 0,
    custom_pool_sample: [],
    custom_pool_status_counts: { unused: 0, in_use: 0, used: 0, failed: 0, disabled: 0 },
    custom_pool_items: [],
    custom_pool_summary: {},
    inbox_address: "",
    inbox_jwt: "",
    has_inbox_jwt: false,
    site_password: "",
    name_prefix: "reg",
    random_length: 10,
    poll_interval: 4,
    poll_timeout: 180,
    max_retries: 3,
    rate_limit_backoff: 10,
    has_site_password: false,
  },
  outlook: {
    enabled: false,
    mode: "manual_pool",
    accounts_pool: "",
    accounts_count: 0,
    accounts_sample: [],
    poll_interval: 5,
    poll_timeout: 180,
    sender_filter: "",
    subject_filter: "",
    imap_host: "outlook.office365.com",
    imap_port: 993,
    imap_ssl: true,
    graph_tenant_id: "",
    graph_client_id: "",
    graph_client_secret: "",
    has_graph_client_secret: false,
  },
};

function normalizeConfig(data) {
  const cf = { ...DEFAULT_FORM.cf_temp_email, ...(data?.cf_temp_email || {}) };
  const outlook = { ...DEFAULT_FORM.outlook, ...(data?.outlook || {}) };
  cf.site_password = cf.has_site_password ? MASK : "";
  cf.custom_pool = "";
  cf.inbox_jwt = cf.has_inbox_jwt ? MASK : "";
  outlook.accounts_pool = "";
  outlook.graph_client_secret = outlook.has_graph_client_secret ? MASK : "";
  return {
    provider: data?.provider || DEFAULT_FORM.provider,
    cf_temp_email: cf,
    outlook,
  };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function parsePoolText(value) {
  const seen = new Set();
  const valid = [];
  const invalid = [];
  for (const raw of String(value || "").split(/[\n,;]+/)) {
    const address = raw.trim().toLowerCase();
    if (!address) continue;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) invalid.push(address);
    else if (!seen.has(address)) { seen.add(address); valid.push(address); }
  }
  return { valid, invalid };
}

function fieldErrors(form) {
  const errors = {};
  const cf = form.cf_temp_email;
  try {
    const url = new URL(cf.base_url);
    if (!["http:", "https:"].includes(url.protocol)) errors.base_url = "必须是 http(s) URL";
  } catch {
    errors.base_url = "请输入有效的 http(s) URL";
  }
  if (!String(cf.domain || "").trim()) errors.domain = "域名不能为空";
  if (!["generated", "custom_pool"].includes(cf.address_mode)) errors.address_mode = "地址来源不正确";
  if (cf.address_mode === "custom_pool") {
    if (!String(cf.inbox_address || "").trim() || !String(cf.inbox_address).includes("@")) {
      errors.inbox_address = "固定收件邮箱格式不正确";
    }
    if (!cf.has_inbox_jwt && !String(cf.inbox_jwt || "").trim()) errors.inbox_jwt = "请配置固定收件 JWT";
    if (!cf.custom_pool_count && !String(cf.custom_pool || "").trim()) errors.custom_pool = "请添加至少一个自定义邮箱";
  }
  for (const key of ["random_length", "poll_interval", "poll_timeout", "max_retries", "rate_limit_backoff"]) {
    if (!Number.isInteger(Number(cf[key])) || Number(cf[key]) <= 0) errors[`cf_${key}`] = "必须为正整数";
  }
  const out = form.outlook;
  if (!Number.isInteger(Number(out.poll_interval)) || Number(out.poll_interval) <= 0) errors.out_poll_interval = "必须为正整数";
  if (!Number.isInteger(Number(out.poll_timeout)) || Number(out.poll_timeout) <= 0) errors.out_poll_timeout = "必须为正整数";
  if (!Number.isInteger(Number(out.imap_port)) || Number(out.imap_port) <= 0) errors.imap_port = "必须为正整数";
  return errors;
}

function FormNumber({ label, value, onChange, error, hint }) {
  return (
    <Input
      label={label}
      type="number"
      min="1"
      value={value}
      error={error}
      hint={hint}
      onChange={(event) => onChange(Number(event.target.value))}
    />
  );
}

function SecretInput({ label, value, configured, visible, onToggle, onChange, hint, error }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      <div className="relative">
        <input
          className={`input pr-9 ${error ? "border-red-400" : ""}`}
          type={visible ? "text" : "password"}
          value={value}
          placeholder={configured ? "已配置，留空不修改" : "可选"}
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          type="button"
          title={visible ? "隐藏字段" : "显示字段"}
          onClick={onToggle}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-700"
        >
          {visible ? <EyeOff size={14} /> : <Eye size={14} />}
        </button>
      </div>
      {error ? <span className="mt-1 block text-[11px] text-red-600">{error}</span> : hint && <span className="mt-1 block text-[11px] text-slate-400">{hint}</span>}
    </label>
  );
}

function ProviderCard({ active, icon, title, description, status, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex min-h-[96px] items-start gap-3 rounded-md border p-3 text-left transition-colors ${
        active ? "border-blue-500 bg-blue-50/50 ring-1 ring-blue-500" : "border-slate-200 bg-white hover:border-slate-300"
      }`}
    >
      <span className={`mt-0.5 ${active ? "text-blue-600" : "text-slate-400"}`}>{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2 text-[13px] font-semibold text-slate-700">
          {title}
          {active && <Badge color="primary">当前启用</Badge>}
        </span>
        <span className="mt-1 block text-xs leading-relaxed text-slate-400">{description}</span>
        <span className="mt-2 block text-[11px] text-slate-500">{status}</span>
      </span>
    </button>
  );
}

export default function MailConfig() {
  const { toast, t } = useApp();
  const [form, setForm] = useState(clone(DEFAULT_FORM));
  const [serverForm, setServerForm] = useState(null);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [lastTest, setLastTest] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [poolImport, setPoolImport] = useState("");
  const [poolImportMode, setPoolImportMode] = useState("append");
  const [poolImportOpen, setPoolImportOpen] = useState(false);
  const [poolImportError, setPoolImportError] = useState("");
  const [selectedIds, setSelectedIds] = useState([]);
  const [poolBusy, setPoolBusy] = useState(false);
  const [touched, setTouched] = useState({ sitePassword: false, inboxJwt: false, accountsPool: false, graphSecret: false });
  const [visible, setVisible] = useState({ sitePassword: false, inboxJwt: false, graphSecret: false });

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.mailConfig.get();
      const normalized = normalizeConfig(data);
      setForm(normalized);
      setServerForm(clone(normalized));
      setUpdatedAt(data.updated_at || null);
      setLastTest(data.test_status || null);
      setPoolImport("");
      setPoolImportError("");
      setSelectedIds([]);
      setTouched({ sitePassword: false, inboxJwt: false, accountsPool: false, graphSecret: false });
    } catch (error) {
      toast(`邮箱配置加载失败: ${error.message}`, "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const patch = (section, key, value) => {
    setForm((current) => ({ ...current, [section]: { ...current[section], [key]: value } }));
    if (section === "cf_temp_email" && key === "site_password") setTouched((state) => ({ ...state, sitePassword: true }));
    if (section === "cf_temp_email" && key === "inbox_jwt") setTouched((state) => ({ ...state, inboxJwt: true }));
    if (section === "outlook" && key === "accounts_pool") setTouched((state) => ({ ...state, accountsPool: true }));
    if (section === "outlook" && key === "graph_client_secret") setTouched((state) => ({ ...state, graphSecret: true }));
  };

  const applyConfigResponse = (data) => {
    const normalized = normalizeConfig(data);
    setForm(normalized);
    setServerForm(clone(normalized));
    setUpdatedAt(data.updated_at || null);
    setLastTest(data.test_status || null);
  };

  const importPool = async () => {
    const { valid, invalid } = parsePoolText(poolImport);
    if (!valid.length) {
      setPoolImportError(invalid.length ? `没有可导入的有效邮箱（${invalid.length} 条格式错误）` : "请先粘贴至少一个邮箱地址");
      return;
    }
    const savedCount = form.cf_temp_email.custom_pool_count || 0;
    const append = poolImportMode === "append" && savedCount > 0;
    if (!append && savedCount > 0) {
      if (!window.confirm(`替换全部会从池中移除当前 ${savedCount} 个地址（历史状态保留），确认替换？`)) return;
    }
    setPoolBusy(true);
    setPoolImportError("");
    try {
      // 导入即保存：追加模式由后端与已保存池合并（明文不回显前端）。
      const data = await api.mailConfig.save({
        cf_temp_email: buildCfPayload({
          custom_pool: valid.join("\n"),
          custom_pool_append: append || undefined,
        }),
      });
      applyConfigResponse(data);
      setPoolImport("");
      setPoolImportOpen(false);
      toast(
        `已导入 ${valid.length} 条地址${invalid.length ? `，忽略 ${invalid.length} 条格式错误` : ""}`,
        invalid.length ? "warning" : "success"
      );
    } catch (error) {
      setPoolImportError(`导入失败: ${error.message}`);
    } finally {
      setPoolBusy(false);
    }
  };

  const errors = useMemo(() => fieldErrors(form), [form]);
  const dirty = useMemo(() => JSON.stringify(form) !== JSON.stringify(serverForm), [form, serverForm]);
  const cfConfigured = form.cf_temp_email.has_site_password;
  const graphConfigured = form.outlook.has_graph_client_secret;
  const poolStatus = form.cf_temp_email.custom_pool_status_counts || {};
  const poolSummary = form.cf_temp_email.custom_pool_summary || {};
  const savedPoolItems = form.cf_temp_email.custom_pool_items || [];
  const poolStatusMeta = {
    unused: { label: "未使用", color: "success" },
    in_use: { label: "使用中", color: "info" },
    used: { label: "已使用", color: "neutral" },
    failed: { label: "失败", color: "danger" },
    disabled: { label: "已停用", color: "neutral" },
  };
  const poolReasonMeta = {
    pre_submit: { label: "可回收", color: "warning" },
    unknown: { label: "待确认", color: "neutral" },
    crash_recovered: { label: "崩溃恢复", color: "neutral" },
    lease_expired: { label: "租约超时", color: "neutral" },
    manual: { label: "人工标记", color: "neutral" },
  };

  const togglePoolSelect = (id) => {
    setSelectedIds((ids) => (ids.includes(id) ? ids.filter((item) => item !== id) : [...ids, id]));
  };
  const toggleSelectAll = () => {
    setSelectedIds((ids) => (ids.length === savedPoolItems.length ? [] : savedPoolItems.map((item) => item.id)));
  };
  const runPoolAction = async (method, payload, label, options = {}) => {
    if (!payload.ids.length && !options.allowEmpty) return;
    setPoolBusy(true);
    try {
      const result = await api.mailConfig[method](payload);
      setSelectedIds([]);
      const skipped = result?.skipped?.length || 0;
      const detail = skipped ? `，跳过 ${skipped} 个（${result.skipped[0]?.reason || ""}）` : "";
      toast(`${label}完成：${result?.affected ?? 0} 个${detail}`, skipped ? "warning" : "success");
      await load();
    } catch (error) {
      toast(`${label}失败: ${error.message}`, "error");
    } finally {
      setPoolBusy(false);
    }
  };
  const requeueSelected = async () => {
    if (!selectedIds.length) return;
    setPoolBusy(true);
    try {
      let result = await api.mailConfig.poolRequeue({ ids: selectedIds });
      if (!result.affected && result.skipped?.length) {
        const ok = window.confirm(`选中的 ${result.skipped.length} 个地址并非「可证明未消费」，强制回收可能复用半消费邮箱。确认强制回收？`);
        if (ok) result = await api.mailConfig.poolRequeue({ ids: selectedIds, force: true });
      }
      setSelectedIds([]);
      toast(`回收完成：${result.affected} 个`, result.affected ? "success" : "warning");
      await load();
    } catch (error) {
      toast(`回收失败: ${error.message}`, "error");
    } finally {
      setPoolBusy(false);
    }
  };
  const removePoolItems = async (ids) => {
    if (!ids.length) return;
    if (!window.confirm(`确认从地址池移除 ${ids.length} 个地址？（仅移除池成员，不影响账号管理中的账号）`)) return;
    setPoolBusy(true);
    try {
      const result = await api.mailConfig.poolRemove({ ids });
      const skipped = result?.skipped?.length || 0;
      const detail = skipped ? `，跳过 ${skipped} 个（${result.skipped[0]?.reason || ""}）` : "";
      toast(`移除完成：${result?.affected ?? 0} 个${detail}`, skipped ? "warning" : "success");
      setSelectedIds([]);
      await load();
    } catch (error) {
      toast(`移除失败: ${error.message}`, "error");
    } finally {
      setPoolBusy(false);
    }
  };

  const buildCfPayload = (overrides) => {
    const cf = { ...form.cf_temp_email };
    delete cf.has_site_password;
    // 池成员只经「批量导入 / 移除」专用动作修改，普通保存不携带 custom_pool。
    delete cf.custom_pool;
    delete cf.custom_pool_append;
    delete cf.custom_pool_count;
    delete cf.custom_pool_sample;
    delete cf.custom_pool_status_counts;
    delete cf.custom_pool_items;
    delete cf.custom_pool_summary;
    if (!touched.sitePassword || !cf.site_password || cf.site_password === MASK) delete cf.site_password;
    if (!touched.inboxJwt || !cf.inbox_jwt || cf.inbox_jwt === MASK) delete cf.inbox_jwt;
    return { ...cf, ...(overrides || {}) };
  };

  const buildPayload = () => {
    const outlook = { ...form.outlook };
    delete outlook.accounts_count;
    delete outlook.accounts_sample;
    delete outlook.has_graph_client_secret;
    if (!touched.accountsPool) delete outlook.accounts_pool;
    if (!touched.graphSecret || !outlook.graph_client_secret || outlook.graph_client_secret === MASK) {
      delete outlook.graph_client_secret;
    }
    return { provider: form.provider, cf_temp_email: buildCfPayload(), outlook };
  };

  const validate = () => {
    if (Object.keys(errors).length) {
      toast("存在校验错误，请修正后保存", "warning");
      return false;
    }
    return true;
  };

  const save = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      const data = await api.mailConfig.save(buildPayload());
      const normalized = normalizeConfig(data);
      setForm(normalized);
      setServerForm(clone(normalized));
      setUpdatedAt(data.updated_at || null);
      setLastTest(data.test_status || lastTest);
      setTouched({ sitePassword: false, inboxJwt: false, accountsPool: false, graphSecret: false });
      setSelectedIds([]);
      toast("邮箱配置已保存", "success");
    } catch (error) {
      toast(`保存失败: ${error.message}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const testConnection = async () => {
    if (!validate()) return;
    setTesting(true);
    try {
      const result = await api.mailConfig.test({ provider: form.provider, config: buildPayload() });
      setLastTest(result);
      toast(result.ok ? "邮箱连接测试成功" : `邮箱连接测试失败: ${result.message}`, result.ok ? "success" : "error");
    } catch (error) {
      const result = { ok: false, message: error.message };
      setLastTest(result);
      toast(`测试失败: ${error.message}`, "error");
    } finally {
      setTesting(false);
    }
  };

  const reset = () => {
    if (!serverForm) return;
    setForm(clone(serverForm));
    setSelectedIds([]);
    setTouched({ sitePassword: false, inboxJwt: false, accountsPool: false, graphSecret: false });
    toast("已撤销未保存修改", "info");
  };

  if (loading) {
    return <div className="flex items-center gap-2 py-12 text-sm text-slate-400"><Loader2 size={16} className="animate-spin" />正在加载邮箱配置…</div>;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[16px] font-semibold text-slate-800">邮箱配置</h2>
          <div className="mt-0.5 text-xs text-slate-400">注册流程邮箱 Provider · Cloudflare 临时邮箱与 Outlook 账号池</div>
        </div>
        <div className="flex items-center gap-2">
          {dirty && <Badge color="warning">有未保存修改</Badge>}
          <Button variant="secondary" size="sm" icon={<RotateCcw size={13} />} onClick={reset} disabled={!dirty}>撤销</Button>
          <Button size="sm" icon={<Save size={13} />} onClick={save} loading={saving}>保存配置</Button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Panel title="当前 Provider">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
            <Mail size={16} className="text-blue-600" />
            {form.provider === "cf_temp_email" ? "Cloudflare 临时邮箱" : "Outlook 账号池"}
          </div>
          <div className="mt-1 text-xs text-slate-400">{form.provider}</div>
        </Panel>
        <Panel title="最近保存">
          <div className="text-sm font-semibold text-slate-700">{updatedAt ? fmtTime(updatedAt) : "尚未记录"}</div>
          <div className="mt-1 text-xs text-slate-400">配置写入后重启仍会保留</div>
        </Panel>
        <Panel title="最近测试">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
            {lastTest?.ok ? <CheckCircle2 size={16} className="text-emerald-500" /> : <XCircle size={16} className="text-slate-300" />}
            {lastTest ? (lastTest.ok ? "连接正常" : "连接失败") : "尚未测试"}
          </div>
          <div className="mt-1 truncate text-xs text-slate-400" title={lastTest?.message}>{lastTest?.message || "保存后可测试当前配置"}</div>
        </Panel>
      </div>

      <Panel title="选择邮箱 Provider" extra={<Badge color={form.provider === "cf_temp_email" ? "info" : "primary"}>仅支持 2 种</Badge>}>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <ProviderCard
            active={form.provider === "cf_temp_email"}
            icon={<Cloud size={18} />}
            title="Cloudflare 临时邮箱"
            description="自动创建地址并轮询解析邮件验证码。"
            status={form.cf_temp_email.enabled ? "已启用 · 自动收信" : "已停用"}
            onClick={() => setForm((current) => ({ ...current, provider: "cf_temp_email" }))}
          />
          <ProviderCard
            active={form.provider === "outlook"}
            icon={<Inbox size={18} />}
            title="Outlook 账号池"
            description="导入固定账号池；manual_pool 自动收信仍需 IMAP/Graph。"
            status={t(`${form.outlook.accounts_count} 个账号 · ${form.outlook.enabled ? "已启用" : "已停用"}`)}
            onClick={() => setForm((current) => ({ ...current, provider: "outlook" }))}
          />
        </div>
      </Panel>

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <Panel
          title={<span className="flex items-center gap-2"><Cloud size={15} className="text-blue-600" />cf_temp_email</span>}
          extra={form.provider === "cf_temp_email" && <Badge color="info" dot>当前启用</Badge>}
        >
          <div className="space-y-4">
            <div className="flex items-center justify-between rounded-md bg-slate-50 px-3 py-2">
              <div><div className="text-[13px] font-medium text-slate-700">启用 Provider</div><div className="text-[11px] text-slate-400">注册流程会使用该服务</div></div>
              <Switch checked={form.cf_temp_email.enabled} onChange={(value) => patch("cf_temp_email", "enabled", value)} />
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Select
                label="邮箱地址来源"
                options={[{ value: "generated", label: "CF 自动创建地址" }, { value: "custom_pool", label: "自定义邮箱池" }]}
                value={form.cf_temp_email.address_mode}
                onChange={(value) => patch("cf_temp_email", "address_mode", value)}
              />
              <Input label="API 地址" value={form.cf_temp_email.base_url} error={errors.base_url} onChange={(e) => patch("cf_temp_email", "base_url", e.target.value)} />
              <Input label="邮箱域名" value={form.cf_temp_email.domain} error={errors.domain} onChange={(e) => patch("cf_temp_email", "domain", e.target.value)} />
              <Input label="邮箱名前缀" value={form.cf_temp_email.name_prefix} onChange={(e) => patch("cf_temp_email", "name_prefix", e.target.value)} />
              <FormNumber label="随机后缀长度" value={form.cf_temp_email.random_length} error={errors.cf_random_length} onChange={(value) => patch("cf_temp_email", "random_length", value)} />
              <FormNumber label="轮询间隔（秒）" value={form.cf_temp_email.poll_interval} error={errors.cf_poll_interval} onChange={(value) => patch("cf_temp_email", "poll_interval", value)} />
              <FormNumber label="验证码超时（秒）" value={form.cf_temp_email.poll_timeout} error={errors.cf_poll_timeout} onChange={(value) => patch("cf_temp_email", "poll_timeout", value)} />
              <FormNumber label="最大重试次数" value={form.cf_temp_email.max_retries} error={errors.cf_max_retries} onChange={(value) => patch("cf_temp_email", "max_retries", value)} />
              <FormNumber label="429 退避（秒）" value={form.cf_temp_email.rate_limit_backoff} error={errors.cf_rate_limit_backoff} onChange={(value) => patch("cf_temp_email", "rate_limit_backoff", value)} />
            </div>
            {form.cf_temp_email.address_mode === "custom_pool" && (
              <div className="space-y-4 rounded-md border border-blue-100 bg-blue-50/40 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-2 text-[13px] font-medium text-slate-700"><ListChecks size={15} className="text-blue-600" />自定义邮箱池</div>
                    <div className="mt-1 text-[11px] text-slate-400">注册时从地址池取号，所有地址应转发到下方固定收件箱。</div>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Badge color={form.cf_temp_email.custom_pool_count ? "success" : "warning"}>{form.cf_temp_email.custom_pool_count} 个地址</Badge>
                  </div>
                </div>
                {form.cf_temp_email.custom_pool_count > 0 && (
                  <div className="space-y-2">
                    <div className="flex flex-wrap gap-1.5">
                      <Badge color={(poolSummary.unused ?? poolStatus.unused ?? 0) > 0 ? "success" : "danger"}>可用 {poolSummary.unused ?? poolStatus.unused ?? 0}</Badge>
                      <Badge color="info">使用中 {poolSummary.in_use ?? poolStatus.in_use ?? 0}</Badge>
                      <Badge color="neutral">已使用 {poolSummary.used ?? poolStatus.used ?? 0}（完整 {poolSummary.used_full ?? 0} / 仅AT {poolSummary.used_at_only ?? 0}）</Badge>
                      <Badge color="danger">失败 {poolSummary.failed ?? poolStatus.failed ?? 0}{(poolSummary.recyclable_failed ?? 0) > 0 ? `（可回收 ${poolSummary.recyclable_failed}）` : ""}</Badge>
                      {(poolSummary.disabled ?? poolStatus.disabled ?? 0) > 0 && <Badge color="neutral">已停用 {poolSummary.disabled ?? poolStatus.disabled}</Badge>}
                    </div>
                    {poolSummary.low_water && (
                      <div className="rounded-md bg-red-50 px-3 py-2 text-[11px] text-red-700">
                        低水位告警：可用地址仅 {poolSummary.unused ?? 0} 个（阈值 {poolSummary.low_water_threshold ?? 0}），请及时补充地址
                      </div>
                    )}
                  </div>
                )}
                {savedPoolItems.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-2">
                    <label className="flex items-center gap-1.5 text-xs text-slate-600">
                      <input type="checkbox" checked={selectedIds.length > 0 && selectedIds.length === savedPoolItems.length} onChange={toggleSelectAll} />
                      全选
                    </label>
                    <span className="text-xs text-slate-400">已选 {selectedIds.length}</span>
                    <div className="ml-auto flex flex-wrap items-center gap-1.5">
                      <Button variant="secondary" size="sm" disabled={!selectedIds.length || poolBusy} onClick={requeueSelected}>回收</Button>
                      <Button variant="secondary" size="sm" disabled={!selectedIds.length || poolBusy} onClick={() => runPoolAction("poolRelease", { ids: selectedIds, outcome: "unused" }, "释放")}>释放</Button>
                      <Button variant="secondary" size="sm" disabled={!selectedIds.length || poolBusy} onClick={() => runPoolAction("poolDisable", { ids: selectedIds }, "停用")}>停用</Button>
                      <Button variant="secondary" size="sm" disabled={!selectedIds.length || poolBusy} onClick={() => runPoolAction("poolEnable", { ids: selectedIds }, "启用")}>启用</Button>
                      <Button variant="secondary" size="sm" loading={poolBusy} onClick={() => runPoolAction("poolVerify", { ids: selectedIds }, "刷新凭据", { allowEmpty: true })}>刷新凭据</Button>
                      <Button variant="dangerSoft" size="sm" disabled={!selectedIds.length || poolBusy} onClick={() => removePoolItems(selectedIds)}>移除</Button>
                    </div>
                  </div>
                )}
                <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 bg-white px-3 py-2">
                  <div className="flex items-center gap-2 text-xs text-slate-600"><LockKeyhole size={14} className="text-slate-400" />已保存地址仅返回脱敏样例，避免凭证泄露</div>
                  <Button variant="secondary" size="sm" icon={<Upload size={13} />} onClick={() => { setPoolImportOpen(true); setPoolImportError(""); }}>批量导入</Button>
                </div>
                <div className="overflow-hidden rounded-md border border-slate-200 bg-white">
                  <div className="grid grid-cols-[1fr_auto] items-center border-b border-slate-100 bg-slate-50 px-3 py-2 text-[11px] font-medium text-slate-500"><span>邮箱地址</span><span>状态</span></div>
                  {savedPoolItems.length > 0 ? savedPoolItems.map((item) => (
                    <div key={item.id} className="flex items-center justify-between gap-3 border-b border-slate-100 px-3 py-2 last:border-0">
                      <label className="flex min-w-0 items-center gap-2">
                        <input type="checkbox" checked={selectedIds.includes(item.id)} onChange={() => togglePoolSelect(item.id)} />
                        <span className="truncate font-mono text-xs text-slate-600">{item.address}</span>
                      </label>
                      <div className="flex shrink-0 items-center gap-1.5">
                        {item.status === "used" && (
                          <Badge color={item.has_refresh_token ? "success" : "warning"}>{item.has_refresh_token ? "完整" : "仅AT"}</Badge>
                        )}
                        {item.status === "failed" && item.reason_code && (
                          <span title={item.last_error || ""}>
                            <Badge color={poolReasonMeta[item.reason_code]?.color || "neutral"}>{poolReasonMeta[item.reason_code]?.label || item.reason_code}</Badge>
                          </span>
                        )}
                        {item.status === "in_use" && item.lease_expires_at && (
                          <span className="text-[11px] text-slate-400">租约至 {fmtTime(item.lease_expires_at).slice(11, 16)}</span>
                        )}
                        <Badge color={poolStatusMeta[item.status]?.color || "neutral"} dot>{poolStatusMeta[item.status]?.label || item.status}</Badge>
                        <button type="button" title="从地址池移除" className="text-slate-400 hover:text-red-600" onClick={() => removePoolItems([item.id])}><Trash2 size={14} /></button>
                      </div>
                    </div>
                  )) : <div className="px-3 py-6 text-center text-xs text-slate-400">地址池为空，请批量导入邮箱地址</div>}
                </div>
                {form.cf_temp_email.custom_pool_count > 0 && <div className="text-[11px] text-slate-400">地址以脱敏形式展示；导入立即保存生效（追加不清空旧地址）。</div>}
                {errors.custom_pool && <span className="block text-[11px] text-red-600">{errors.custom_pool}</span>}
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Input label="固定收件 CF 邮箱" value={form.cf_temp_email.inbox_address} error={errors.inbox_address} placeholder="例如 jackson@708651.xyz" onChange={(e) => patch("cf_temp_email", "inbox_address", e.target.value)} />
                  <SecretInput
                    label="固定收件 JWT"
                    value={form.cf_temp_email.inbox_jwt}
                    configured={form.cf_temp_email.has_inbox_jwt}
                    error={errors.inbox_jwt}
                    visible={visible.inboxJwt}
                    onToggle={() => setVisible((state) => ({ ...state, inboxJwt: !state.inboxJwt }))}
                    onChange={(value) => patch("cf_temp_email", "inbox_jwt", value)}
                    hint="仅用于轮询固定 CF 收件箱，接口不会返回明文。"
                  />
                </div>
              </div>
            )}
            <SecretInput
              label="站点访问密码"
              value={form.cf_temp_email.site_password}
              configured={cfConfigured}
              visible={visible.sitePassword}
              onToggle={() => setVisible((state) => ({ ...state, sitePassword: !state.sitePassword }))}
              onChange={(value) => patch("cf_temp_email", "site_password", value)}
              hint="请求会通过 x-custom-auth 发送，接口不会返回明文。"
            />
          </div>
        </Panel>

        <Panel
          title={<span className="flex items-center gap-2"><AtSign size={15} className="text-violet-600" />outlook</span>}
          extra={form.provider === "outlook" && <Badge color="primary" dot>当前启用</Badge>}
        >
          <div className="space-y-4">
            <div className="flex items-center justify-between rounded-md bg-slate-50 px-3 py-2">
              <div><div className="text-[13px] font-medium text-slate-700">启用 Provider</div><div className="text-[11px] text-slate-400">第一阶段使用 manual_pool</div></div>
              <Switch checked={form.outlook.enabled} onChange={(value) => patch("outlook", "enabled", value)} />
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <Select label="收信模式" options={[{ value: "manual_pool", label: "manual_pool" }, { value: "imap", label: "imap（预留）" }, { value: "graph", label: "graph（预留）" }]} value={form.outlook.mode} onChange={(value) => patch("outlook", "mode", value)} />
              <div className="flex items-end pb-1 text-xs text-slate-500"><Users size={14} className="mr-1.5 text-slate-400" />{t(`已配置 ${form.outlook.accounts_count} 个账号`)}</div>
            </div>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-slate-600">账号池</span>
              <textarea
                className="input min-h-28 resize-y font-mono text-xs"
                value={form.outlook.accounts_pool}
                placeholder={form.outlook.accounts_count ? `已配置 ${form.outlook.accounts_count} 个账号；输入新内容可替换，留空不修改` : "每行一个：email,password 或 email----password"}
                onChange={(e) => patch("outlook", "accounts_pool", e.target.value)}
              />
              <span className="mt-1 block text-[11px] text-slate-400">保存和接口返回均不会回显密码。清空文本并保存可清除账号池。</span>
            </label>
            {form.outlook.accounts_sample?.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5"><span className="text-[11px] text-slate-400">样例：</span>{form.outlook.accounts_sample.map((sample) => <Badge key={sample} color="neutral">{sample}</Badge>)}</div>
            )}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <FormNumber label="轮询间隔（秒）" value={form.outlook.poll_interval} error={errors.out_poll_interval} onChange={(value) => patch("outlook", "poll_interval", value)} />
              <FormNumber label="验证码超时（秒）" value={form.outlook.poll_timeout} error={errors.out_poll_timeout} onChange={(value) => patch("outlook", "poll_timeout", value)} />
              <Input label="发件人过滤" value={form.outlook.sender_filter} onChange={(e) => patch("outlook", "sender_filter", e.target.value)} />
              <Input label="主题过滤" value={form.outlook.subject_filter} onChange={(e) => patch("outlook", "subject_filter", e.target.value)} />
            </div>
            <div className="border-t border-slate-100 pt-3">
              <div className="mb-3 flex items-center gap-1.5 text-xs font-medium text-slate-600"><ShieldCheck size={14} />IMAP / Graph 预留字段</div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <Input label="IMAP Host" value={form.outlook.imap_host} onChange={(e) => patch("outlook", "imap_host", e.target.value)} />
                <FormNumber label="IMAP Port" value={form.outlook.imap_port} error={errors.imap_port} onChange={(value) => patch("outlook", "imap_port", value)} />
                <Input label="Graph Tenant ID" value={form.outlook.graph_tenant_id} onChange={(e) => patch("outlook", "graph_tenant_id", e.target.value)} />
                <Input label="Graph Client ID" value={form.outlook.graph_client_id} onChange={(e) => patch("outlook", "graph_client_id", e.target.value)} />
                <div className="flex items-end pb-1"><Switch checked={form.outlook.imap_ssl} label="IMAP SSL" onChange={(value) => patch("outlook", "imap_ssl", value)} /></div>
                <SecretInput
                  label="Graph Client Secret"
                  value={form.outlook.graph_client_secret}
                  configured={graphConfigured}
                  visible={visible.graphSecret}
                  onToggle={() => setVisible((state) => ({ ...state, graphSecret: !state.graphSecret }))}
                  onChange={(value) => patch("outlook", "graph_client_secret", value)}
                />
              </div>
            </div>
          </div>
        </Panel>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-slate-200 pt-3">
        <Button variant="secondary" icon={testing ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} onClick={testConnection} disabled={testing}>
          {testing ? "测试中…" : "测试当前 Provider"}
        </Button>
        {lastTest && <span className={`flex items-center gap-1.5 text-xs ${lastTest.ok ? "text-emerald-600" : "text-red-600"}`}><KeyRound size={13} />{lastTest.message}</span>}
      </div>
      {poolImportOpen && (
        <div className="fixed inset-0 z-40 bg-slate-950/30" onMouseDown={(event) => { if (event.target === event.currentTarget) setPoolImportOpen(false); }}>
          <aside className="absolute right-0 top-0 flex h-full w-full max-w-md flex-col bg-white shadow-2xl">
            <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
              <div><div className="flex items-center gap-2 text-sm font-semibold text-slate-800"><ClipboardPaste size={16} className="text-blue-600" />批量导入邮箱</div><div className="mt-1 text-xs text-slate-400">支持换行、逗号或分号分隔，系统会自动去重。</div></div>
              <button type="button" title="关闭" className="text-slate-400 hover:text-slate-700" onClick={() => setPoolImportOpen(false)}><XCircle size={18} /></button>
            </div>
            <div className="flex-1 space-y-4 overflow-y-auto p-5">
              <div className="grid grid-cols-2 gap-2 rounded-md bg-slate-100 p-1">
                {[{ value: "append", label: "追加（保留旧地址）" }, { value: "replace", label: "替换全部地址" }].map((option) => <button key={option.value} type="button" onClick={() => setPoolImportMode(option.value)} className={`rounded px-2 py-1.5 text-xs font-medium ${poolImportMode === option.value ? "bg-white text-blue-700 shadow-sm" : "text-slate-500"}`}>{option.label}</button>)}
              </div>
              {poolImportMode === "append" && (form.cf_temp_email.custom_pool_count || 0) > 0 && (
                <div className="rounded-md bg-blue-50 px-3 py-2 text-xs text-blue-700">已保存 {form.cf_temp_email.custom_pool_count} 个地址；导入后直接追加到池尾并立即生效，旧地址保持不变。</div>
              )}
              <textarea autoFocus value={poolImport} onChange={(event) => { setPoolImport(event.target.value); setPoolImportError(""); }} className="input min-h-56 resize-y font-mono text-xs" placeholder="name@example.com\nsecond@example.com" />
              {poolImportError && <div className={`rounded-md px-3 py-2 text-xs ${poolImportError.includes("忽略") ? "bg-amber-50 text-amber-700" : "bg-red-50 text-red-700"}`}>{poolImportError}</div>}
              <div className="rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500"><div className="mb-2 flex items-center gap-2 font-medium text-slate-700"><ListChecks size={14} />导入规则</div><div>• 导入立即保存生效，无需再点保存</div><div>• 自动去重并统一转为小写</div><div>• 无效格式不会进入地址池</div><div>• 追加不会覆盖已保存地址，重复地址自动忽略</div><div>• 替换全部会从池中移除当前所有地址</div></div>
            </div>
            <div className="flex items-center justify-end gap-2 border-t border-slate-200 px-5 py-4"><Button variant="secondary" onClick={() => setPoolImportOpen(false)}>取消</Button><Button icon={<Upload size={14} />} loading={poolBusy} onClick={importPool}>导入并保存</Button></div>
          </aside>
        </div>
      )}
    </div>
  );
}
