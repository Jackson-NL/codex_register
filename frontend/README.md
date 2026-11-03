# AccountOps frontend

## 技术栈
- React 18 + TypeScript 5（渐进迁移，保留业务 JSX）
- Vite 8 + React Router 7（声明式路由，页面懒加载）
- Tailwind CSS 3 + shadcn/ui New York（本地源码）+ Radix primitives
- Lucide / Recharts；原有 FastAPI 接口和凭据处理契约不变

Node.js 要求：22.12+（22.x）、24.x 或26+，推荐24 LTS。开发/预览默认端口5180；后端8000。

```powershell
npm ci
npm run dev
npm run typecheck
npm run lint
npm run test:ui
npm test
npm run build
```

`npm run build`先做TypeScript检查再构建。`npm test`沿用现有Node业务逻辑测试；`test:ui`为Vitest + Testing Library的组件行为测试。旧测试已知基线：`codexOAuth.test.js`两项期望遗漏`auto_rebuild_failed`字段，需与OAuth业务单独同步，不是本轮UI改版引入。

## 组件与设计系统
- `src/styles/design-system.css`：亮/暗语义token、共享布局和旧页面视觉兼容层。
- `src/components/Layout.tsx`：导航、工具栏、移动端Sheet、搜索Dialog、用户和通知菜单。
- `src/components/RouteErrorBoundary.tsx`：隔离路由加载异常，保留外壳导航并提供刷新恢复，不显示原始错误堆栈。
- `src/components/ui/{button,card,input,dialog,dropdown-menu,sheet,switch}.tsx`：shadcn/ui基础组件。源码来自官方New York registry，保留MIT许可于`LICENSE.shadcn.md`，本地有少量可访问性和默认参数调整。
- `src/components/ui/index.jsx`：旧业务组件适配层，保留既有variant/loading/footer/closable等契约；新增页面优先直接使用TS primitives，不继续扩充此大文件。
- `components.json`：shadcn CLI配置，路径别名`@/*`已同步Vite和TypeScript。

旧页面不得通过改UI降低敏感字段保护、自动复制凭据或自动触发认证。顶部搜索已替换为真实功能模块搜索，支持名称、用途、关键词，不再展示虚构账号、任务或代理结果；它不是数据库内容搜索。

`package.json`将间接依赖`minimatch`固定到兼容的11.x修复版本（`^11.0.1`），避免当前lint工具链携带旧版glob DoS告警；升级ESLint/typescript-eslint时应复核该override是否仍需要。

## TypeScript迁移边界
入口、路由、共享Context、外壳和新增基础组件已迁移，严格类型检查开启。`allowJs: true` / `checkJs: false`只为旧业务JSX兼容，不代表所有账号/注册逻辑都已完成TS迁移。ESLint对新TS启用推荐规则和Hooks检查，旧JSX仅语法兼容，避免借换皮引入业务大重构。

## 工作导航与侧栏

点击品牌或侧栏「工作导航」进入 `/workspace`，按“创建新账号 / 恢复账号登录 / 获取授权与验证码”选择操作路径，或使用功能目录搜索与分类。

侧栏按工作空间、账号运营、认证与安全、基础资源、系统管理分组；支持模块搜索、分组折叠、星标收藏和新建任务快捷入口。当前路由所在分组保持展开，搜索时自动展开匹配分组。管理员入口仍按实例设置显示，认证门禁不变。

收藏只把有效模块 ID 写入 `accountops-module-favorites-v1`，不写账号或凭据。存储不可用时通过共享内存保留当前会话收藏，后打开的手机导航也能读取；仅此种情况下刷新会丢失收藏。各业务页顶部「模块指南」默认折叠，展开可查看能力边界与相关模块。

模块元数据集中于 `src/lib/navigation.ts`，同步驱动侧栏、指南、目录、面包屑和顶部功能搜索。新增业务模块时同时维护该目录与 `src/App.tsx` 路由。

## 邮箱资源中心

侧栏「邮箱配置」仍使用 `/mail-config`，页面重构为邮箱资源中心，兼容原有 `/api/mail-config` 接口，不迁移或删除已有邮箱数据。

- 概览显示服务端已保存的注册来源及两个邮箱池的数量；查看 Cloudflare/iCloud 不会切换来源。「设为注册来源」只修改草稿，点击「保存配置」后才生效。
- 服务配置与邮箱池分开。配置保存仅提交修改过的白名单字段；池导入、清空、回收等操作立即生效，但不提交或丢弃配置草稿。刷新资源也保留草稿。
- Cloudflare 池支持脱敏地址搜索、状态筛选、每页 25 条和当前页批量操作。更改筛选/分页会清空选择；释放、移除、强制回收需确认。核验全部凭据受服务端单次 500 条上限约束，不执行远程登录。
- iCloud 接口只返回最多 3 条脱敏样例，因此不提供伪造的全量列表、全池搜索或逐条状态操作；支持追加、整体替换和显式清空，后两者需确认。
- 导入目标固定为打开导入窗口时查看的服务。支持粘贴或 TXT，最多 256 KB / 5000 条；展示有效、输入内重复、错误数量，错误修正后才能提交。邮箱统一小写，收信 API URL 的大小写及 query 原样保留；iCloud 仅按换行分隔，不拆分 URL 中的逗号、分号。追加时已有邮箱由服务端去重，已有收信 API 不会被覆盖。
- 已保存凭证不回显、不使用假遮罩充当字段值；留空不修改，只能输入新值替换。现有 API 不支持显式删除已保存的站点密码/JWT，本页不会把清空输入伪装成删除成功。普通配置保存不会清空邮箱池。
- 草稿和导入凭据只保存在内存，不写浏览器存储。未保存时离开当前模块（含 SPA 导航/历史后退）需确认；操作执行中阻止离开。`useMailDraftGuard.ts` 是局部 BrowserRouter 适配器，退出模块后恢复 navigator 方法；不影响其他页面，也不改全局路由架构。

实现拆分于 `src/lib/mailConfig.ts`、`src/components/MailConfigPanels.tsx`、`MailPoolPanel.tsx`、`MailImportDialog.tsx` 和 `src/pages/MailConfig.tsx`。运行 `npm run test:ui -- test/mailConfig.test.tsx test/mailConfigPage.test.tsx` 验证解析、增量写入与主要交互。

## 2FA / MFA 验证码


侧栏「2FA / MFA 验证码」入口为 `/mfa-codes`。

- 密钥数据支持每行一个 Base32、`otpauth://totp` URI、`邮箱----密钥`、`邮箱----密码----密钥`，或 TXT 文件；生成后丢弃密码字段。最多 200 条、文件 256 KB。
- 本地使用 Web Crypto HMAC 按 RFC 6238 计算，支持 SHA-1/256/512、6/8 位、5–300 秒周期。需要 HTTPS 或 localhost 安全上下文，并校准本机时钟。
- 右上显示动态口令、倒计时、默认隐藏的密钥参数；可复制单项或全部。
- 邮箱查询只读本项目账号数据库的已绑定 Base32 2FA（默认 SHA-1 / 6 位 / 30 秒），不是外部 MFA 查询站点。接口 `POST /api/mfa/codes` 只接收邮箱、只返回口令和到期时间，保留管理员授权与来源检查，响应禁止缓存。重复邮箱、未绑定和无效密钥分别提示。
- 当前页面账户支持平台、账号、备注、JSON 导入与显式导出。JSON 文件上限 2 MB（最多 200 条），可往返导入包含完整多字节备注的导出文件。导出前提示 JSON 含明文密钥；导入格式为 `{ "version": 1, "accounts": [{ "label": "demo@example.invalid", "issuer": "Example", "secret": "BASE32_SECRET", "note": "", "algorithm": "SHA-1", "digits": 6, "period": 30 }] }`，也支持该账户数组。
- 手动输入的密钥、本页账户与常用邮箱仅存页面内存，不上传、不写库、不写 localStorage/sessionStorage；离开或刷新页面即清空。
