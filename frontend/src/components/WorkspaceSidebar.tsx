import { useEffect, useId, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import {
  ArrowUpRight,
  ChevronDown,
  Command,
  Compass,
  Search,
  ShieldCheck,
  Star,
  X,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import {
  MODULE_GROUPS,
  MODULES,
  readFavorites,
  searchModules,
  writeFavorites,
  type WorkspaceModule,
} from "../lib/navigation";

export default function WorkspaceSidebar({
  onNavigate,
}: {
  onNavigate?: () => void;
}) {
  const { t, adminEnabled } = useApp();
  const { pathname } = useLocation();
  const [query, setQuery] = useState("");
  const [favorites, setFavorites] = useState(readFavorites);
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const instance = useId();
  const visible = searchModules(query, adminEnabled, t);
  const pinned = favorites
    .map((id) => MODULES.find((module) => module.id === id))
    .filter(
      (module): module is WorkspaceModule =>
        !!module && (adminEnabled || !module.adminOnly),
    );
  useEffect(() => {
    const syncStorage = () => setFavorites(readFavorites());
    const syncMemory = (event: Event) => {
      if (event instanceof CustomEvent && Array.isArray(event.detail))
        setFavorites(
          event.detail.filter(
            (id: unknown): id is string =>
              typeof id === "string" &&
              MODULES.some((module) => module.id === id),
          ),
        );
    };
    window.addEventListener("storage", syncStorage);
    window.addEventListener("accountops-favorites-change", syncMemory);
    return () => {
      window.removeEventListener("storage", syncStorage);
      window.removeEventListener("accountops-favorites-change", syncMemory);
    };
  }, []);
  const toggleFavorite = (id: string) => {
    const next = favorites.includes(id)
      ? favorites.filter((value) => value !== id)
      : [...favorites, id];
    setFavorites(next);
    writeFavorites(next);
    // Synchronize desktop and mobile instances without putting business data in storage.
    window.dispatchEvent(
      new CustomEvent("accountops-favorites-change", { detail: next }),
    );
  };

  return (
    <>
      <Link
        to="/workspace"
        className="ops-brand"
        onClick={onNavigate}
        aria-label="AccountOps"
      >
        <span className="ops-brand-mark">
          <Command size={21} />
        </span>
        <span>
          <strong>
            AccountOps<span className="ops-brand-period">.</span>
          </strong>
          <small>ACCOUNT OPERATIONS</small>
        </span>
      </Link>
      <div className="ws-instance">
        <span className="ops-status-dot" />
        <div>
          <strong>{t("账号运维控制台")}</strong>
          <small>{t("实例工作区")}</small>
        </div>
        <span className="ws-instance-tag">LOCAL</span>
      </div>
      <Link to="/register" className="ws-create" onClick={onNavigate}>
        <span>+</span>
        {t("新建注册任务")}
        <ArrowUpRight size={14} />
      </Link>
      <div className="ws-sidebar-search">
        <Search size={15} />
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t("搜索侧栏模块")}
          placeholder={t("搜索模块…")}
          autoComplete="off"
        />
        {query && (
          <button
            type="button"
            aria-label={t("清除模块搜索")}
            onClick={() => setQuery("")}
          >
            <X size={14} />
          </button>
        )}
      </div>
      <div className="ws-sidebar-scroll">
        {!query && (
          <nav className="ws-favorites" aria-label={t("常用模块")}>
            <div className="ws-section-caption">
              <Star size={11} />
              {t("常用模块")}
              <small>{t("仅保存导航偏好")}</small>
            </div>
            <div className="ws-favorite-links">
              {pinned.length ? (
                pinned.map((module) => (
                  <Link
                    key={module.id}
                    to={module.path}
                    onClick={onNavigate}
                    aria-label={t(module.label)}
                    title={t(module.label)}
                  >
                    <module.icon size={13} />
                    <span>{t(module.label)}</span>
                  </Link>
                ))
              ) : (
                <p>{t("点击模块旁的星标，加入常用")}</p>
              )}
            </div>
          </nav>
        )}
        <nav className="ops-navigation ws-navigation" aria-label={t("主导航")}>
          {MODULE_GROUPS.map((group, index) => {
            const items = visible.filter((module) => module.group === group.id);
            if (!items.length) return null;
            const current = items.some((module) => module.path === pathname);
            const expanded =
              !!query || current || !collapsed.includes(group.id);
            const groupId = `${instance}-${group.id}`;
            return (
              <section className="ops-nav-group" key={group.id}>
                <button
                  type="button"
                  className="ws-group-toggle"
                  aria-label={t(group.label)}
                  aria-expanded={expanded}
                  aria-controls={groupId}
                  title={
                    current ? t("当前模块所在分组保持展开") : t(group.caption)
                  }
                  onClick={() => {
                    if (!current)
                      setCollapsed((previous) =>
                        previous.includes(group.id)
                          ? previous.filter((id) => id !== group.id)
                          : [...previous, group.id],
                      );
                  }}
                >
                  <span>{String(index + 1).padStart(2, "0")}</span>
                  <strong>{t(group.label)}</strong>
                  <ChevronDown
                    size={12}
                    className={expanded ? "" : "is-collapsed"}
                  />
                </button>
                <div id={groupId} hidden={!expanded}>
                  {items.map((module) => (
                    <div
                      className={`ws-nav-row ${module.path === pathname ? "is-current" : ""}`}
                      key={module.id}
                    >
                      <NavLink
                        to={module.path}
                        end
                        onClick={onNavigate}
                        aria-label={t(module.label)}
                        title={t(module.description)}
                        className={({ isActive }) =>
                          `ops-nav-link ${isActive ? "is-active" : ""}`
                        }
                      >
                        <module.icon size={17} strokeWidth={1.6} />
                        <span>
                          <strong>{t(module.label)}</strong>
                          {module.path === pathname && (
                            <small>{t(module.shortDescription)}</small>
                          )}
                        </span>
                      </NavLink>
                      <button
                        type="button"
                        className={`ws-pin ${favorites.includes(module.id) ? "is-pinned" : ""}`}
                        aria-label={`${t(favorites.includes(module.id) ? "取消收藏" : "收藏")} ${t(module.label)}`}
                        aria-pressed={favorites.includes(module.id)}
                        title={t(
                          favorites.includes(module.id) ? "取消收藏" : "收藏",
                        )}
                        onClick={() => toggleFavorite(module.id)}
                      >
                        <Star
                          size={12}
                          fill={
                            favorites.includes(module.id)
                              ? "currentColor"
                              : "none"
                          }
                        />
                      </button>
                    </div>
                  ))}
                </div>
              </section>
            );
          })}
          {!visible.length && (
            <div className="ws-no-results">
              <Search size={22} />
              <p>{t("没有匹配的模块")}</p>
              <button type="button" onClick={() => setQuery("")}>
                {t("查看全部模块")}
              </button>
            </div>
          )}
        </nav>
      </div>
      <div className="ops-sidebar-bottom ws-sidebar-bottom">
        <Link
          to="/workspace"
          onClick={onNavigate}
          className="ws-directory-link"
        >
          <Compass size={17} />
          <span>
            <strong>{t("不知道从哪里开始？")}</strong>
            <small>{t("查看功能目录与操作路径")}</small>
          </span>
          <ArrowUpRight size={14} />
        </Link>
        <div className="ws-sidebar-footnote">
          <ShieldCheck size={12} />
          {t("凭据按需查看，敏感操作手动确认")}
        </div>
      </div>
    </>
  );
}
