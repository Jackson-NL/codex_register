import type { ReactNode } from "react";
import type { AppContextValue, Language, Notification, SearchResult, Theme, ToastMessage, ToastOptions } from "./types";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { genNotifications, fmtAgo } from "../mock/data";
import { applyTheme, readStoredTheme, THEME_STORAGE_KEY } from "../theme";
import { installDocumentTranslator, readStoredLanguage, saveStoredLanguage, translate } from "../i18n";
import { api } from "../api";
import { searchModules } from "../lib/navigation";

const AppCtx = createContext<AppContextValue | null>(null);

let toastSeq = 0;

export function AppProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>(() => genNotifications());
  const [notifyOpen, setNotifyOpen] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(() => Date.now());
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQ, setSearchQ] = useState("");
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme());
  const [language, setLanguageState] = useState<Language>(() => readStoredLanguage());
  const [adminChecking, setAdminChecking] = useState(true);
  const [adminEnabled, setAdminEnabled] = useState(true);
  const [adminAuthenticated, setAdminAuthenticated] = useState(false);
  const [adminOverview, setAdminOverview] = useState<Record<string, unknown> | null>(null);
  const navigate = useNavigate();

  const refreshAdminSession = useCallback(async () => {
    setAdminChecking(true);
    try {
      const status = await api.admin.status();
      const enabled = status?.enabled !== false;
      setAdminEnabled(enabled);
      if (!enabled) {
        setAdminOverview(null);
        setAdminAuthenticated(false);
        return null;
      }
      const overview = await api.admin.overview();
      setAdminOverview(overview);
      setAdminAuthenticated(true);
      return overview;
    } catch {
      setAdminOverview(null);
      setAdminAuthenticated(false);
      setAdminEnabled(true);
      return null;
    } finally {
      setAdminChecking(false);
    }
  }, []);

  useEffect(() => {
    refreshAdminSession();
  }, [refreshAdminSession]);

  const adminLogin = useCallback(async (key: string) => {
    await api.admin.login(key);
    const overview = await refreshAdminSession();
    if (!overview) throw new Error("管理员会话校验失败，请重试");
    return overview;
  }, [refreshAdminSession]);

  const adminLogout = useCallback(async () => {
    try {
      await api.admin.logout();
    } finally {
      setAdminOverview(null);
      setAdminAuthenticated(false);
    }
  }, []);

  useEffect(() => {
    applyTheme(theme);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Storage may be unavailable in private or restricted browser contexts.
    }
  }, [theme]);

  useEffect(() => {
    saveStoredLanguage(language);
    document.documentElement.lang = language;
    document.title = language === "en-US" ? "AccountOps · Account operations console" : "AccountOps · 账号运维控制台";
    return installDocumentTranslator(language);
  }, [language]);

  // Use the saved UI preference when opening the console in a new browser profile.
  useEffect(() => {
    let alive = true;
    fetch("/api/settings/ui")
      .then((response) => response.ok ? response.json() : null)
      .then((payload) => {
        const saved = payload?.general?.language;
        if (alive && (saved === "en-US" || saved === "zh-CN")) setLanguageState(saved);
      })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  // 自动刷新：每 5s 更新"最近更新于 xx 秒前"
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const t = setInterval(() => setUpdatedAt(Date.now()), 5000);
    return () => clearInterval(t);
  }, [autoRefresh]);

  // Search actual feature metadata; never invent account/task/proxy records.
  const searchResults = useMemo<SearchResult[]>(() => {
    if (!searchQ.trim()) return [];
    return searchModules(searchQ, adminEnabled, value => translate(value, language)).slice(0, 10).map((module) => ({
      type: "功能", id: module.id, title: module.label, path: module.path, filter: {},
    }));
  }, [searchQ, adminEnabled, language]);

  const toast = useCallback((message: string, type: ToastMessage["type"] = "info", opts: ToastOptions = {}) => {
    const id = ++toastSeq;
    setToasts((prev) => [...prev, { id, message, type, ...opts }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), opts.duration || 3600);
  }, []);

  const pushNotify = useCallback((n: Notification) => setNotifications((prev) => [n, ...prev]), []);

  const markAllRead = useCallback(() => {
    setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
  }, []);

  const refreshNow = useCallback(() => setUpdatedAt(Date.now()), []);

  const toggleTheme = useCallback(() => {
    setTheme((current) => (current === "dark" ? "light" : "dark"));
  }, []);

  const setLanguage = useCallback((value: string) => {
    setLanguageState(value === "en-US" ? "en-US" : "zh-CN");
  }, []);
  const t = useCallback((value: unknown) => translate(value, language), [language]);

  const gotoWithFilter = useCallback(
    (path: string, filter: Record<string, unknown>) => {
      navigate(path, { state: { filter } });
    },
    [navigate],
  );

  const value = useMemo<AppContextValue>(
    () => ({
      toasts,
      toast,
      notifications,
      pushNotify,
      notifyOpen,
      setNotifyOpen,
      markAllRead,
      updatedAt,
      autoRefresh,
      setAutoRefresh,
      refreshNow,
      searchOpen,
      setSearchOpen,
      searchQ,
      setSearchQ,
      searchResults,
      gotoWithFilter,
      theme,
      setTheme,
      toggleTheme,
      language,
      setLanguage,
      t,
      adminChecking,
      adminEnabled,
      adminAuthenticated,
      adminOverview,
      adminLogin,
      adminLogout,
      updatedLabel: fmtAgo(updatedAt),
    }),
    [toasts, toast, notifications, pushNotify, notifyOpen, markAllRead, updatedAt, autoRefresh, refreshNow, searchOpen, searchQ, searchResults, gotoWithFilter, theme, toggleTheme, language, setLanguage, t, adminChecking, adminEnabled, adminAuthenticated, adminOverview, adminLogin, adminLogout],
  );

  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}

export function useApp() {
  const context = useContext(AppCtx);
  if (!context) throw new Error("useApp must be used within AppProvider");
  return context;
}
