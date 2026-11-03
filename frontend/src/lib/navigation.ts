import {
  Compass,
  Fingerprint,
  KeyRound,
  LayoutDashboard,
  LogIn,
  Mail,
  Network,
  Settings,
  ShieldCheck,
  Users,
  UserPlus,
  type LucideIcon,
} from "lucide-react";

export const MODULE_GROUPS = [
  { id: "workspace", label: "工作空间", caption: "从这里开始" },
  { id: "accounts", label: "账号运营", caption: "创建与管理账号资产" },
  { id: "security", label: "认证与安全", caption: "恢复登录与双重验证" },
  { id: "resources", label: "基础资源", caption: "邮箱与网络出口" },
  { id: "system", label: "系统管理", caption: "实例配置与访问控制" },
] as const;
export type ModuleGroup = (typeof MODULE_GROUPS)[number]["id"];
export interface WorkspaceModule {
  id: string;
  path: string;
  label: string;
  group: ModuleGroup;
  icon: LucideIcon;
  description: string;
  shortDescription: string;
  capabilities: string[];
  keywords: string;
  next: string[];
  adminOnly?: boolean;
}

export const MODULES: WorkspaceModule[] = [
  {
    id: "workspace",
    path: "/workspace",
    label: "工作导航",
    group: "workspace",
    icon: Compass,
    description: "按目标找到合适的工具，查看模块能力与推荐操作路径。",
    shortDescription: "功能目录与操作路径",
    capabilities: ["功能模块目录", "按目标快速导航"],
    keywords: "home modules guide navigation 导航 目录 工作台",
    next: ["accounts", "register"],
  },
  {
    id: "dashboard",
    path: "/",
    label: "仪表盘",
    group: "workspace",
    icon: LayoutDashboard,
    description: "查看账号健康、注册进展与资源概况，发现需要处理的异常。",
    shortDescription: "全局概况与异常发现",
    capabilities: ["账号健康概览", "注册趋势与任务进展", "资源状态查看"],
    keywords: "dashboard stats overview 统计 总览 概况",
    next: ["accounts", "register", "proxies"],
  },
  {
    id: "accounts",
    path: "/accounts",
    label: "账号管理",
    group: "accounts",
    icon: Users,
    description: "集中管理已入库账号，筛选凭据状态、检查可用性并按需导出。",
    shortDescription: "账号资产与凭据状态",
    capabilities: ["账号筛选与批量操作", "凭据状态与健康检查", "账号信息与图像额度刷新", "账号导入导出"],
    keywords:
      "account assets health export import refresh quota 账号 资产 健康 筛选 导出 导入 刷新 额度",
    next: ["account-relogin", "codex-oauth", "mfa-codes"],
  },
  {
    id: "register",
    path: "/register",
    label: "注册工作台",
    group: "accounts",
    icon: UserPlus,
    description: "配置邮箱与代理后创建注册任务，跟踪执行日志和批量任务结果。",
    shortDescription: "新账号创建与任务追踪",
    capabilities: ["单次与批量注册", "任务阶段与执行日志", "注册结果入库"],
    keywords: "register signup batch tasks 注册 新建 创建 批量 日志 任务",
    next: ["mail-config", "proxies", "accounts"],
  },
  {
    id: "account-relogin",
    path: "/account-relogin",
    label: "账号重登",
    group: "security",
    icon: LogIn,
    description:
      "使用邮箱、密码和 2FA 恢复账号登录，优先获取 RT，并将成功结果写回账号库。",
    shortDescription: "恢复登录与更新凭据",
    capabilities: [
      "邮箱密码与 2FA 协议重登",
      "RT 优先，AT 兜底",
      "成功结果自动入库",
    ],
    keywords:
      "relogin recovery refresh token access token rt at 登录 恢复 重登 令牌 更新",
    next: ["accounts", "mfa-codes"],
  },
  {
    id: "codex-oauth",
    path: "/codex-oauth",
    label: "Codex OAuth",
    group: "security",
    icon: Fingerprint,
    description:
      "为已有账号执行 Codex OAuth 授权，查看候选账号、执行进度与授权结果。",
    shortDescription: "Codex 授权与结果管理",
    capabilities: ["候选账号与资格筛选", "OAuth 授权任务", "授权进度与结果"],
    keywords: "codex oauth authorization 授权 令牌",
    next: ["accounts", "proxies"],
  },
  {
    id: "mfa-codes",
    path: "/mfa-codes",
    label: "2FA / MFA 验证码",
    group: "security",
    icon: KeyRound,
    description:
      "本地生成动态验证码，按邮箱查询库内已绑定的 2FA，管理临时 MFA 账户。",
    shortDescription: "动态口令与临时账户",
    capabilities: [
      "本地 TOTP 与倒计时",
      "按邮箱只读查询验证码",
      "本页账户与 JSON 导入导出",
    ],
    keywords: "2fa mfa totp otp authenticator 验证码 密钥 双重验证",
    next: ["account-relogin", "accounts"],
  },
  {
    id: "mail-config",
    path: "/mail-config",
    label: "邮箱配置",
    group: "resources",
    icon: Mail,
    description: "配置注册所需的邮箱服务与邮箱池，统一管理验证码收取资源。",
    shortDescription: "邮箱服务与收码资源",
    capabilities: [
      "邮箱 Provider 配置",
      "Cloudflare 与 iCloud 邮箱池管理",
      "邮箱资源可用性检查",
    ],
    keywords: "mail email icloud cloudflare provider 邮箱 收码 服务 资源",
    next: ["register"],
  },
  {
    id: "proxies",
    path: "/proxies",
    label: "代理池",
    group: "resources",
    icon: Network,
    description:
      "维护网络出口代理，检查连通性与延迟，为注册和授权提供可用出口。",
    shortDescription: "网络出口与连通性",
    capabilities: ["代理导入与维护", "连通性与延迟检查", "可用网络出口筛选"],
    keywords: "proxy network latency connection 代理 网络 出口 延迟 连通",
    next: ["register", "codex-oauth"],
  },
  {
    id: "settings",
    path: "/settings",
    label: "系统设置",
    group: "system",
    icon: Settings,
    description: "集中调整实例级运行参数与界面偏好，修改后明确保存以应用配置。",
    shortDescription: "运行参数与界面偏好",
    capabilities: ["实例级运行配置", "界面语言与偏好", "配置保存与恢复默认"],
    keywords:
      "settings configuration preferences language theme 配置 参数 偏好 语言 主题 设置",
    next: ["workspace"],
  },
  {
    id: "admin",
    path: "/admin",
    label: "管理员入口",
    group: "system",
    icon: ShieldCheck,
    description: "查看管理员会话与受保护的管理信息，管理当前实例的访问边界。",
    shortDescription: "管理会话与访问控制",
    capabilities: ["管理员会话查看", "受保护的管理信息"],
    keywords: "admin security session 管理员 会话 安全 访问 控制",
    next: ["settings"],
    adminOnly: true,
  },
];

export function getModule(path: string): WorkspaceModule | undefined {
  return MODULES.find((module) => module.path === path);
}
export function searchModules(
  query: string,
  adminEnabled: boolean,
  translate: (value: string) => string = (value) => value,
): WorkspaceModule[] {
  const tokens = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return MODULES.filter(
    (module) =>
      (adminEnabled || !module.adminOnly) &&
      tokens.every((token) =>
        [
          module.id,
          module.label,
          module.description,
          module.shortDescription,
          module.keywords,
          ...module.capabilities,
          ...[
            module.label,
            module.description,
            module.shortDescription,
            ...module.capabilities,
          ].map(translate),
        ]
          .join(" ")
          .toLocaleLowerCase()
          .includes(token),
      ),
  );
}

export const FAVORITES_KEY = "accountops-module-favorites-v1";
export const DEFAULT_FAVORITES = ["accounts", "account-relogin", "mfa-codes"];
// Shared fallback survives lazy mobile drawer mounts when browser storage is unavailable.
// Only validated navigation IDs are kept; never account identifiers or credentials.
let memoryFavorites = [...DEFAULT_FAVORITES];
function validIds(values: unknown): string[] {
  if (!Array.isArray(values)) return [...DEFAULT_FAVORITES];
  return [
    ...new Set(
      values.filter(
        (id): id is string =>
          typeof id === "string" && MODULES.some((module) => module.id === id),
      ),
    ),
  ];
}
export function readFavorites(storage?: Pick<Storage, "getItem">): string[] {
  try {
    const source = storage ?? window.localStorage;
    const value = source.getItem(FAVORITES_KEY);
    const ids =
      value === null ? [...DEFAULT_FAVORITES] : validIds(JSON.parse(value));
    if (!storage) memoryFavorites = ids;
    return [...ids];
  } catch {
    return [...(storage ? DEFAULT_FAVORITES : memoryFavorites)];
  }
}
export function writeFavorites(
  ids: string[],
  storage?: Pick<Storage, "setItem">,
): void {
  const safeIds = validIds(ids);
  if (!storage) memoryFavorites = safeIds;
  try {
    (storage ?? window.localStorage).setItem(
      FAVORITES_KEY,
      JSON.stringify(safeIds),
    );
  } catch {
    /* Preferences are optional; memory-only operation remains available. */
  }
}
