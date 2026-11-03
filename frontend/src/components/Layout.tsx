import { Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  ArrowDownToLine, ArrowUpRight, Bell, ChevronDown, ChevronRight,
  LogOut, Menu, Moon, RefreshCw, Search, ShieldCheck, SlidersHorizontal, Sun, X,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import type { SearchResult } from "../context/types";
import { ToastHost } from "./ui";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "./ui/dialog";
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from "./ui/sheet";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { fmtAgo } from "../mock/data";
import WorkspaceSidebar from "./WorkspaceSidebar";
import { ModuleGuide } from "./ModuleGuide";
import { getModule, MODULES, MODULE_GROUPS } from "../lib/navigation";
import "./WorkspaceNavigation.css";

function ToolbarButton({ label, children, onClick, pressed }: { label: string; children: ReactNode; onClick?: () => void; pressed?: boolean }) {
  return <Button type="button" variant="ghost" size="icon" title={label} aria-label={label} aria-pressed={pressed} onClick={onClick} className="ops-toolbar-button">{children}</Button>;
}

export default function Layout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { notifyOpen, setNotifyOpen, notifications, markAllRead, updatedLabel, autoRefresh, setAutoRefresh, refreshNow, searchOpen, setSearchOpen, searchQ, setSearchQ, searchResults, gotoWithFilter, theme, toggleTheme, t, adminEnabled, adminLogout, toast } = useApp();
  const location = useLocation();
  const navigate = useNavigate();
  const searchTriggerRef = useRef<HTMLButtonElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const unread = notifications.filter((notification) => !notification.read).length;
  const current = getModule(location.pathname) ?? MODULES[1];

  useEffect(() => {
    mainRef.current?.scrollTo?.({ top: 0 });
  }, [location.pathname]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target;
      const editing = target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable);
      if ((event.key === "k" && (event.ctrlKey || event.metaKey)) || (event.key === "/" && !editing)) {
        event.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [setSearchOpen]);

  const goSearch = (result: SearchResult) => {
    setSearchOpen(false);
    setSearchQ("");
    gotoWithFilter(result.path, result.filter);
  };

  const logout = async () => {
    try { await adminLogout(); navigate("/admin"); }
    catch { toast(t("退出登录失败，请重试"), "error"); }
  };

  return (
    <div className="ops-shell">
      <a className="ops-skip-link" href="#main-content">{t("跳转到主内容")}</a>
      <aside className="ops-sidebar"><WorkspaceSidebar /></aside>
      <div className="ops-main-column">
        <header className="ops-topbar">
          <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
            <SheetTrigger asChild><Button variant="ghost" size="icon" className="ops-mobile-menu" aria-label={t("打开导航")}><Menu size={20} /></Button></SheetTrigger>
            <SheetContent side="left" className="ops-mobile-sidebar" aria-describedby="navigation-description" closeLabel={t("关闭导航")}>
              <SheetTitle className="sr-only">{t("主导航")}</SheetTitle><SheetDescription id="navigation-description" className="sr-only">{t("选择要打开的工作台或资源页面")}</SheetDescription>
              <WorkspaceSidebar onNavigate={() => setSidebarOpen(false)} />
            </SheetContent>
          </Sheet>
          <div className="ops-breadcrumb"><span>{t("工作空间")}</span><ChevronRight size={13} /><strong>{t(current.label)}</strong></div>
          <div className="ops-topbar-tools">
            <Button ref={searchTriggerRef} variant="ghost" className="ops-search-trigger" onClick={() => setSearchOpen(true)} aria-label={t("全局搜索")}><Search size={16} /><span>{t("功能搜索")}</span><kbd>⌘ K</kbd></Button>
            <span className="ops-toolbar-separator" />
            <ToolbarButton label={t(theme === "dark" ? "切换浅色主题" : "切换暗色主题")} onClick={toggleTheme}>{theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}</ToolbarButton>
            <ToolbarButton label={t("立即刷新")} onClick={refreshNow}><RefreshCw size={17} /></ToolbarButton>
            <DropdownMenu open={notifyOpen} onOpenChange={setNotifyOpen}>
              <DropdownMenuTrigger asChild><Button variant="ghost" size="icon" className="ops-toolbar-button ops-notification-trigger" aria-label={t("通知中心")}><Bell size={18} />{unread > 0 && <span className="ops-unread-dot" />}</Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="ops-notifications">
                <DropdownMenuLabel><span>{t("通知中心")} <span className="text-muted-foreground">({unread})</span></span></DropdownMenuLabel>
                <DropdownMenuItem onSelect={(event) => { event.preventDefault(); markAllRead(); }} className="text-primary">{t("全部已读")}</DropdownMenuItem>
                <DropdownMenuSeparator />
                <div className="max-h-80 overflow-y-auto">{notifications.length === 0 ? <p className="p-6 text-center text-sm text-muted-foreground">{t("暂无通知")}</p> : notifications.map((notification) => <div key={notification.id} className={`ops-notification ${notification.read ? "is-read" : ""}`}><span className={`ops-event-dot ${notification.level}`} /><div><p>{notification.title}</p><small>{fmtAgo(notification.time)}</small></div></div>)}</div>
              </DropdownMenuContent>
            </DropdownMenu>
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button variant="ghost" className="ops-user-trigger" aria-label={t("用户菜单")}><span className="ops-user-avatar">A</span><span className="ops-user-name">Admin</span><ChevronDown size={13} /></Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-52"><DropdownMenuLabel>AccountOps <span className="font-normal text-muted-foreground">/ Admin</span></DropdownMenuLabel><DropdownMenuSeparator /><DropdownMenuItem onSelect={() => navigate("/settings")}><SlidersHorizontal size={15} />{t("偏好设置")}</DropdownMenuItem>{adminEnabled && <><DropdownMenuItem onSelect={() => navigate("/admin")}><ShieldCheck size={15} />{t("管理员入口")}</DropdownMenuItem><DropdownMenuSeparator /><DropdownMenuItem onSelect={() => { void logout(); }} className="text-destructive"><LogOut size={15} />{t("退出登录")}</DropdownMenuItem></>}</DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>
        <div className="ops-contextbar"><span><span className="ops-status-dot" />{t("账号运维控制台")}<span className="ops-context-slash">/</span><span className="ops-context-section">{t(MODULE_GROUPS.find(group => group.id === current.group)?.label ?? current.group)}</span></span><button type="button" onClick={() => setAutoRefresh(!autoRefresh)} aria-pressed={autoRefresh} title={t(autoRefresh ? "关闭自动刷新" : "开启自动刷新")}><RefreshCw size={12} /><span>{t("更新于")} {updatedLabel}</span><span className="ops-auto-label">{t(autoRefresh ? "自动" : "手动")}</span></button></div>
        <main ref={mainRef} id="main-content" tabIndex={-1} className="ops-main"><ModuleGuide module={current} /><RouteErrorBoundary key={location.pathname}><Suspense fallback={<div role="status" className="p-8 text-sm text-muted-foreground">{t("加载中…")}</div>}><Outlet /></Suspense></RouteErrorBoundary></main>
      </div>
      <Dialog open={searchOpen} onOpenChange={setSearchOpen}>
        <DialogContent className="ops-search-dialog" aria-describedby="search-description" showClose={false} onCloseAutoFocus={(event) => { event.preventDefault(); searchTriggerRef.current?.focus(); }}>
          <DialogTitle className="sr-only">{t("全局搜索")}</DialogTitle><DialogDescription id="search-description" className="sr-only">{t("搜索模块名称、用途或关键词…")}</DialogDescription>
          <div className="ops-search-input"><Search size={19} /><Input value={searchQ} onChange={(event) => setSearchQ(event.target.value)} placeholder={t("搜索模块名称、用途或关键词…")} aria-label={t("全局搜索关键词")} /><Button variant="ghost" size="icon" aria-label={t("关闭搜索")} onClick={() => setSearchOpen(false)}><X size={16} /></Button></div>
          <div className="ops-search-results">{!searchQ && <div className="ops-search-empty"><Search size={27} /><p>{t("输入关键词，快速找到功能模块")}</p><small>{t("按 Esc 关闭搜索")}</small></div>}{searchQ && searchResults.length === 0 && <div className="ops-search-empty"><p>{t("未找到匹配结果")}</p></div>}{searchResults.map((result) => {
            const module = getModule(result.path);
            return <button key={result.id} type="button" onClick={() => goSearch(result)} className="ws-command-result">
              {module && <module.icon size={18} />}<span><strong>{t(result.title)}</strong><small>{t(module?.shortDescription ?? "功能模块")}</small></span><ArrowUpRight size={15} />
            </button>;
          })}</div>
          <div className="ops-search-footer"><span><ArrowDownToLine size={12} />{t("选择结果以跳转")}</span><kbd>ESC</kbd></div>
        </DialogContent>
      </Dialog>
      <ToastHost />
    </div>
  );
}

