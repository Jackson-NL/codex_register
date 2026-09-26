import { useMemo, useState } from "react";
import { Save, RotateCcw, CheckCircle2, XCircle, Loader2, ShieldCheck, Eye, EyeOff, RefreshCw } from "lucide-react";
import {
  Panel, Button, Input, Select, Switch, Badge, useAsyncData, Modal,
} from "../components/ui";
import { useApp } from "../context/AppContext";
import { api } from "../api";
import { defaultSettings, fmtTime } from "../mock/data";
import { LANGUAGE_OPTIONS } from "../i18n";

const GROUPS = [
  { key: "general", label: "通用" },
  { key: "refresh", label: "数据刷新" },
  { key: "sms", label: "接码设置" },
  { key: "sub2api", label: "Sub2API" },
  { key: "oauth", label: "OAuth 接入" },
  { key: "proxy", label: "代理策略" },
  { key: "notify", label: "通知" },
  { key: "retention", label: "数据保留" },
];

const FIELD_DEFS = {
  general: [
    { key: "instanceName", label: "实例名称", type: "text", desc: "显示在侧栏与导出报告中的实例标识" },
    { key: "timezone", label: "时区", type: "select", options: ["Asia/Shanghai", "UTC", "America/New_York", "Europe/Berlin"], desc: "所有时间展示与 cron 调度使用的时区" },
    { key: "language", label: "界面语言", type: "select", options: ["zh-CN", "en-US"], desc: "控制台界面语言" },
    { key: "registrationTag", label: "注册批次标签", type: "text", desc: "自动写入新账号的 tag 字段（如 post-fix-20260823），用于区分不同风控策略下注册的账号；留空=不打标签" },
  ],
  refresh: [
    { key: "autoRefresh", label: "自动刷新", type: "switch", desc: "开启后按固定间隔自动刷新页面数据" },
    { key: "interval", label: "刷新间隔（秒）", type: "number", min: 5, max: 600, desc: "自动刷新周期，建议 ≥ 30 秒" },
    { key: "maxAge", label: "数据最大时效（分钟）", type: "number", min: 1, max: 1440, desc: "超过时效的数据标记为过期" },
  ],
  oauth: [
    { key: "callbackUrl", label: "授权回调地址", type: "text", desc: "OAuth 授权码回调解析入口，需与 auth0 配置一致", locked: true },
    { key: "allowedOrigins", label: "允许来源", type: "text", desc: "逗号分隔的允许跨域来源，用于校验回调来源" },
    { key: "clientName", label: "客户端名称", type: "text", desc: "OAuth 应用的注册名称（只读）", locked: true },
  ],
  sms: [
    { key: "api_key", label: "单 Key（兜底）", type: "password", desc: "SMSBOWER 单把 API 密钥；Key 池为空时才使用" },
    { key: "base_url", label: "API 地址", type: "text", desc: "handler_api 端点地址" },
    { key: "service", label: "服务代码", type: "text", desc: "接码服务标识（如 dr）" },
    { key: "country", label: "国家代码", type: "number", min: 1, max: 999, desc: "国家数字代码（如 73=巴⻄）" },
    { key: "max_price", label: "最高单价（$）", type: "number", min: 0.001, max: 10, desc: "单条号码最高可接受价格" },
  ],
  sub2api: [
    { key: "base_url", label: "服务地址", type: "text", desc: "Sub2API 服务根地址，例如 https://sub2api.example" },
    { key: "admin_api_key", label: "管理员 API Key", type: "password", desc: "优先使用 x-api-key 认证" },
    { key: "jwt", label: "管理员 JWT", type: "password", desc: "未配置 API Key 时作为备用认证" },
    { key: "timeout", label: "请求超时（秒）", type: "number", min: 3, max: 120, desc: "获取分组和上传账号的单次请求超时" },
    { key: "proxy", label: "出口代理", type: "text", desc: "Sub2API 按出口 IP 地区准入，直连常返回 403 REGION_NOT_SUPPORTED。留空=沿用「默认代理」；填 direct 强制直连（自建/内网部署时用）" },
    { key: "group_ids", label: "默认上传分组 ID", type: "text", desc: "逗号分隔，可填多个分组 ID；账号管理上传时默认使用，也可在弹窗临时修改" },
  ],
  proxy: [
    { key: "testTimeout", label: "测试超时（秒）", type: "number", min: 3, max: 30, desc: "代理连通性测试的等待超时" },
    { key: "maxLatency", label: "最大可用延迟（ms）", type: "number", min: 200, max: 5000, desc: "超过此延迟的代理标记为降级" },
    { key: "minSuccessRate", label: "最低成功率（%）", type: "number", min: 1, max: 100, desc: "低于此成功率触发告警" },
    { key: "autoDisable", label: "自动禁用故障代理", type: "switch", desc: "连续失败后自动将代理移出可用池" },
  ],
  notify: [
    { key: "inApp", label: "站内通知", type: "switch", desc: "在通知中心展示告警与任务状态" },
    { key: "webhook", label: "Webhook 通知", type: "switch", desc: "通过 HTTP 回调推送事件" },
    { key: "webhookUrl", label: "Webhook 地址", type: "text", desc: "接收事件回调的 URL" },
    { key: "email", label: "邮件通知", type: "switch", desc: "发送告警邮件到以下收件人" },
    { key: "emailRecipients", label: "告警收件人", type: "text", desc: "逗号分隔的邮箱列表" },
    { key: "warnThreshold", label: "告警阈值（%）", type: "number", min: 1, max: 100, desc: "成功率低于此值触发 warning" },
    { key: "dangerThreshold", label: "严重阈值（%）", type: "number", min: 1, max: 100, desc: "成功率低于此值触发 danger" },
  ],
  retention: [
    { key: "logDays", label: "运行日志保留（天）", type: "number", min: 1, max: 365, desc: "任务与检查的原始日志保留时长" },
    { key: "exportDays", label: "导出记录保留（天）", type: "number", min: 1, max: 365, desc: "导出历史与报告文件的保留时长" },
    { key: "autoClean", label: "自动清理过期数据", type: "switch", desc: "按上述保留期定时清理" },
  ],
};

export default function Settings() {
  const { toast, t, setLanguage } = useApp();
  const { data: serverSettings } = useAsyncData(() => api.settings.getUi().catch(() => null));
  const { data: smsSettings, reload: reloadRuntimeSettings } = useAsyncData(() => api.settings.get().catch(() => null));
  const [form, setForm] = useState(null);
  const [group, setGroup] = useState("general");
  const [saving, setSaving] = useState(null);
  const [saved, setSaved] = useState({});
  const [errors, setErrors] = useState({});
  const [confirmReset, setConfirmReset] = useState(false);
  const [secretVisible, setSecretVisible] = useState(false);
  const [testingSms, setTestingSms] = useState(false);
  const [smsTestResult, setSmsTestResult] = useState(null);
  // Key 池：独立于 form（后端按 .env 持久化），留空表示不修改
  const [keyPoolInput, setKeyPoolInput] = useState("");
  const [keyStrategy, setKeyStrategy] = useState("");
  const [poolCleared, setPoolCleared] = useState(false);

  const keyPoolMasks = smsSettings?.smsbower_api_key_masks || [];
  const keyPoolCount = smsSettings?.smsbower_api_key_count ?? keyPoolMasks.length;
  const activeStrategy = keyStrategy || smsSettings?.smsbower_key_strategy || "round_robin";
  const STRATEGY_OPTIONS = [
    { value: "round_robin", label: "轮询（每租一个号取下一把）" },
    { value: "random", label: "随机（每租一个号随机取一把）" },
  ];
  const poolDirty = !!keyPoolInput.trim() || poolCleared
    || (!!keyStrategy && keyStrategy !== (smsSettings?.smsbower_key_strategy || "round_robin"));

  const defaultVal = useMemo(() => defaultSettings(), []);
  // 服务端返回的整组配置可能为空对象，需与默认值逐组合并，避免 current[group] 为 undefined
  const current = useMemo(() => {
    const base = form || serverSettings || {};
    const merged = { ...defaultVal };
    for (const g of Object.keys(defaultVal)) {
      merged[g] = { ...defaultVal[g], ...(base[g] || {}) };
    }
    // sms 组走后端配置（非 UI blob）
    if (smsSettings && !form?.sms) {
      merged.sms = {
        ...merged.sms,
        api_key: smsSettings.smsbower_has_api_key ? "••••••••" : "",
        base_url: smsSettings.smsbower_base_url,
        service: smsSettings.smsbower_service,
        country: smsSettings.smsbower_country,
        max_price: smsSettings.smsbower_max_price,
      };
    }
    if (smsSettings && !form?.sub2api) {
      merged.sub2api = {
        ...merged.sub2api,
        base_url: smsSettings.sub2api_base_url,
        admin_api_key: smsSettings.sub2api_has_admin_api_key ? "••••••••" : "",
        jwt: smsSettings.sub2api_has_jwt ? "••••••••" : "",
        timeout: smsSettings.sub2api_timeout,
        proxy: smsSettings.sub2api_proxy ?? merged.sub2api.proxy,
        group_ids: smsSettings.sub2api_group_ids ?? merged.sub2api.group_ids,
      };
    }
    // 注册批次标签走后端配置（.env 持久化）
    if (smsSettings && !form?.general) {
      merged.general = { ...merged.general, registrationTag: smsSettings.registration_tag ?? merged.general.registrationTag ?? "" };
    }
    return merged;
  }, [form, serverSettings, smsSettings, defaultVal]);
  const defs = FIELD_DEFS[group];

  const patch = (key, value) => setForm((f) => ({ ...(f || serverSettings || defaultVal), [group]: { ...current[group], [key]: value } }));

  const validateGroup = () => {
    const errs = {};
    defs.forEach((d) => {
      if (d.type === "number") {
        const v = Number(current[group][d.key]);
        if (Number.isNaN(v) || v < d.min || v > d.max) errs[d.key] = `取值范围 ${d.min} ~ ${d.max}`;
      }
      if (d.type === "text" && !d.locked && d.key === "webhookUrl" && current[group].webhook && !current[group].webhookUrl.trim()) {
        errs[d.key] = "启用 Webhook 后必须填写地址";
      }
    });
    setErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const save = async () => {
    if (!validateGroup()) { toast("存在校验错误，请修正后保存", "warning"); return; }
    setSaving(group);
    try {
      if (group === "sms") {
        const body = {
          smsbower_base_url: current.sms.base_url,
          smsbower_service: current.sms.service,
          smsbower_country: Number(current.sms.country),
          smsbower_max_price: Number(current.sms.max_price),
        };
        if (current.sms.api_key && current.sms.api_key !== "••••••••") {
          body.smsbower_api_key = current.sms.api_key;
        }
        // Key 池：输入内容追加到现有池；点击「清空」后提交空串=清空池
        if (poolCleared) body.smsbower_api_keys = "";
        else if (keyPoolInput.trim()) body.smsbower_api_keys_append = keyPoolInput;
        if (keyStrategy) body.smsbower_key_strategy = keyStrategy;
        await api.settings.put(body);
        setKeyPoolInput("");
        setPoolCleared(false);
        setKeyStrategy("");
        setSmsTestResult(null);
      } else if (group === "sub2api") {
        const body = {
          sub2api_base_url: current.sub2api.base_url,
          sub2api_timeout: Number(current.sub2api.timeout),
          sub2api_group_ids: String(current.sub2api.group_ids || "").trim(),
          sub2api_proxy: String(current.sub2api.proxy || "").trim(),
        };
        if (current.sub2api.admin_api_key && current.sub2api.admin_api_key !== "••••••••") {
          body.sub2api_admin_api_key = current.sub2api.admin_api_key;
        }
        if (current.sub2api.jwt && current.sub2api.jwt !== "••••••••") {
          body.sub2api_jwt = current.sub2api.jwt;
        }
        await api.settings.put(body);
      } else {
        await api.settings.putUi(current);
        if (group === "general") {
          setLanguage(current.general.language);
          // 注册批次标签持久化到后端 .env，注册时自动写入账号 tag
          await api.settings.put({ registration_tag: String(current.general.registrationTag ?? "").trim() });
        }
      }
      setSaving(null);
      setSaved((s) => ({ ...s, [group]: Date.now() }));
      setForm(null); // reset form diff after save
      if (group === "sms" || group === "sub2api") reloadRuntimeSettings();
      toast(t("设置已保存"), "success", { detail: `${t("分组")}「${t(GROUPS.find((g) => g.key === group)?.label)}」${t("保存成功")}` });
    } catch (e) {
      setSaving(null);
      toast(`保存失败: ${e.message}`, "error");
    }
  };

  const dirty = useMemo(() => {
    if (!form) return poolDirty;
    if (group === "sms") {
      const base = smsSettings ? {
        api_key: smsSettings.smsbower_has_api_key ? "••••••••" : "",
        base_url: smsSettings.smsbower_base_url,
        service: smsSettings.smsbower_service,
        country: smsSettings.smsbower_country,
        max_price: smsSettings.smsbower_max_price,
      } : defaultVal.sms;
      return JSON.stringify(form.sms) !== JSON.stringify(base) || poolDirty;
    }
    if (group === "sub2api") {
      const base = smsSettings ? {
        base_url: smsSettings.sub2api_base_url,
        admin_api_key: smsSettings.sub2api_has_admin_api_key ? "••••••••" : "",
        jwt: smsSettings.sub2api_has_jwt ? "••••••••" : "",
        timeout: smsSettings.sub2api_timeout,
        proxy: smsSettings.sub2api_proxy ?? "",
        group_ids: smsSettings.sub2api_group_ids ?? "",
      } : defaultVal.sub2api;
      return JSON.stringify(form.sub2api) !== JSON.stringify(base);
    }
    return JSON.stringify(form[group]) !== JSON.stringify(serverSettings?.[group] || defaultVal[group]);
  }, [form, serverSettings, smsSettings, group, defaultVal, poolDirty]);

  const testSmsbower = async () => {
    setTestingSms(true);
    setSmsTestResult(null);
    try {
      const res = await api.settings.testSmsbower();
      setSmsTestResult(res.ok
        ? {
          type: "success",
          text: `连接成功 · ${res.key_count || 1} 把 Key 可用 ${res.usable_count ?? 1} 把 · 余额合计 $${res.balance}`,
          results: res.results || [],
        }
        : { type: "error", text: `连接失败: ${res.error}`, results: res.results || [] });
      reloadRuntimeSettings();
    } catch (e) {
      setSmsTestResult({ type: "error", text: `测试失败: ${e.message}` });
    } finally {
      setTestingSms(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-[16px] font-semibold text-slate-800">{t("系统设置")}</h2>
          <div className="mt-0.5 text-xs text-slate-400">{t("实例级配置 · 修改保存后全局生效")}</div>
        </div>
        <Button variant="secondary" icon={<RotateCcw size={13} />} onClick={() => setConfirmReset(true)}>恢复默认值</Button>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        {/* 分组导航 */}
        <div className="panel h-fit p-2 lg:col-span-1">
          {GROUPS.map((g) => (
            <button key={g.key} onClick={() => setGroup(g.key)}
              className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-[13px] transition-colors ${group === g.key ? "bg-blue-50 font-medium text-blue-700" : "text-slate-600 hover:bg-slate-50"}`}>
              <span>{t(g.label)}</span>
              {saved[g.key] && <CheckCircle2 size={13} className="text-emerald-500" />}
            </button>
          ))}
          <div className="mt-2 border-t border-slate-100 px-3 py-2 text-[11px] text-slate-400">
            {saved[group] ? `${t("上次保存")} ${fmtTime(saved[group])}` : t("本组暂无保存记录")}
          </div>
        </div>

        {/* 设置内容 */}
        <div className="lg:col-span-3">
          <Panel title={GROUPS.find((g) => g.key === group)?.label}
            extra={
              <div className="flex items-center gap-2">
                {dirty && <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-700">{t("有未保存的修改")}</span>}
                <Button size="sm" icon={saving === group ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />} onClick={save} disabled={saving === group}>
                  {saving === group ? "保存中…" : "保存"}
                </Button>
              </div>
            }>
            <div className="space-y-4">
              {group === "oauth" && (
                <div className="rounded-md border border-slate-200">
                  <div className="flex items-center justify-between border-b border-slate-100 bg-slate-50/60 px-3 py-2.5">
                    <span className="flex items-center gap-1.5 text-xs font-medium text-slate-600"><ShieldCheck size={14} />{t("连接状态")}</span>
                    <Badge color={current.oauth.connected ? "success" : "danger"} dot>{current.oauth.connected ? "已连接" : "未连接"}</Badge>
                  </div>
                  <div className="px-3 py-2.5 text-[11px] text-slate-400">
                    {t("客户端密钥不会在此展示。如需轮换密钥，请在 OAuth 提供商侧操作并同步更新后端环境变量。")}
                  </div>
                </div>
              )}

              {defs.map((d) => (
                <div key={d.key} className="flex items-start justify-between gap-6">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-[13px] font-medium text-slate-700">
                      {t(d.label)}
                      {d.locked && <Badge color="neutral">只读</Badge>}
                    </div>
                      <div className="mt-0.5 text-xs leading-relaxed text-slate-400">{t(d.desc)}</div>
                    {errors[d.key] && <div className="mt-1 text-[11px] text-red-600">{t(errors[d.key])}</div>}
                  </div>
                  <div className="w-56 shrink-0">
                    {d.type === "switch" ? (
                      <div className="flex justify-end pt-1"><Switch checked={!!current[group][d.key]} onChange={(v) => patch(d.key, v)} /></div>
                    ) : d.type === "select" ? (
                      <Select options={d.key === "language" ? LANGUAGE_OPTIONS : d.options.map((o) => ({ value: o, label: o }))} value={current[group][d.key]} onChange={(v) => patch(d.key, v)} />
                    ) : d.key === "webhookUrl" && !current.notify.webhook ? (
                      <Input value="" disabled placeholder="启用 Webhook 后可编辑" />
                    ) : d.key === "callbackUrl" || d.key === "clientName" ? (
                      <div className="relative">
                        <Input value={d.key === "callbackUrl" ? (secretVisible ? current.oauth.callbackUrl : "https://ops.exa***.com/api/auth/callback") : current.oauth.clientName} disabled />
                        {d.key === "callbackUrl" && (
                          <button onClick={() => setSecretVisible(!secretVisible)} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
                            {secretVisible ? <EyeOff size={14} /> : <Eye size={14} />}
                          </button>
                        )}
                      </div>
                    ) : d.type === "password" ? (
                      <div className="relative">
                        <Input type={secretVisible ? "text" : "password"} value={current[group][d.key]}
                          placeholder={current[group][d.key] === "••••••••" ? "已配置（留空不修改）" : ""}
                          onChange={(e) => patch(d.key, e.target.value)} />
                        <button onClick={() => setSecretVisible(!secretVisible)}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
                          {secretVisible ? <EyeOff size={14} /> : <Eye size={14} />}
                        </button>
                      </div>
                    ) : (
                      <Input type={d.type} value={current[group][d.key]} min={d.min} max={d.max}
                        onChange={(e) => patch(d.key, d.type === "number" ? Number(e.target.value) : e.target.value)} />
                    )}
                  </div>
                </div>
              ))}

              {group === "notify" && (
                <div className="flex items-center gap-2 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-500">
                  <RefreshCw size={12} /> 告警阈值用于健康检查通过率触发站内/Webhook/邮件通知。
                </div>
              )}
              {group === "sms" && (
                <div className="space-y-3 rounded-md border border-slate-200 p-3">
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
                      <ShieldCheck size={14} />API Key 池（多 Key 轮询）
                    </span>
                    <Badge color={keyPoolCount > 0 ? "success" : "neutral"} dot>
                      {keyPoolCount > 0 ? `${keyPoolCount} 把可用` : "未配置"}
                    </Badge>
                  </div>

                  {keyPoolCount > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {keyPoolMasks.map((item, index) => (
                        <span key={`${item}-${index}`} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500">
                          #{index + 1} {item}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="flex items-start justify-between gap-6">
                    <div className="min-w-0 flex-1">
                      <div className="text-[13px] font-medium text-slate-700">轮询策略</div>
                      <div className="mt-0.5 text-xs leading-relaxed text-slate-400">
                        每新建一个订单（取号 / 租 Gmail）就从池里取一把 Key，所以同一个任务连续租号也会分摊到多把 Key。订单的查码、取消会精确复用创建它的那把 Key（SMSBower 的订单归属其创建 Key，换 Key 会查不到订单）。
                      </div>
                    </div>
                    <div className="w-56 shrink-0">
                      <Select options={STRATEGY_OPTIONS} value={activeStrategy} onChange={setKeyStrategy} />
                    </div>
                  </div>

                  <div className="flex items-start justify-between gap-6">
                    <div className="min-w-0 flex-1">
                      <div className="text-[13px] font-medium text-slate-700">追加 Key</div>
                      <div className="mt-0.5 text-xs leading-relaxed text-slate-400">
                        每行一把，也支持逗号、分号分隔。保存后追加到现有 Key 池，重复 Key 会自动去重；留空保存=保持不变。
                      </div>
                      {poolCleared && (
                        <div className="mt-1 text-[11px] text-red-600">已标记清空：保存后 Key 池将被清空</div>
                      )}
                    </div>
                    <div className="w-56 shrink-0 space-y-2">
                      <textarea
                        className="input h-24 resize-y font-mono text-[12px]"
                        placeholder={keyPoolCount > 0 ? "已配置 Key 池（留空不修改）" : "每行一把 Key"}
                        value={keyPoolInput}
                        onChange={(e) => { setKeyPoolInput(e.target.value); if (e.target.value.trim()) setPoolCleared(false); }}
                      />
                      <button
                        className="text-[11px] text-slate-400 hover:text-red-600"
                        onClick={() => { setKeyPoolInput(""); setPoolCleared(true); }}
                      >
                        清空 Key 池
                      </button>
                    </div>
                  </div>

                  <div className="flex items-center gap-3 border-t border-slate-100 pt-3">
                    <Button variant="secondary" size="sm" icon={<RefreshCw size={12} />} onClick={testSmsbower} disabled={testingSms}>
                      {testingSms ? "测试中…" : "测试全部 Key"}
                    </Button>
                    {smsTestResult && (
                      <span className={`text-xs ${smsTestResult.type === "success" ? "text-emerald-600" : "text-red-600"}`}>
                        {smsTestResult.text}
                      </span>
                    )}
                    {smsTestResult?.type === "success" && <Badge color="success" dot>已连接</Badge>}
                  </div>

                  {smsTestResult?.results?.length > 0 && (
                    <div className="space-y-1 rounded bg-slate-50 px-3 py-2">
                      {smsTestResult.results.map((item) => (
                        <div key={item.slot} className="flex items-center gap-2 text-[11px]">
                          <span className="font-mono text-slate-500">#{item.slot} {item.mask}</span>
                          {item.ok
                            ? <span className="text-emerald-600">余额 ${item.balance}</span>
                            : <span className="text-red-600">{item.error || "不可用"}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </Panel>
        </div>
      </div>

      <Modal open={confirmReset} onClose={() => setConfirmReset(false)} title="恢复默认值" width={420}
        footer={<><Button variant="secondary" onClick={() => setConfirmReset(false)}>取消</Button><Button variant="danger" onClick={() => { setForm({ ...defaultVal }); setConfirmReset(false); toast("已恢复默认值，请确认保存", "info"); }}>恢复</Button></>}>
        <p className="text-[13px] leading-relaxed text-slate-600">{t("确定将当前分组「")}{t(GROUPS.find((g) => g.key === group)?.label)}{t("」的所有设置恢复为默认值？修改将在点击保存后生效。")}</p>
      </Modal>
    </div>
  );
}
