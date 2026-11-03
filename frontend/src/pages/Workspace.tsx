import { useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  Compass,
  Search,
  ShieldCheck,
  X,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { MODULE_GROUPS, MODULES, searchModules } from "../lib/navigation";
import "./Workspace.css";

const FLOWS = [
  {
    number: "01",
    title: "创建新账号",
    description: "先准备资源，再创建任务，最后检查入库结果。",
    steps: ["mail-config", "proxies", "register", "accounts"],
  },
  {
    number: "02",
    title: "恢复账号登录",
    description: "为已有账号恢复凭据，成功后回到账号库检查。",
    steps: ["account-relogin", "accounts"],
  },
  {
    number: "03",
    title: "获取授权与验证码",
    description: "按需进入授权或动态口令工具，不自动触发认证。",
    steps: ["codex-oauth", "mfa-codes"],
  },
];

export default function Workspace() {
  const { t, adminEnabled } = useApp();
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("all");
  const modules = searchModules(query, adminEnabled, t).filter(
    (module) => module.id !== "workspace",
  );
  const visible = modules.filter(
    (module) => group === "all" || module.group === group,
  );
  return (
    <div className="ws-page">
      <header className="ws-page-intro">
        <div>
          <span className="ws-overline">YOUR OPERATIONS, ORGANIZED</span>
          <h1>
            {t("工作导航")}
            <span aria-hidden="true">.</span>
          </h1>
          <p>
            {t("先明确目标，再选择工具。让每一次账号操作都有清晰的下一步。")}
          </p>
        </div>
        <Link to="/" className="ws-overview-link">
          {t("查看运行概况")}
          <ArrowUpRight size={16} />
        </Link>
      </header>
      <section className="ws-hero" aria-labelledby="workflow-title">
        <div className="ws-hero-title">
          <span className="ws-hero-icon">
            <Compass size={21} />
          </span>
          <div>
            <small>QUICK START</small>
            <h2 id="workflow-title">{t("你今天想完成什么？")}</h2>
          </div>
          <span className="ws-hero-note">
            <ShieldCheck size={14} />
            {t("只导航，不自动执行")}
          </span>
        </div>
        <div className="ws-workflows">
          {FLOWS.map((flow) => (
            <article key={flow.number}>
              <span className="ws-flow-number">{flow.number}</span>
              <h3>{t(flow.title)}</h3>
              <p>{t(flow.description)}</p>
              <div className="ws-flow-steps">
                {flow.steps.map((id, index) => {
                  const module = MODULES.find((item) => item.id === id)!;
                  return (
                    <span key={id}>
                      {index > 0 && <ArrowRight size={11} />}
                      <Link to={module.path}>
                        <module.icon size={13} />
                        {t(module.label)}
                      </Link>
                    </span>
                  );
                })}
              </div>
            </article>
          ))}
        </div>
      </section>
      <section className="ws-module-section" aria-label={t("功能模块目录")}>
        <div className="ws-directory-heading">
          <div>
            <span className="ws-overline">MODULE DIRECTORY</span>
            <h2>{t("所有功能，各司其职")}</h2>
          </div>
          <div className="ws-directory-search">
            <Search size={16} />
            <input
              type="search"
              aria-label={t("搜索功能模块")}
              placeholder={t("搜索名称、用途或关键词…")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              autoComplete="off"
            />
            {query && (
              <button
                type="button"
                aria-label={t("清除目录搜索")}
                onClick={() => setQuery("")}
              >
                <X size={14} />
              </button>
            )}
          </div>
        </div>
        <div className="ws-filters" role="group" aria-label={t("按功能分类")}>
          <button
            type="button"
            aria-pressed={group === "all"}
            onClick={() => setGroup("all")}
          >
            {t("全部模块")}
            <span>{modules.length}</span>
          </button>
          {MODULE_GROUPS.filter((item) => item.id !== "workspace").map(
            (item) => (
              <button
                type="button"
                key={item.id}
                aria-pressed={group === item.id}
                onClick={() => setGroup(item.id)}
              >
                {t(item.label)}
                <span>
                  {modules.filter((module) => module.group === item.id).length}
                </span>
              </button>
            ),
          )}
        </div>
        <div className="ws-module-grid">
          {visible.map((module) => (
            <article className="ws-module-card" key={module.id}>
              <div className="ws-module-card-top">
                <span className={`ws-module-icon tone-${module.group}`}>
                  <module.icon size={20} />
                </span>
                <small>
                  {t(
                    MODULE_GROUPS.find((item) => item.id === module.group)!
                      .label,
                  )}
                </small>
                <ArrowUpRight size={16} />
              </div>
              <h3>{t(module.label)}</h3>
              <p>{t(module.description)}</p>
              <ul>
                {module.capabilities.map((capability) => (
                  <li key={capability}>
                    <Check size={12} />
                    {t(capability)}
                  </li>
                ))}
              </ul>
              <Link
                to={module.path}
                aria-label={`${t("打开")} ${t(module.label)}`}
              >
                {t("进入模块")}
                <ArrowRight size={15} />
              </Link>
            </article>
          ))}
        </div>
        {!visible.length && (
          <div className="ws-directory-empty" role="status">
            <Search size={28} />
            <h3>{t("没有匹配的模块")}</h3>
            <p>{t("试试其他关键词，或清除筛选查看全部功能。")}</p>
            <button
              type="button"
              onClick={() => {
                setQuery("");
                setGroup("all");
              }}
            >
              {t("重置筛选")}
            </button>
          </div>
        )}
      </section>
      <footer className="ws-directory-footer">
        <ShieldCheck size={15} />
        <p>
          {t("导航收藏仅保存模块标识；账号凭据仍由各业务模块按原有规则管理。")}
        </p>
        <span>{t("工具有边界，操作有路径")}</span>
      </footer>
    </div>
  );
}
