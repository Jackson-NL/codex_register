// 账号管理页筛选工具栏 + 批量操作条（从 Accounts.jsx 拆出）
// 增强：筛选条件 chip 化可单独移除；保存视图持久化到 localStorage
import { useMemo } from "react";
import {
  Pause, Play, Tag, RefreshCw, Columns3, Save, X, FileJson,
  Download, Upload,
} from "lucide-react";
import {
  Panel, Button, Select, SearchInput, Checkbox, Dropdown, Badge,
} from "../components/ui";
import { FILTER_DEFS } from "./accountsShared";

export function AccountsToolbar({
  q, setQ, filters, setFilter, tagOptions,
  visibleCols, setVisibleCols, columns, savedViews, onSaveView, onApplyView,
  onSub2apiSync, onImport, onExport, onSub2apiUpload,
}) {
  // 汇总所有非 all 的筛选条件为 chip
  const activeChips = useMemo(() => {
    const chips = [];
    for (const [key, def] of Object.entries(FILTER_DEFS)) {
      const v = filters[key];
      if (v && v !== "all") {
        const opt = def.find((o) => o.value === v);
        chips.push({ key, value: v, label: `${def[0].label.replace("全部", "")}：${opt?.label || v}` });
      }
    }
    if (filters.tag && filters.tag !== "all") {
      chips.push({ key: "tag", value: filters.tag, label: `标签：${filters.tag === "none" ? "无标签" : filters.tag}` });
    }
    return chips;
  }, [filters]);

  return (
    <Panel pad={false}>
      <div className="flex flex-wrap items-center gap-2 p-3">
        <SearchInput value={q} onChange={setQ} placeholder="检索 ID / 邮箱 / 手机 / 代理…" className="min-w-[260px]" />
        <Select options={FILTER_DEFS.health} value={filters.health} onChange={(v) => setFilter("health", v)} className="w-28" />
        <Select options={FILTER_DEFS.cred} value={filters.cred} onChange={(v) => setFilter("cred", v)} className="w-32" />
        <Select options={FILTER_DEFS.sub2api} value={filters.sub2api} onChange={(v) => setFilter("sub2api", v)} className="w-36" />
        <Select options={FILTER_DEFS.rt} value={filters.rt} onChange={(v) => setFilter("rt", v)} className="w-28" />
        <Select options={[{ value: "all", label: "全部标签" }, { value: "none", label: "无标签" }, ...tagOptions.map((tg) => ({ value: tg, label: tg }))]}
          value={filters.tag} onChange={(v) => setFilter("tag", v)} className="w-36" />
        <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
          <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={onSub2apiSync} title="拉取远端 Sub2API 账号并按邮箱匹配，刷新本地上传状态">同步 Sub2API 状态</Button>
          <Button variant="secondary" size="sm" icon={<Upload size={13} />} onClick={onImport}>导入</Button>
          <Button variant="secondary" size="sm" icon={<Download size={13} />} onClick={onExport}>导出</Button>
          <Button variant="primary" size="sm" icon={<Upload size={13} />} onClick={onSub2apiUpload} title="将所选账号上传到 Sub2API 分组">上传到 Sub2API</Button>
          <Dropdown trigger={<Button variant="secondary" size="sm" icon={<Columns3 size={13} />}>列</Button>}
            items={columns.map((c) => ({
              label: (
                <span className="flex items-center gap-2"><Checkbox checked={visibleCols.includes(c.key)} onChange={(v) => setVisibleCols((cols) => v ? [...cols, c.key] : cols.filter((k) => k !== c.key))} />{c.title}</span>
              ),
              onClick: () => {},
            }))} />
          <Button variant="secondary" size="sm" icon={<Save size={13} />} onClick={onSaveView}>保存视图</Button>
          {savedViews.length > 0 && (
            <Dropdown trigger={<Button variant="secondary" size="sm">视图 ▾</Button>}
              items={savedViews.map((v) => ({ label: v.name, onClick: () => onApplyView(v) }))} />
          )}
        </div>
      </div>

      {/* 筛选 chip 行：单独移除某个条件 */}
      {(activeChips.length > 0 || q) && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-slate-100 px-3 py-2">
          <span className="text-[11px] text-slate-400">筛选条件：</span>
          {q && (
            <Badge color="neutral">
              搜索：{q}
              <button type="button" className="ml-1 inline-flex" aria-label="清除搜索" onClick={() => setQ("")}>
                <X size={10} />
              </button>
            </Badge>
          )}
          {activeChips.map((chip) => (
            <Badge key={chip.key} color="info">
              {chip.label}
              <button type="button" className="ml-1 inline-flex" aria-label={`移除 ${chip.label}`} onClick={() => setFilter(chip.key, "all")}>
                <X size={10} />
              </button>
            </Badge>
          ))}
          <button type="button" className="ml-1 text-[11px] text-slate-400 underline-offset-2 hover:text-slate-600 hover:underline"
            onClick={() => { setQ(""); Object.keys(FILTER_DEFS).forEach((k) => setFilter(k, "all")); setFilter("tag", "all"); }}>
            清空全部
          </button>
        </div>
      )}
    </Panel>
  );
}

export function BatchActionBar({ selected, onBatchAction, onSub2apiUpload, onCancel }) {
  if (!selected.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 bg-blue-50/60 px-3 py-2 text-xs">
      <span className="rounded-full bg-blue-100 px-2 py-1 text-blue-700">已选 <b className="tnum">{selected.length}</b> 个账号</span>
      <Button size="sm" variant="secondary" icon={<Pause size={13} />} onClick={() => onBatchAction("pause")}>暂停</Button>
      <Button size="sm" variant="secondary" icon={<Play size={13} />} onClick={() => onBatchAction("resume")}>恢复</Button>
      <Button size="sm" variant="secondary" icon={<Tag size={13} />} onClick={() => onBatchAction("tag")}>打标签</Button>
      <Button size="sm" variant="secondary" icon={<FileJson size={13} />} onClick={onSub2apiUpload}>上传到 Sub2API</Button>
      <Button size="sm" variant="ghost" className="ml-auto" onClick={onCancel}>取消选择</Button>
    </div>
  );
}
