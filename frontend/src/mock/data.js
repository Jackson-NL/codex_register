// ============================================================
// 模拟数据层：为全部 8 个页面提供可操作的演示数据
// ============================================================

export const PLANS = ["free", "plus", "pro", "team", "enterprise"];
export const SOURCES = ["oauth", "json", "jsonl", "manual"];
export const HEALTH = ["healthy", "warning", "unhealthy", "unknown"];

export const HEALTH_META = {
  healthy: { label: "健康", color: "success" },
  warning: { label: "告警", color: "warning" },
  unhealthy: { label: "异常", color: "danger" },
  unknown: { label: "未知", color: "neutral" },
};

export const CRED_META = {
  active: { label: "有效", color: "success" },
  expiring: { label: "即将过期", color: "warning" },
  expired: { label: "已过期", color: "danger" },
  revoked: { label: "已吊销", color: "danger" },
  pending: { label: "待刷新", color: "neutral" },
};

const first = ["lin", "chen", "wang", "zhou", "xu", "sun", "ma", "zhao", "wu", "zheng"];
const last = ["yang", "li", "qian", "hao", "lei", "ning", "tian", "fan", "geng", "ren"];
const d = (n) => new Date(Date.now() - n * 86400e3);

export function rand(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}
export function pick(arr) {
  return arr[rand(0, arr.length - 1)];
}
export function uid(prefix = "id") {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}${Math.random().toString(36).slice(2, 6)}`;
}
export function fmtTime(ts) {
  const t = new Date(ts);
  if (Number.isNaN(t.getTime())) return "—";
  const p = (x) => String(x).padStart(2, "0");
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
}
export function fmtAgo(ts) {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  let language = "zh-CN";
  try { language = localStorage.getItem("accountops-language") || language; } catch { /* storage unavailable */ }
  if (language === "en-US") {
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}
export function maskEmail(email) {
  const [name, domain] = email.split("@");
  return `${name.slice(0, 3)}***@${domain}`;
}
export function maskToken(t) {
  return t ? `${t.slice(0, 12)}••••••••${t.slice(-6)}` : "—";
}

// ---------------- 账号 ----------------
export function genAccounts(n = 48) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const plan = pick(PLANS);
    const health = pick(HEALTH);
    const cred = pick(Object.keys(CRED_META));
    const source = pick(SOURCES);
    const created = d(rand(1, 60));
    const lastCheck = Date.now() - rand(5, 720) * 3600e3;
    const user = `${pick(first)}${pick(last)}`;
    out.push({
      id: `acc_${String(i + 1).padStart(4, "0")}`,
      email: `${user}${rand(10, 99)}@${pick(["outlook.com", "gmail.com", "icloud.com", "qq.com", "163.com"])}`,
      source,
      plan,
      credentialStatus: cred,
      health,
      healthScore: health === "healthy" ? rand(88, 100) : health === "warning" ? rand(65, 87) : health === "unhealthy" ? rand(20, 64) : 0,
      proxyId: rand(0, 4) === 0 ? "" : `prx_${String(rand(1, 12)).padStart(3, "0")}`,
      lastCheck,
      createdAt: created.getTime(),
      labels: pick([
        ["巴西", "主池"],
        ["美国", "备用"],
        ["新加坡"],
        ["主池", "验证过"],
        ["备用", "低优先级"],
        [],
      ]),
      accessTokenExpiry: Date.now() + rand(-5, 25) * 86400e3,
      refreshTokenStatus: pick(["ok", "ok", "ok", "expired", "missing"]),
      lastRefreshResult: pick(["成功 (200)", "成功 (200)", "失败: invalid_grant", "成功 (200)", "—"]),
      usageCount: rand(0, 340),
      note: "",
    });
  }
  return out;
}

// ---------------- 接入任务 ----------------
export function genTasks(n = 14) {
  const creators = ["admin", "ops_bot", "li.wang", "system"];
  const out = [];
  for (let i = 0; i < n; i++) {
    const total = rand(10, 320);
    const failed = rand(0, Math.round(total * 0.22));
    const running = rand(0, 3);
    const success = total - failed - running;
    const status = i === 0 ? "running" : pick(["done", "done", "done", "failed", "canceled", "paused"]);
    const created = Date.now() - rand(20, 7200) * 60e3;
    out.push({
      id: `task_${String(1000 + i)}`,
      source: pick(["oauth", "json", "jsonl", "manual"]),
      total,
      success,
      failed,
      running,
      creator: pick(creators),
      createdAt: created,
      duration: status === "running" ? rand(2, 40) : rand(3, 180),
      status,
      name: pick(["批量导入 batch-0716", "OAuth 授权回调接入", "JSONL 全量导入", "手工录入 32 个", "重试失败批次 A", "增量导入 daily"]),
      records: [],
      failReasons: [],
    });
  }
  const t0 = out[0];
  t0.records = Array.from({ length: 24 }, (_, k) => ({
    email: `user${rand(10, 999)}@${pick(["outlook.com", "gmail.com"])}`,
    result: k < 16 ? "success" : "failed",
    error: k < 16 ? "" : pick(["邮箱已注册", "验证码超时", "Cloudflare 挑战失败", "代理不可达"]),
    time: Date.now() - k * 9000,
  }));
  t0.failReasons = [
    { reason: "邮箱已注册", count: 4 },
    { reason: "验证码超时", count: 2 },
    { reason: "代理不可达", count: 1 },
    { reason: "Cloudflare 挑战失败", count: 1 },
  ];
  const t1 = out[1];
  t1.records = Array.from({ length: 18 }, (_, k) => ({
    email: `alice${rand(10, 999)}@gmail.com`,
    result: k < 17 ? "success" : "failed",
    error: k < 17 ? "" : "JSON 字段缺失: email",
    time: Date.now() - k * 60000,
  }));
  t1.failReasons = [{ reason: "JSON 字段缺失: email", count: 1 }];
  return out;
}



// ---------------- 代理 ----------------
export function genProxies(n = 12) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const status = pick(["online", "online", "online", "degraded", "offline"]);
    out.push({
      id: `prx_${String(i + 1).padStart(3, "0")}`,
      name: `proxy-${["sg", "us", "jp", "br", "de", "kr"][i % 6]}-${String(i + 1).padStart(2, "0")}`,
      address: `x.x.${rand(10, 99)}.${rand(2, 250)}:${rand(10000, 65000)}`,
      region: pick(["新加坡", "美国", "日本", "巴西", "德国", "韩国"]),
      protocol: pick(["http", "socks5", "http"]),
      status,
      accountCount: rand(0, 18),
      latency: status === "offline" ? 0 : rand(120, 900),
      successRate: status === "offline" ? 0 : rand(72, 100),
      lastTested: Date.now() - rand(2, 300) * 60e3,
      tags: pick([["主用"], ["备用"], ["主用", "低延迟"], ["备用", "测试"], []]),
      group: pick(["主池", "备用池", "测试池"]),
      latencyHistory: Array.from({ length: 24 }, () => rand(100, 900)),
      healthHistory: Array.from({ length: 14 }, (_, k) => (Math.random() > 0.25 ? "ok" : "fail")),
    });
  }
  return out;
}

// ---------------- 审计日志 ----------------
const ACTIONS = [
  "账号.查看凭证",
  "账号.导出",
  "账号.暂停",
  "账号.恢复",
  "账号.删除",
  "账号.编辑元数据",
  "账号.立即健康检查",
  "任务.创建",
  "任务.取消",
  "任务.重试失败项",
  "代理.新增",
  "代理.禁用",
  "代理.测试连接",
  "设置.修改",
  "设置.恢复默认",
  "导入.上传",
  "导出.执行",
];
const OBJECTS = ["账号", "任务", "代理", "设置", "导入", "导出", "策略", "通知"];
export function genAudit(n = 90) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const result = Math.random() > 0.18 ? "success" : "failed";
    out.push({
      id: `aud_${String(i + 1).padStart(5, "0")}`,
      time: Date.now() - rand(2, 1200) * 60e3,
      operator: pick(["admin", "li.wang", "ops_bot", "system", "wang.zhou"]),
      action: pick(ACTIONS),
      object: pick(OBJECTS),
      objectId: rand(0, 1) ? `acc_${String(rand(1, 48)).padStart(4, "0")}` : `task_${rand(1000, 1013)}`,
      result,
      ip: rand(0, 1) ? `103.${rand(1, 200)}.${rand(1, 200)}.${rand(1, 250)}` : "127.0.0.1",
      taskId: rand(0, 1) ? `task_${rand(1000, 1013)}` : "",
      detail: pick([
        "导出 12 个账号，脱敏开启",
        "凭证明文查看，授权有效期 30s",
        "注册策略已更新",
        "代理 proxy-sg-03 已禁用",
        "批量暂停 5 个账号",
        "导入 accounts_0716.json 成功",
      ]),
    });
  }
  return out;
}

// ---------------- 通知 ----------------
export function genNotifications() {
  return [
    { id: "ntf_1", level: "danger", title: "账号 acc_0042 健康检查失败", time: Date.now() - 3 * 60e3, read: false },
    { id: "ntf_2", level: "warning", title: "代理 proxy-sg-03 成功率低于 60%", time: Date.now() - 18 * 60e3, read: false },
    { id: "ntf_3", level: "warning", title: "12 个 access_token 将在 24h 内过期", time: Date.now() - 55 * 60e3, read: false },
    { id: "ntf_4", level: "info", title: "接入任务 task_1000 已完成", time: Date.now() - 2 * 3600e3, read: true },
    { id: "ntf_5", level: "success", title: "批量注册任务已完成，通过率 94.2%", time: Date.now() - 5 * 3600e3, read: true },
    { id: "ntf_6", level: "info", title: "导出报告 report_0716.csv 已生成", time: Date.now() - 26 * 3600e3, read: true },
  ];
}

// ---------------- 仪表盘趋势数据 ----------------
export function genTrend14() {
  const healthy = [38, 39, 40, 41, 43, 44, 44, 46, 47, 46, 47, 48, 47, 48];
  const warning = [4, 5, 4, 5, 4, 5, 6, 5, 5, 6, 5, 5, 6, 6];
  const unhealthy = [3, 3, 4, 3, 3, 4, 4, 5, 5, 4, 4, 5, 4, 5];
  return Array.from({ length: 14 }, (_, i) => {
    const day = new Date(Date.now() - (13 - i) * 86400e3);
    return {
      date: `${day.getMonth() + 1}/${day.getDate()}`,
      healthy: healthy[i],
      warning: warning[i],
      unhealthy: unhealthy[i],
      total: healthy[i] + warning[i] + unhealthy[i],
    };
  });
}

export function genEvents() {
  const ev = [
    { level: "danger", text: "账号 acc_0042 连续 3 次健康检查失败", time: Date.now() - 8 * 60e3 },
    { level: "warning", text: "代理 proxy-sg-03 成功率降至 55%", time: Date.now() - 22 * 60e3 },
    { level: "warning", text: "12 个 access_token 即将过期", time: Date.now() - 40 * 60e3 },
    { level: "danger", text: "刷新令牌刷新失败: invalid_grant (acc_0017)", time: Date.now() - 2 * 3600e3 },
    { level: "info", text: "接入任务 task_1004 开始执行", time: Date.now() - 3 * 3600e3 },
    { level: "success", text: "批量注册任务完成，通过率 94.2%", time: Date.now() - 5 * 3600e3 },
  ];
  return ev;
}

export function genToDoAccounts() {
  return [
    { id: "acc_0042", email: "lin***@outlook.com", reason: "健康检查连续失败", level: "danger" },
    { id: "acc_0017", email: "chen***@gmail.com", reason: "refresh_token 失效", level: "danger" },
    { id: "acc_0029", email: "wang***@icloud.com", reason: "token 即将过期", level: "warning" },
    { id: "acc_0033", email: "zhou***@qq.com", reason: "代理不可达", level: "warning" },
    { id: "acc_0008", email: "sun***@163.com", reason: "授权范围缺失", level: "warning" },
  ];
}

// ---------------- 设置 ----------------
export function defaultSettings() {
  return {
    general: {
      instanceName: "AccountOps",
      timezone: "Asia/Shanghai",
      language: "zh-CN",
    },
    refresh: {
      autoRefresh: true,
      interval: 30,
      maxAge: 720,
    },
    oauth: {
      callbackUrl: "https://ops.example.com/api/auth/callback",
      allowedOrigins: "https://ops.example.com, http://localhost:5173",
      clientName: "ops-console",
      connected: true,
    },
    proxy: {
      testTimeout: 10,
      maxLatency: 1500,
      minSuccessRate: 60,
      autoDisable: true,
    },
    notify: {
      inApp: true,
      webhook: false,
      webhookUrl: "",
      email: false,
      emailRecipients: "",
      warnThreshold: 60,
      dangerThreshold: 20,
    },
    sms: {
      api_key: "",
      base_url: "https://smsbower.app/stubs/handler_api.php",
      service: "dr",
      country: 73,
      max_price: 0.034,
    },
    sub2api: {
      base_url: "",
      admin_api_key: "",
      jwt: "",
      timeout: 30,
      proxy: "",
      group_ids: "",
    },
    retention: {
      logDays: 30,
      exportDays: 90,
      autoClean: true,
    },
  };
}
