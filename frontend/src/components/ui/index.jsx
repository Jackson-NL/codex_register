import { forwardRef, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, ChevronsUpDown, Search, X, Loader2, Inbox, AlertTriangle, RefreshCw, Eye, EyeOff, Copy, Check, ShieldAlert, XCircle } from "lucide-react";
import { useApp } from "../../context/AppContext";
import { Button as PrimitiveButton } from "./button";
import { Card, CardContent, CardHeader } from "./card";
import { Input as PrimitiveInput } from "./input";
import { Dialog, DialogContent, DialogTitle } from "./dialog";
import { Sheet, SheetContent, SheetTitle } from "./sheet";
import { Switch as PrimitiveSwitch } from "./switch";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "./dropdown-menu";
import { cn } from "../../lib/utils";

function translateNode(node, t) {
  if (typeof node === "string") return t(node);
  if (Array.isArray(node)) return node.map((item) => translateNode(item, t));
  return node;
}

function captureReturnFocus() {
  const active = document.activeElement;
  const menu = active?.closest?.("[role='menu']");
  const triggerId = menu?.getAttribute("data-return-focus-id");
  return (triggerId && document.getElementById(triggerId)) || active;
}

// ---------------- 基础 ---------------- //
/** @type {import("react").ForwardRefExoticComponent<import("./legacy-types").LegacyButtonProps & import("react").RefAttributes<HTMLButtonElement>>} */
export const Button = forwardRef(function Button({ children, onClick, variant = "primary", size = "md", icon, disabled, loading, className = "", type = "button", title, ...props }, ref) {
  const { t } = useApp();
  const v = { primary: "default", secondary: "outline", ghost: "ghost", danger: "destructive", dangerSoft: "outline", success: "default" }[variant] || "default";
  const s = { sm: "sm", md: "default", lg: "lg" }[size] || "default";
  return (
    <PrimitiveButton ref={ref} {...props} type={type} title={title} variant={v} size={s} disabled={disabled || loading} aria-busy={loading || undefined} onClick={onClick}
      className={cn("btn", variant === "dangerSoft" && "border-destructive/30 text-destructive hover:bg-destructive/10", className)}>
      {loading ? <Loader2 size={14} className="animate-spin" /> : icon}
      {translateNode(children, t)}
    </PrimitiveButton>
  );
});

/** @type {import("react").ForwardRefExoticComponent<import("./legacy-types").LegacyIconButtonProps & import("react").RefAttributes<HTMLButtonElement>>} */
export const IconBtn = forwardRef(function IconBtn({ icon, onClick, title, danger, active, className = "", ...props }, ref) {
  const { t } = useApp();
  return (
    <PrimitiveButton ref={ref} {...props} type="button" variant="ghost" size="icon" title={t(title)} aria-label={t(title || "更多操作")} onClick={onClick}
      className={cn("h-8 w-8 text-muted-foreground", danger && "hover:text-destructive", active && "bg-accent text-accent-foreground", className)}>
      {icon}
    </PrimitiveButton>
  );
});

const COLOR_MAP = {
  success: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warning: "bg-amber-50 text-amber-700 border-amber-200",
  danger: "bg-red-50 text-red-700 border-red-200",
  info: "bg-blue-50 text-blue-700 border-blue-200",
  neutral: "bg-slate-100 text-slate-600 border-slate-200",
  primary: "bg-blue-50 text-blue-700 border-blue-200",
};
const DOT_MAP = { success: "bg-emerald-500", warning: "bg-amber-500", danger: "bg-red-500", info: "bg-blue-500", neutral: "bg-slate-400" };

export function Badge({ children, color = "neutral", dot, className = "" }) {
  const { t } = useApp();
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-px text-xs font-medium ${COLOR_MAP[color]} ${className}`}>
      {dot && <span className={`h-1.5 w-1.5 rounded-full ${DOT_MAP[color]}`} />}
      {translateNode(children, t)}
    </span>
  );
}

export function Tag({ children, onRemove, color = "neutral" }) {
  const { t } = useApp();
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-px text-[11px] ${COLOR_MAP[color]}`}>
      {translateNode(children, t)}
      {onRemove && <button onClick={onRemove} className="opacity-60 hover:opacity-100"><X size={10} /></button>}
    </span>
  );
}

export function Panel({ title, extra, children, className = "", bodyClass = "", pad = true }) {
  const { t } = useApp();
  return (
    <Card className={cn("panel", className)}>
      {(title || extra) && (
        <CardHeader className="panel-head flex-row space-y-0">
          <div className="panel-title">{typeof title === "string" ? t(title) : title}</div>
          {extra}
        </CardHeader>
      )}
      <CardContent className={cn(pad ? "ops-panel-body" : "p-0", bodyClass)}>{children}</CardContent>
    </Card>
  );
}

// ---------------- 表单 ---------------- //
export function Input({ label, hint, error, icon, className = "", ...props }) {
  const { t } = useApp();
  return (
    <label className="block">
      {label && <span className="mb-1 block text-xs font-medium text-slate-600">{t(label)}</span>}
      <div className="relative">
        {icon && <span className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400">{icon}</span>}
        <PrimitiveInput {...props} aria-invalid={!!error} className={cn("input", icon && "pl-8", error && "border-destructive", className)} />
      </div>
      {hint && !error && <span className="mt-1 block text-[11px] text-slate-400">{t(hint)}</span>}
      {error && <span className="mt-1 block text-[11px] text-red-600">{t(error)}</span>}
    </label>
  );
}

export function Select({ label, options, value, onChange, className = "", placeholder }) {
  const { t } = useApp();
  return (
    <label className="block">
      {label && <span className="mb-1 block text-xs font-medium text-slate-600">{t(label)}</span>}
      <div className="relative">
        <select value={value ?? ""} onChange={(e) => onChange(e.target.value)} className={`input appearance-none pr-7 ${className}`}>
          {placeholder && <option value="">{t(placeholder)}</option>}
          {options.map((o) => (
            <option key={o.value} value={o.value}>{t(o.label)}</option>
          ))}
        </select>
        <ChevronDown size={14} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-slate-400" />
      </div>
    </label>
  );
}

export function Switch({ checked, onChange, label, disabled }) {
  const { t } = useApp();
  return (
    <label className="inline-flex min-h-9 items-center gap-2 cursor-pointer">
      <PrimitiveSwitch checked={checked} onCheckedChange={onChange} disabled={disabled} aria-label={t(label || "切换设置")} />
      {label && <span className="text-[13px] text-slate-700">{t(label)}</span>}
    </label>
  );
}

export function Checkbox({ checked, onChange, indeterminate }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input ref={ref} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)}
      className="h-3.5 w-3.5 cursor-pointer rounded border-slate-300 accent-blue-600" />
  );
}

export function SearchInput({ value, onChange, placeholder = "搜索…", className = "" }) {
  const { t } = useApp();
  return (
    <div className={`relative ${className}`}>
      <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={t(placeholder)}
        className="input pl-8 w-52" />
      {value && (
        <button onClick={() => onChange("")} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
          <X size={13} />
        </button>
      )}
    </div>
  );
}

// ---------------- 状态 ---------------- //
export function Empty({ title = "暂无数据", desc, icon, action }) {
  const { t } = useApp();
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="mb-2 text-slate-300">{icon || <Inbox size={36} strokeWidth={1.2} />}</div>
      <div className="text-sm text-slate-600">{t(title)}</div>
      {desc && <div className="mt-1 max-w-sm text-xs text-slate-400">{t(desc)}</div>}
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function Loading({ rows = 4, cols = 5, label = "加载中…" }) {
  const { t } = useApp();
  return (
    <div className="p-4">
      <div className="mb-3 flex items-center gap-2 text-xs text-slate-400"><Loader2 size={13} className="animate-spin" /> {t(label)}</div>
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="flex gap-3">
            {Array.from({ length: cols }).map((_, c) => (
              <div key={c} className="h-4 flex-1 animate-pulse rounded bg-slate-100" style={{ width: `${30 + ((r * 13 + c * 29) % 50)}%` }} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function ErrorState({ message = "加载失败", onRetry }) {
  const { t } = useApp();
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <AlertTriangle size={30} strokeWidth={1.4} className="mb-2 text-amber-500" />
      <div className="text-sm text-slate-700">{t(message)}</div>
      {onRetry && (
        <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} className="mt-3" onClick={onRetry}>重试</Button>
      )}
    </div>
  );
}

export function ProgressBar({ value, color = "bg-blue-600", height = "h-1.5", showText }) {
  return (
    <div className="flex items-center gap-2">
      <div className={`w-full overflow-hidden rounded-full bg-slate-100 ${height}`}>
        <div className={`${height} rounded-full ${color} transition-all`} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
      </div>
      {showText && <span className="tnum text-xs text-slate-500">{value}%</span>}
    </div>
  );
}

// ---------------- 弹窗 / 抽屉 ---------------- //
export function Modal({ open, onClose, title, children, footer, width = 560, closable = true }) {
  const { t } = useApp();
  const returnFocus = useRef(null);
  return (
    <Dialog open={!!open} onOpenChange={(value) => { if (!value && closable) onClose(); }}>
      <DialogContent aria-describedby={undefined} showClose={closable} closeLabel={t("关闭")} className="flex max-h-[88dvh] w-[calc(100vw-32px)] flex-col gap-0 overflow-hidden p-0 rounded-xl" style={{ maxWidth: width }}
        onOpenAutoFocus={() => { returnFocus.current = captureReturnFocus(); }}
        onCloseAutoFocus={(event) => { event.preventDefault(); returnFocus.current?.focus?.(); }}
        onEscapeKeyDown={(event) => { if (!closable) event.preventDefault(); }} onInteractOutside={(event) => { if (!closable) event.preventDefault(); }}>
        <div className="flex items-center justify-between border-b border-border px-5 py-5 pr-14">
          <DialogTitle>{t(title)}</DialogTitle>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-5 py-5">{children}</div>
        {footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-muted/30 px-5 py-4">{footer}</div>}
      </DialogContent>
    </Dialog>
  );
}

export function Confirm({ open, onClose, onConfirm, title = "确认操作", message, danger, confirmText = "确认", loading }) {
  return (
    <Modal open={open} onClose={onClose} title={title} width={420}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>取消</Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} loading={loading}>{confirmText}</Button>
        </>
      }>
      <div className="flex gap-3">
        <div className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full ${danger ? "bg-red-50 text-red-600" : "bg-blue-50 text-blue-600"}`}>
          <AlertTriangle size={16} />
        </div>
        <p className="text-[13px] leading-relaxed text-slate-600">{message}</p>
      </div>
    </Modal>
  );
}

export function Drawer({ open, onClose, title, children, footer, width = 640 }) {
  const { t } = useApp();
  const returnFocus = useRef(null);
  return (
    <Sheet open={!!open} onOpenChange={(value) => { if (!value) onClose(); }}>
      <SheetContent aria-describedby={undefined} closeLabel={t("关闭")} className="flex w-full flex-col gap-0 p-0 sm:max-w-none" style={{ maxWidth: width }}
        onOpenAutoFocus={() => { returnFocus.current = captureReturnFocus(); }} onCloseAutoFocus={(event) => { event.preventDefault(); returnFocus.current?.focus?.(); }}>
        <div className="flex items-center justify-between border-b border-border px-5 py-5 pr-14">
          <SheetTitle>{t(title)}</SheetTitle>
        </div>
        <div className="min-h-0 flex-1 overflow-auto">{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-4">{footer}</div>}
      </SheetContent>
    </Sheet>
  );
}

// ---------------- 标签页 ---------------- //
export function Tabs({ tabs, active, onChange, size = "md" }) {
  const { t: translateText } = useApp();
  return (
    <div className={`flex items-center gap-1 border-b border-slate-200 ${size === "sm" ? "px-2" : "px-4"}`}>
      {tabs.map((t) => (
        <button key={t.key} onClick={() => onChange(t.key)}
          className={`relative -mb-px border-b-2 px-3 py-2 text-[13px] transition-colors ${active === t.key
            ? "border-blue-600 font-medium text-blue-700"
            : "border-transparent text-slate-500 hover:text-slate-800"}`}>
          {t.label && (typeof t.label === "string" ? translateText(t.label) : t.label)}
          {t.count != null && <span className="ml-1.5 rounded bg-slate-100 px-1 text-[11px] text-slate-500">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

// ---------------- 下拉菜单 ---------------- //
export function Dropdown({ trigger, items, align = "right" }) {
  const { t } = useApp();
  const triggerRef = useRef(null);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger ref={triggerRef} asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent data-return-focus-id={triggerRef.current?.id} align={align === "right" ? "end" : "start"} className="min-w-40">
        {items.map((item, index) => item.divider ? <DropdownMenuSeparator key={index} /> : <DropdownMenuItem key={index} disabled={item.disabled} onSelect={() => item.onClick?.()} className={item.danger ? "text-destructive focus:text-destructive" : ""}>{item.icon}{typeof item.label === "string" ? t(item.label) : item.label}</DropdownMenuItem>)}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ---------------- 数据表 ---------------- //
export function DataTable({ columns, data, loading, error, onRetry, selectable, selected, onSelectAll, onSelectRow, rowKey = "id", page, pageSize, total, onPage, onPageSize, emptyTitle, toolbar, rowClick, dense }) {
  const { t } = useApp();
  const pages = Math.max(1, Math.ceil((total ?? data.length) / pageSize));
  return (
    <div>
      {toolbar}
      <div className="overflow-x-auto">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50/70 text-left">
              {selectable && (
                <th className="w-9 px-3 py-2">
                  <Checkbox checked={selected.length > 0 && selected.length === data.length}
                    indeterminate={selected.length > 0 && selected.length < data.length}
                    onChange={(v) => onSelectAll?.(v)} />
                </th>
              )}
              {columns.map((c) => (
                <th key={c.key} className={`th ${c.align === "right" ? "text-right" : ""}`} style={{ width: c.width }}>
                  <span
                    onClick={c.onClick}
                    className={`inline-flex items-center gap-1 ${c.onClick ? "cursor-pointer select-none hover:text-slate-800" : ""}`}>
                    {typeof c.title === "string" ? t(c.title) : c.title}
                    {c.sortable && <SortIcon col={c} />}
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td colSpan={columns.length + (selectable ? 1 : 0)}><Loading rows={5} cols={Math.min(6, columns.length)} /></td></tr>
            )}
            {!loading && error && (
              <tr><td colSpan={columns.length + (selectable ? 1 : 0)}><ErrorState message={error} onRetry={onRetry} /></td></tr>
            )}
            {!loading && !error && data.length === 0 && (
              <tr><td colSpan={columns.length + (selectable ? 1 : 0)}><Empty title={emptyTitle || "暂无数据"} /></td></tr>
            )}
            {!loading && !error && data.map((row) => (
              <tr key={row[rowKey]} onClick={() => rowClick?.(row)}
                className={`tr-row ${rowClick ? "cursor-pointer" : ""} ${dense ? "" : ""}`}>
                {selectable && (
                  <td className="td px-3" onClick={(e) => e.stopPropagation()}>
                    <Checkbox checked={selected.includes(row[rowKey])} onChange={(v) => onSelectRow?.(row[rowKey], v)} />
                  </td>
                )}
                {columns.map((c) => (
                  <td key={c.key} className={`td ${c.align === "right" ? "text-right" : ""}`} style={c.mono ? { fontFamily: "var(--font-mono)", fontSize: 12 } : undefined}>
                    {c.render ? c.render(row) : row[c.key]}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pageSize > 0 && (
        <Pagination page={page} pages={pages} total={total ?? data.length} pageSize={pageSize} onPage={onPage} onPageSize={onPageSize} />
      )}
    </div>
  );
}

function SortIcon({ col }) {
  return (
    <span className="text-slate-400">
      {col.sortDir === "asc" ? <ChevronUp size={12} /> : col.sortDir === "desc" ? <ChevronDown size={12} /> : <ChevronsUpDown size={12} className="text-slate-300" />}
    </span>
  );
}

export function Pagination({ page, pages, total, pageSize, onPage, onPageSize }) {
  const { t } = useApp();
  return (
    <div className="flex items-center justify-between border-t border-slate-200 px-3 py-2">
      <div className="text-xs text-slate-500">
        {t("共")} <span className="tnum">{total}</span> {t("条")} · {t("第")} <span className="tnum">{page}/{pages}</span> {t("页")}
      </div>
      <div className="flex items-center gap-2">
        <select value={pageSize} onChange={(e) => onPageSize?.(Number(e.target.value))} className="input w-20 px-1.5 py-1 text-xs">
          {[10, 20, 50, 100].map((n) => <option key={n} value={n}>{n}{t("条/页")}</option>)}
        </select>
        <button disabled={page <= 1} onClick={() => onPage?.(page - 1)} className="btn border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-40"><ChevronLeft size={13} /></button>
        <button disabled={page >= pages} onClick={() => onPage?.(page + 1)} className="btn border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:opacity-40"><ChevronRight size={13} /></button>
      </div>
    </div>
  );
}

// ---------------- 指标卡 ---------------- //
export function StatCard({ label, value, sub, trend, trendDir, icon, onClick, loading }) {
  const { t } = useApp();
  const trendColor = trendDir === "up" ? "text-emerald-600" : trendDir === "down" ? "text-red-600" : "text-slate-400";
  return (
    <Card onClick={onClick} role={onClick ? "button" : undefined} tabIndex={onClick ? 0 : undefined} onKeyDown={onClick ? (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onClick(); } } : undefined} className={`panel ops-stat ${onClick ? "cursor-pointer" : ""}`}>
      <div className="flex items-start justify-between">
        <div>
          <div className="ops-stat-label">{typeof label === "string" ? t(label) : label}</div>
          <div className="tnum ops-stat-value">
            {loading ? <Loader2 size={20} className="animate-spin text-slate-300" /> : value}
          </div>
        </div>
        {icon && <div className="ops-stat-icon">{icon}</div>}
      </div>
      <div className="ops-stat-footer">
        {trend != null && <span className={`tnum font-medium ${trendColor}`}>{trendDir === "up" ? "↑" : "↓"} {trend}%</span>}
        {sub && <span>{typeof sub === "string" ? t(sub) : sub}</span>}
      </div>
    </Card>
  );
}

// ---------------- Toast ---------------- //
export function ToastHost() {
  const { toasts } = useApp();
  const iconMap = {
    success: <span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500 text-[11px] font-bold text-white">✓</span>,
    error: <span className="flex h-5 w-5 items-center justify-center rounded-full bg-red-500 text-[11px] font-bold text-white">✕</span>,
    warning: <span className="flex h-5 w-5 items-center justify-center rounded-full bg-amber-500 text-[11px] font-bold text-white">!</span>,
    info: <span className="flex h-5 w-5 items-center justify-center rounded-full bg-blue-500 text-[11px] font-bold text-white">i</span>,
  };
  return (
    <div className="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div key={t.id}
          className="pointer-events-auto flex items-start gap-2.5 rounded-md border border-slate-200 bg-white px-3 py-2.5 shadow-lg">
          {iconMap[t.type]}
          <div className="min-w-0">
            <div className="text-[13px] text-slate-800">{t.message}</div>
            {t.detail && <div className="mt-0.5 break-all text-[11px] text-slate-400">{t.detail}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------- 通用 hook：模拟异步 ---------------- //
export function useAsyncData(fetcher, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError("");
    const timer = setTimeout(async () => {
      try {
        const res = await fetcher();
        if (alive) { setData(res); setLoading(false); }
      } catch (e) {
        if (alive) { setError(e?.message || String(e)); setLoading(false); }
      }
    }, 350);
    return () => { alive = false; clearTimeout(timer); };
    // Legacy callers explicitly control when their fetcher is refreshed.
  }, [...deps, tick]);

  return { data, setData, loading, error, reload: () => setTick((t) => t + 1) };
}

// ---------------- 页面头部 ---------------- //
export function PageHeader({ title, subtitle, extra, badge }) {
  const { t } = useApp();
  return (
    <div className="ops-page-header">
      <div className="flex items-center gap-2.5">
        <div>
          <div className="flex items-center gap-2">
            <h1>{typeof title === "string" ? t(title) : title}</h1>
            {badge}
          </div>
          {subtitle && <p>{typeof subtitle === "string" ? t(subtitle) : subtitle}</p>}
        </div>
      </div>
      {extra && <div className="ops-page-header-actions">{extra}</div>}
    </div>
  );
}

// ---------------- 风险提示条 ---------------- //
const RISK_STYLE = {
  danger: "border-red-200 bg-red-50 text-red-800",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-blue-200 bg-blue-50 text-blue-800",
};
const RISK_ICON = { danger: XCircle, warning: AlertTriangle, info: ShieldAlert };
export function RiskBanner({ level = "warning", title, children, onAction, actionText, className = "" }) {
  const { t } = useApp();
  const Icon = RISK_ICON[level] || AlertTriangle;
  return (
    <div className={`flex items-start gap-2.5 rounded-md border px-3 py-2.5 text-[13px] ${RISK_STYLE[level]} ${className}`}>
      <Icon size={15} className="mt-0.5 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{typeof title === "string" ? t(title) : title}</div>
        {children && <div className="mt-0.5 text-xs opacity-80">{children}</div>}
      </div>
      {onAction && <Button variant="ghost" size="sm" className="shrink-0 text-inherit" onClick={onAction}>{actionText || "处理"}</Button>}
    </div>
  );
}

// ---------------- 敏感字段：默认脱敏 / 受控显示 / 自动隐藏 / 复制审计 ---------------- //
function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  const ok = document.execCommand("copy");
  document.body.removeChild(ta);
  return ok;
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  if (!fallbackCopy(text)) throw new Error("浏览器拒绝了剪贴板写入");
}

export function SecretField({
  value,           // 明文值
  mask,            // 脱敏展示值（缺省时用占位符）
  label = "敏感字段",
  kind = "password", // password | token | totp | generic
  autoHideMs = 30000,
  copyable = true,
  confirmLevel = "simple", // none | simple（二次点击确认） | hard（输入确认跳过，二次点击）
  onReveal,        // 显示时的回调（可写审计）
  onCopyAudit,     // 复制时回调（可写审计）
  className = "",
}) {
  const { toast } = useApp();
  const [revealed, setRevealed] = useState(false);
  const [armed, setArmed] = useState(false); // 二次确认第一步
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(null);

  const stopTimer = () => { if (timerRef.current) { clearTimeout(timerRef.current); timerRef.current = null; } };
  useEffect(() => () => stopTimer(), []);

  const toggleReveal = () => {
    if (!revealed) {
      if (confirmLevel === "simple" && !armed) {
        setArmed(true);
        setTimeout(() => setArmed(false), 2500);
        return;
      }
      setRevealed(true);
      setArmed(false);
      onReveal?.();
      if (autoHideMs > 0) {
        stopTimer();
        timerRef.current = setTimeout(() => { setRevealed(false); stopTimer(); }, autoHideMs);
      }
    } else {
      setRevealed(false);
      stopTimer();
    }
  };

  const doCopy = async (e) => {
    e?.stopPropagation?.();
    if (!value) return;
    try {
      await copyText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
      onCopyAudit?.();
      toast(`已复制 ${label}`, "success");
    } catch (err) {
      toast(`复制失败: ${err.message}`, "error");
    }
  };

  const placeholder = mask || "••••••••";
  const kindColor = kind === "token" ? "text-violet-700" : kind === "totp" ? "text-emerald-700" : kind === "password" ? "text-slate-700" : "text-slate-700";

  return (
    <div className={`group flex min-w-0 items-center gap-1.5 ${className}`}>
      <span className={`mono truncate ${revealed ? kindColor : "text-slate-500"}`} title={revealed ? undefined : label}>
        {revealed ? value || "—" : placeholder}
      </span>
      {value && (
        <>
          <button
            title={revealed ? "隐藏" : armed ? "再次点击确认显示" : "显示明文"}
            onClick={(e) => { e.stopPropagation(); toggleReveal(); }}
            className={`shrink-0 text-slate-400 transition-colors hover:text-slate-700 ${armed ? "text-amber-500" : ""}`}>
            {revealed ? <EyeOff size={12} /> : armed ? <ShieldAlert size={12} /> : <Eye size={12} />}
          </button>
          {copyable && (
            <button title={`复制 ${label}`} onClick={doCopy}
              className={`shrink-0 text-slate-400 transition-colors hover:text-slate-700 ${copied ? "text-emerald-500" : ""}`}>
              {copied ? <Check size={12} /> : <Copy size={12} />}
            </button>
          )}
        </>
      )}
    </div>
  );
}

// ---------------- 注册阶段 Stepper ---------------- //
export function StageStepper({ stages, current = -1, status = "pending", compact }) {
  // status: pending | running | success | failed
  const size = compact ? 16 : 22;
  const doneUpTo = status === "success" ? stages.length : current;
  return (
    <div className="flex items-start">
      {stages.map((s, i) => {
        const done = i < doneUpTo;
        const isCurrent = i === current && (status === "running" || status === "pending");
        const failed = status === "failed" && i === current;
        return (
          <div key={s.key} className={`flex items-start ${i < stages.length - 1 ? "flex-1" : ""}`}>
            <div className="flex flex-col items-center" style={{ width: compact ? 44 : 56 }}>
              <div className={`flex items-center justify-center rounded-full transition-colors ${
                done ? "bg-emerald-500 text-white"
                : failed ? "bg-red-500 text-white"
                : isCurrent ? "bg-blue-600 text-white ring-4 ring-blue-100"
                : "bg-slate-100 text-slate-400"
              }`} style={{ width: size, height: size }}>
                {done ? <Check size={compact ? 10 : 13} />
                  : failed ? <XCircle size={compact ? 11 : 14} />
                  : isCurrent ? <Loader2 size={compact ? 11 : 13} className="animate-spin" />
                  : <span className="text-[10px] font-semibold">{i + 1}</span>}
              </div>
              <div className={`mt-1 text-center leading-tight ${isCurrent ? "font-medium text-blue-700" : done ? "text-slate-600" : "text-slate-400"} ${compact ? "text-[10px]" : "text-[11px]"}`}>
                {s.label}
              </div>
            </div>
            {i < stages.length - 1 && (
              <div style={{ marginTop: size / 2 - 1 }} className={`h-px flex-1 ${done ? "bg-emerald-400" : "bg-slate-200"}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------- 时间线 ---------------- //
const TL_DOT = { danger: "bg-red-500", warning: "bg-amber-500", success: "bg-emerald-500", info: "bg-blue-500", neutral: "bg-slate-400" };
export function Timeline({ items, renderTime, empty = "暂无记录" }) {
  if (!items || items.length === 0) return <Empty title={empty} />;
  return (
    <div className="relative pl-5">
      {items.map((it, i) => (
        <div key={i} className="relative pb-4 last:pb-0">
          {i < items.length - 1 && <span className="absolute left-[5px] top-4 h-full w-px bg-slate-100" />}
          <span className={`absolute -left-[15px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-white ${TL_DOT[it.level] || TL_DOT.info}`} />
          <div className="text-[12.5px] text-slate-700">{it.text}</div>
          {renderTime && <div className="mt-0.5 text-[11px] text-slate-400">{renderTime(it)}</div>}
        </div>
      ))}
    </div>
  );
}

// ---------------- 状态徽章（状态词 → 颜色） ---------------- //
const STATUS_META = {
  active: ["success", "可用"], healthy: ["success", "健康"], success: ["success", "成功"], ok: ["success", "正常"],
  cooling: ["warning", "冷却中"], pending: ["neutral", "待执行"], running: ["info", "运行中"], warning: ["warning", "告警"],
  risky: ["warning", "有风险"], paused: ["neutral", "已暂停"], expired: ["neutral", "已过期"], unknown: ["neutral", "未知"],
  failed: ["danger", "失败"], unhealthy: ["danger", "异常"], offline: ["danger", "离线"], released: ["neutral", "已释放"],
};
export function StatusBadge({ status, label, dot = true, className = "" }) {
  const meta = STATUS_META[status] || ["neutral", label || status];
  return <Badge color={meta[0]} dot={dot} className={className}>{meta[1]}</Badge>;
}
