import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Users, UserCheck, Activity, KeyRound, Mail, ArrowUpRight, Plus, CalendarDays,
} from "lucide-react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell, BarChart, Bar,
} from "recharts";
import { Panel, StatCard, Badge, Button, useAsyncData, Empty } from "../components/ui";
import { useApp } from "../context/AppContext";
import { api } from "../api";
import { fmtTime, fmtAgo } from "../mock/data";
import { accStatus, isHealthy } from "./accountsShared";

const PIE_COLORS = { normal: "#208d72", abnormal: "#d76e73" };
const TASK_BADGE = {
  success: ["success", "已完成"],
  failed: ["danger", "失败"],
  running: ["info", "进行中"],
  pending: ["neutral", "待执行"],
  done: ["success", "已完成"],
};

// 失败原因归类：从注册任务 error 字段解析出可读分类
const FAIL_PATTERNS = [
  { re: /验证码|otp|code/i, label: "验证码" },
  { re: /风控|Cloudflare|挑战|challenge|turnstile/i, label: "风控拦截" },
  { re: /代理|proxy|不可达|连接/i, label: "代理失败" },
  { re: /浏览器|browser|Camoufox|页面/i, label: "浏览器异常" },
  { re: /token|令牌|oauth/i, label: "Token 提取" },
  { re: /2FA|totp|TOTP/i, label: "2FA 绑定" },
  { re: /邮箱|email|Gmail|gmail/i, label: "邮箱问题" },
  { re: /重试|超时|timeout/i, label: "超时重试" },
];

function classifyError(err = "") {
  if (!err) return "未知原因";
  for (const p of FAIL_PATTERNS) {
    if (p.re.test(err)) return p.label;
  }
  return "其他失败";
}

function buildTrend(regs) {
  const days = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date(Date.now() - i * 86400e3);
    days.push({ date: `${d.getMonth() + 1}/${d.getDate()}`, success: 0, failed: 0, ts: d.getTime() });
  }
  (regs || []).forEach((r) => {
    const ts = r.finished_at ? new Date(r.finished_at).getTime() : r.created_at ? new Date(r.created_at).getTime() : 0;
    if (!ts) return;
    const day = days.find((x) => x.ts <= ts && ts < x.ts + 86400e3);
    if (!day) return;
    if (r.status === "success") day.success += 1;
    else if (r.status === "failed") day.failed += 1;
  });
  return days;
}

export default function Dashboard() {
  const { gotoWithFilter, t } = useApp();
  const navigate = useNavigate();

  const { data: stats } = useAsyncData(() => api.stats.get());
  const { data: registrations } = useAsyncData(() => api.registrations.list({ limit: 200 }));
  // 异常账号清单：后端 `?status=abnormal` 只能按 status 字段粗筛，会漏掉
  // 「status=active 但 AT 已过期」的账号（实测 1387 个），所以这里拉一批后
  // 在前端按 health 精确过滤，与账号页徽标、饼图口径完全一致。
  const { data: accountPool } = useAsyncData(() => api.accounts.list({ limit: 500 }));
  const abnormalAccounts = useMemo(
    () => (accountPool || []).filter((a) => !isHealthy(a)),
    [accountPool],
  );

  const trend = useMemo(() => buildTrend(registrations), [registrations]);

  const failTop5 = useMemo(() => {
    const m = {};
    (registrations || []).filter((r) => r.status === "failed").forEach((r) => {
      const k = classifyError(r.error);
      m[k] = (m[k] || 0) + 1;
    });
    return Object.entries(m).map(([name, value]) => ({ name, value })).sort((a, b) => b.value - a.value).slice(0, 5);
  }, [registrations]);

  const riskEvents = useMemo(() => {
    const ev = [];
    (registrations || []).filter((r) => r.status === "failed").slice(0, 4).forEach((r) => {
      ev.push({ level: "danger", text: `${t("注册任务")} reg_${r.id} ${t("失败")}: ${t(classifyError(r.error))}`, time: r.finished_at || r.created_at });
    });
    (registrations || []).filter((r) => r.status === "running" || r.status === "pending").slice(0, 3).forEach((r) => {
      ev.push({ level: "info", text: `${t("注册任务")} reg_${r.id} ${t(r.status === "running" ? "执行中" : "排队中")}`, time: r.created_at });
    });
    return ev.slice(0, 6);
  }, [registrations, t]);

  const statMetrics = useMemo(() => {
    if (!stats) return null;
    return {
      // 账号状态只有两态：正常（active/cooling）与异常（其余全部）。
      available: stats.normal_accounts ?? 0,
      abnormal: stats.abnormal_accounts ?? 0,
      todaySuccess: stats.today_success ?? 0,
      todayFailed: stats.today_failed ?? 0,
      passRate: stats.pass_rate ?? 0,
      totpCoverage: stats.totp_coverage ?? 0,
      gmailActive: stats.gmail_active_sessions ?? 0,
      gmailExpired: stats.gmail_expired_sessions ?? 0,
      proxyFailed: stats.proxy_failed ?? 0,
    };
  }, [stats]);

  const healthDist = useMemo(() => {
    if (!stats) return [];
    const normal = stats.normal_accounts ?? 0;
    // 兜底：后端异常时不要用减法造出虚假数字，直接用上报的 abnormal_accounts。
    const abnormal = stats.abnormal_accounts ?? 0;
    return [
      { name: t("正常"), key: "normal", value: Math.max(0, normal) },
      { name: t("异常"), key: "abnormal", value: Math.max(0, abnormal) },
    ].filter((x) => x.value > 0);
  }, [stats, t]);

  const recentTasks = (registrations || []).slice(0, 6);

  const taskStatusBadge = (status) => {
    const meta = TASK_BADGE[status] || TASK_BADGE.pending;
    return <Badge color={meta[0]} dot>{t(meta[1])}</Badge>;
  };

  return (
    <div className="space-y-4">
      <div className="ops-dashboard-intro">
        <div><div className="ops-dashboard-kicker"><span className="ops-status-dot" /> WORKSPACE OVERVIEW</div><h1>{t("工作空间概览")}</h1><p>{t("从账号状态到任务进展，所有关键动态尽在掌握。")}</p></div>
        <div><div className="ops-dashboard-actions"><Button variant="secondary" icon={<Users size={15} />} onClick={() => navigate("/accounts")}>账号管理</Button><Button icon={<Plus size={15} />} onClick={() => navigate("/register")}>新建注册任务</Button></div><div className="ops-dashboard-date"><CalendarDays size={12} className="mr-1.5 inline" />{new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric", weekday: "short" })}</div></div>
      </div>
      {/* 指标 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="正常 / 异常账号" value={statMetrics ? `${statMetrics.available} / ${statMetrics.abnormal}` : "—"} sub="账号状态只有这两态"
          icon={<Users size={17} />} onClick={() => gotoWithFilter("/accounts", { health: "abnormal" })} />
        <StatCard label="今日注册成功" value={statMetrics ? statMetrics.todaySuccess : "—"} sub={statMetrics ? t(`失败 ${statMetrics.todayFailed} · 成功率 ${statMetrics.passRate}%`) : ""}
          icon={<UserCheck size={17} />} onClick={() => gotoWithFilter("/register", {})} />
        <StatCard label="2FA 覆盖率" value={statMetrics ? `${statMetrics.totpCoverage}%` : "—"} sub="已绑定 TOTP 的账号占比"
          icon={<KeyRound size={17} />} onClick={() => gotoWithFilter("/accounts", {})} />
        <StatCard label="Gmail 会话" value={statMetrics ? statMetrics.gmailActive : "—"} sub={statMetrics ? t(`活跃 ${statMetrics.gmailActive} · 过期 ${statMetrics.gmailExpired}`) : ""}
          icon={<Mail size={17} />} onClick={() => gotoWithFilter("/register", { gmail: true })} />
      </div>

      {/* 中部图表 */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title="近 14 天注册趋势" className="xl:col-span-2"
          extra={<span className="flex items-center gap-2 text-xs text-slate-400"><Activity size={13} />按注册任务统计</span>}>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={trend} margin={{ top: 6, right: 8, left: -16, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef0f4" vertical={false} />
                <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#9aa3b2" }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 11, fill: "#9aa3b2" }} axisLine={false} tickLine={false} allowDecimals={false} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6, border: "1px solid #e5e8ee" }} />
                <Line isAnimationActive={false} type="monotone" dataKey="success" name={t("成功")} stroke="#208d72" strokeWidth={2} dot={false} />
                <Line isAnimationActive={false} type="monotone" dataKey="failed" name={t("失败")} stroke="#d76e73" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Panel>
        <Panel title="账号健康状态分布"
          extra={<Button variant="ghost" size="sm" onClick={() => gotoWithFilter("/accounts", {})}>查看全部<ArrowUpRight size={13} /></Button>}>
          <div className="relative h-44">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie isAnimationActive={false} data={healthDist} dataKey="value" nameKey="name" innerRadius={48} outerRadius={68} paddingAngle={2} stroke="var(--panel)"
                  onClick={(e) => gotoWithFilter("/accounts", { health: e.key })} style={{ cursor: "pointer" }}>
                  {healthDist.map((e) => <Cell key={e.key} fill={PIE_COLORS[e.key] || "#94a3b8"} />)}
                </Pie>
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6, border: "1px solid #e5e8ee" }} />
              </PieChart>
            </ResponsiveContainer>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <div className="tnum text-xl font-semibold text-slate-800">{stats?.total_accounts || 0}</div>
              <div className="text-[11px] text-slate-400">账号总数</div>
            </div>
          </div>
          <div className="mt-2 space-y-1.5">
            {healthDist.map((e) => (
              <div key={e.key} className="flex items-center gap-2 text-xs">
                <span className="h-2 w-2 rounded-full" style={{ background: PIE_COLORS[e.key] || "#94a3b8" }} />
                <span className="text-slate-500">{e.name}</span>
                <span className="tnum ml-auto font-medium text-slate-700">{e.value}</span>
              </div>
            ))}
          </div>
        </Panel>
      </div>

      {/* 失败原因 + 代理状态 */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Panel title="注册失败原因 Top 5">
          {failTop5.length === 0 ? <Empty title="近 200 条任务无失败" /> : (
            <div className="h-44">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={failTop5} layout="vertical" margin={{ top: 0, right: 16, left: 8, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef0f4" horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 11, fill: "#9aa3b2" }} axisLine={false} tickLine={false} allowDecimals={false} />
                  <YAxis type="category" dataKey="name" width={90} tick={{ fontSize: 11, fill: "#5b6472" }} axisLine={false} tickLine={false} />
                  <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6, border: "1px solid #e5e8ee" }} />
                  <Bar isAnimationActive={false} dataKey="value" name="次数" fill="#d76e73" radius={[0, 4, 4, 0]} barSize={14} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>
        <Panel title="代理池健康"
          extra={<Button variant="ghost" size="sm" onClick={() => gotoWithFilter("/proxies", {})}>管理 →</Button>}>
          <div className="grid grid-cols-3 gap-3">
            {[
              { label: "在线", value: stats?.proxy_online ?? "—", color: "text-emerald-600" },
              { label: "离线/失败", value: stats?.proxy_failed ?? "—", color: "text-red-600" },
              { label: "总数", value: stats?.proxy_total ?? "—", color: "text-slate-700" },
            ].map((s) => (
              <div key={s.label} className="rounded-md bg-slate-50 px-3 py-4 text-center">
                <div className={`tnum text-xl font-semibold ${s.color}`}>{s.value}</div>
                <div className="mt-0.5 text-[11px] text-slate-400">{s.label}</div>
              </div>
            ))}
          </div>
          <div className="mt-3 rounded-md border border-slate-100 px-3 py-2 text-xs text-slate-500">
            {t(`今日注册成功 ${stats?.today_success ?? 0} · 失败 ${stats?.today_failed ?? 0} · 成功率 ${stats?.pass_rate ?? 0}%`)}
          </div>
        </Panel>
      </div>

      {/* 下部三栏 */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Panel title="最新注册任务" className="xl:col-span-1"
          extra={<Link to="/register" className="text-xs text-blue-600 hover:text-blue-700">全部任务 →</Link>}>
          <div className="-mx-4 -my-4">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50/70 text-left text-xs text-slate-500">
                  <th className="px-4 py-2 font-medium">任务 ID</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">时间</th>
                </tr>
              </thead>
              <tbody>
                {recentTasks.length === 0 && (
                  <tr><td colSpan={3} className="px-4 py-4 text-center text-xs text-slate-400">暂无注册任务，去注册工作台发起</td></tr>
                )}
                {recentTasks.map((t) => (
                  <tr key={t.id} className="cursor-pointer border-b border-slate-100 hover:bg-slate-50" onClick={() => navigate("/register")}>
                    <td className="mono px-4 py-2 text-slate-700">reg_{t.id}</td>
                    <td className="px-3 py-2">{taskStatusBadge(t.status)}</td>
                    <td className="px-3 py-2 text-xs text-slate-400">{t.created_at ? fmtAgo(new Date(t.created_at).getTime()) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>

        <Panel title="风险 / 异常事件" className="xl:col-span-1">
          {riskEvents.length === 0 ? (
            <Empty title="暂无风险事件" />
          ) : (
            <div className="px-4 py-3">
              <div>
                {riskEvents.map((e, i) => (
                  <div key={i} className="relative flex gap-3 pb-4 last:pb-0">
                    {i < riskEvents.length - 1 && <span className="absolute left-[5px] top-4 h-full w-px bg-slate-100" />}
                    <span className={`relative mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${e.level === "danger" ? "bg-red-500" : e.level === "warning" ? "bg-amber-500" : e.level === "success" ? "bg-emerald-500" : "bg-blue-500"}`} />
                    <div className="min-w-0">
                      <div className="text-[12.5px] text-slate-700">{e.text}</div>
                      <div className="mt-0.5 text-[11px] text-slate-400">{e.time ? fmtTime(new Date(e.time).getTime()) : "—"}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Panel>

        <Panel title="需要处理的账号" className="xl:col-span-1"
          extra={<Button variant="ghost" size="sm" onClick={() => gotoWithFilter("/accounts", { health: "abnormal" })}>全部 →</Button>}>
          <div className="-mx-4 -my-4">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50/70 text-left text-xs text-slate-500">
                  <th className="px-4 py-2 font-medium">账号</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {(!abnormalAccounts || abnormalAccounts.length === 0) ? (
                  <tr><td colSpan={3} className="px-4 py-4 text-center text-xs text-slate-400">暂无异常账号</td></tr>
                ) : (
                  abnormalAccounts.slice(0, 6).map((a) => (
                    <tr key={a.id} className="cursor-pointer border-b border-slate-100 hover:bg-slate-50" onClick={() => gotoWithFilter("/accounts", { q: String(a.id) })}>
                      <td className="mono px-4 py-2 text-slate-700">acc_{a.id}<div className="text-[11px] text-slate-400">{a.email || a.phone || "—"}</div></td>
                      <td className="px-3 py-2"><Badge color={accStatus(a)[0]} dot>{accStatus(a)[1]}</Badge></td>
                      <td className="px-3 py-2">
                        <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); gotoWithFilter("/accounts", { q: String(a.id) }); }}>查看</Button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </Panel>
      </div>
    </div>
  );
}
