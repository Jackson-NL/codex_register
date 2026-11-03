import type { Dispatch, SetStateAction } from "react";

export type Theme = "light" | "dark";
export type Language = "zh-CN" | "en-US";
export interface ToastOptions { duration?: number; detail?: string }
export interface ToastMessage extends ToastOptions {
  id: number;
  message: string;
  type: "info" | "success" | "error" | "warning";
}
export interface Notification {
  id: string;
  title: string;
  time: number | string;
  level: string;
  read: boolean;
}
export interface SearchResult {
  type: string;
  id: string;
  title: string;
  path: string;
  filter: Record<string, unknown>;
}
export interface AppContextValue {
  toasts: ToastMessage[];
  toast: (message: string, type?: ToastMessage["type"], options?: ToastOptions) => void;
  notifications: Notification[];
  pushNotify: (notification: Notification) => void;
  notifyOpen: boolean;
  setNotifyOpen: Dispatch<SetStateAction<boolean>>;
  markAllRead: () => void;
  updatedAt: number;
  updatedLabel: string;
  autoRefresh: boolean;
  setAutoRefresh: Dispatch<SetStateAction<boolean>>;
  refreshNow: () => void;
  searchOpen: boolean;
  setSearchOpen: Dispatch<SetStateAction<boolean>>;
  searchQ: string;
  setSearchQ: Dispatch<SetStateAction<string>>;
  searchResults: SearchResult[];
  gotoWithFilter: (path: string, filter: Record<string, unknown>) => void;
  theme: Theme;
  setTheme: Dispatch<SetStateAction<Theme>>;
  toggleTheme: () => void;
  language: Language;
  setLanguage: (language: string) => void;
  t: (value: unknown) => string;
  adminChecking: boolean;
  adminEnabled: boolean;
  adminAuthenticated: boolean;
  adminOverview: Record<string, unknown> | null;
  adminLogin: (key: string) => Promise<Record<string, unknown>>;
  adminLogout: () => Promise<void>;
}
