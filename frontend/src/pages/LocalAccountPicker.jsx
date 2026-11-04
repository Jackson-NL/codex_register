import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge, Button, Checkbox, Input, Modal } from "../components/ui";
import { api } from "../api";
import { accStatus, isHealthy } from "./accountsShared";
import { fmtAgo } from "../mock/data";

// 从本地账号库选号重登。
//
// 设计要点：
// - 正常账号同样可选（用户明确要求），因此不做「只列异常」的过滤；
// - 只把 id 交给后端，密码/TOTP 由服务端从库读取，凭据不经过浏览器；
// - 无密码的账号勾不上（协议登录必须要密码），在行内直接说明原因而不是让任务失败；
// - 已在重登队列中的账号禁用勾选，避免同一账号并发登录。

const PAGE_SIZE = 50;

const STATUS_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "normal", label: "正常" },
  { value: "abnormal", label: "异常" },
];

// 不再单独提供「凭据」筛选：状态（health）本身就等于「status 正常 且 凭据可用」，
// 两列同时出现会让用户以为它们是两件不同的事（曾经正是这个错位）。

const RT_OPTIONS = [
  { value: "all", label: "全部 RT" },
  { value: "with_rt", label: "有 RT" },
  { value: "no_rt", label: "无 RT" },
];

function Select({ label, value, onChange, options, disabled }) {
  return (
    <label className="inline-flex items-center gap-1 text-[11px] text-slate-500">
      <span className="shrink-0">{label}</span>
      <select
        className="rounded border border-slate-200 bg-white px-1.5 py-1 text-[11px] text-slate-700"
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}

export default function LocalAccountPicker({ open, onClose, onPicked, busyEmails = new Set(), maxSelectable = 20 }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(() => new Set());
  const [keyword, setKeyword] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [rtFilter, setRtFilter] = useState("all");
  const [tagFilter, setTagFilter] = useState("all");
  const [page, setPage] = useState(1);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const list = await api.accounts.list();
      setRows(Array.isArray(list) ? list : []);
    } catch (failure) {
      setError(failure?.message ? "账号列表加载失败" : "账号列表加载失败");
    } finally { setLoading(false); }
  }, []);

  // 每次打开都重新拉一次：用户可能刚改过账号状态或凭据
  useEffect(() => {
    if (!open) return;
    setSelected(new Set());
    setKeyword("");
    setStatusFilter("all");
    setRtFilter("all");
    setTagFilter("all");
    setPage(1);
    load();
  }, [open, load]);

  const tagOptions = useMemo(() => {
    const tags = new Set();
    rows.forEach((row) => { if (row.tag) tags.add(row.tag); });
    return Array.from(tags).sort();
  }, [rows]);

  // 一个账号能否被选中：有邮箱 + 有密码 + 不在队列里
  const ineligibleReason = useCallback((row) => {
    const email = String(row.email || "").trim().toLowerCase();
    if (!email) return "无邮箱";
    if (!row.has_password) return "本地无密码";
    if (busyEmails.has(email)) return "已在重登队列";
    return "";
  }, [busyEmails]);

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase();
    return rows.filter((row) => {
      if (statusFilter !== "all" && isHealthy(row) !== (statusFilter === "normal")) return false;
      if (rtFilter === "with_rt" && !row.has_refresh_token) return false;
      if (rtFilter === "no_rt" && row.has_refresh_token) return false;
      if (tagFilter !== "all" && (row.tag || "") !== tagFilter) return false;
      if (kw) {
        const haystack = `${row.id} ${row.email || ""} ${row.phone || ""} ${row.tag || ""}`.toLowerCase();
        if (!haystack.includes(kw)) return false;
      }
      return true;
    });
  }, [rows, keyword, statusFilter, rtFilter, tagFilter]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const selectableOnPage = pageRows.filter((row) => !ineligibleReason(row));
  const allOnPageSelected = selectableOnPage.length > 0 && selectableOnPage.every((row) => selected.has(row.id));

  const toggle = (id) => setSelected((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else if (next.size < maxSelectable) next.add(id);
    return next;
  });

  const toggleAllOnPage = () => setSelected((prev) => {
    const next = new Set(prev);
    if (allOnPageSelected) selectableOnPage.forEach((row) => next.delete(row.id));
    else selectableOnPage.forEach((row) => { if (next.size < maxSelectable) next.add(row.id); });
    return next;
  });

  const chosen = filtered.filter((row) => selected.has(row.id));

  const confirm = () => {
    const emails = chosen.map((row) => String(row.email || "").trim().toLowerCase());
    onPicked(emails, chosen.map((row) => row.id));
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="从本地账号选择"
      width={920}
      footer={(
        <div className="flex w-full flex-wrap items-center gap-2">
          <span className="text-[11px] text-slate-500">
            已选 <b className="tnum text-slate-800">{selected.size}</b> / {maxSelectable}
          </span>
          <span className="text-[11px] text-slate-400">共 {filtered.length} 个匹配 · 只传账号 ID，密码由服务端读取</span>
          <span className="ml-auto flex gap-2">
            <Button variant="secondary" size="sm" onClick={onClose}>取消</Button>
            <Button variant="primary" size="sm" disabled={!selected.size} onClick={confirm}>加入重登（{selected.size}）</Button>
          </span>
        </div>
      )}
    >
      <div className="space-y-3">
        {/* 筛选区 */}
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={keyword}
            onChange={(event) => { setKeyword(event.target.value); setPage(1); }}
            placeholder="搜索 ID / 邮箱 / 手机 / 标签"
            className="min-w-[220px] flex-1"
          />
          <Select label="状态" value={statusFilter} onChange={(v) => { setStatusFilter(v); setPage(1); }} options={STATUS_OPTIONS} />
          <Select label="RT" value={rtFilter} onChange={(v) => { setRtFilter(v); setPage(1); }} options={RT_OPTIONS} />
          <Select
            label="标签"
            value={tagFilter}
            onChange={(v) => { setTagFilter(v); setPage(1); }}
            options={[{ value: "all", label: "全部标签" }, ...tagOptions.map((tag) => ({ value: tag, label: tag }))]}
          />
          <Button variant="secondary" size="sm" onClick={load} disabled={loading}>{loading ? "加载中…" : "刷新"}</Button>
        </div>

        {error && <div role="alert" className="rounded border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-600">{error}</div>}

        {/* 列表 */}
        <div className="max-h-[420px] overflow-auto rounded border border-slate-200">
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 bg-slate-50 text-left text-[11px] text-slate-500">
              <tr className="border-b border-slate-200">
                <th className="w-9 px-2 py-1.5">
                  <input
                    type="checkbox"
                    aria-label="全选本页可选账号"
                    checked={allOnPageSelected}
                    disabled={!selectableOnPage.length}
                    onChange={toggleAllOnPage}
                  />
                </th>
                <th className="px-2 py-1.5 font-medium">账号</th>
                <th className="px-2 py-1.5 font-medium">状态</th>
                <th className="px-2 py-1.5 font-medium">RT</th>
                <th className="px-2 py-1.5 font-medium">标签</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 && (
                <tr><td colSpan={5} className="px-3 py-8 text-center text-[11px] text-slate-400">{loading ? "加载中…" : "没有匹配的账号"}</td></tr>
              )}
              {pageRows.map((row) => {
                const reason = ineligibleReason(row);
                const status = accStatus(row);
                const disabled = Boolean(reason);
                return (
                  <tr
                    key={row.id}
                    className={`border-b border-slate-100 ${disabled ? "opacity-55" : "hover:bg-slate-50"} ${selected.has(row.id) ? "bg-blue-50/60" : ""}`}
                    onClick={() => { if (!disabled) toggle(row.id); }}
                    style={disabled ? undefined : { cursor: "pointer" }}
                    title={reason || ""}
                  >
                    <td className="px-2 py-1.5" onClick={(event) => event.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`选择 ${row.email || row.id}`}
                        checked={selected.has(row.id)}
                        disabled={disabled}
                        onChange={() => toggle(row.id)}
                      />
                    </td>
                    <td className="px-2 py-1.5">
                      <div className="mono truncate text-slate-700" title={row.email || ""}>{row.email || "—"}</div>
                      <div className="text-[10px] text-slate-400">
                        acc_{row.id}
                        {reason ? <span className="ml-1 text-amber-600">· {reason}</span> : null}
                      </div>
                    </td>
                    <td className="px-2 py-1.5"><Badge color={status[0]} dot>{status[1]}</Badge></td>
                    <td className="px-2 py-1.5 text-[11px] text-slate-500">{row.has_refresh_token ? "有" : "无"}</td>
                    <td className="px-2 py-1.5 text-[11px] text-slate-500">{row.tag || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* 分页 */}
        <div className="flex items-center justify-between text-[11px] text-slate-500">
          <span>第 {currentPage} / {pageCount} 页</span>
          <span className="flex gap-1">
            <Button variant="secondary" size="sm" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}>上一页</Button>
            <Button variant="secondary" size="sm" disabled={currentPage >= pageCount} onClick={() => setPage(currentPage + 1)}>下一页</Button>
          </span>
        </div>
      </div>
    </Modal>
  );
}
