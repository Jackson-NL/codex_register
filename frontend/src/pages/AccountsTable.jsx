// 账号管理页数据表（从 Accounts.jsx 拆出）
// 列布局优化：计划与标签合并为单列、凭据紧凑单列（AT/RT 两行合为一行带状态点）、行高更紧凑
import { DataTable, Badge } from "../components/ui";
import {
  PLAN_COLOR, accStatus, credState, tokenExpiryText,
  CredentialLine, CopyBtn, shortValue,
} from "./accountsShared";
import { fmtAgo } from "../mock/data";
import { sub2apiUploadBadge, normalizeSub2APIUploadSummary } from "./sub2apiUtils";
import { RefreshCw, MoreHorizontal, FileText, Pause, Play, Trash2, FileJson, KeyRound, Loader2 } from "lucide-react";
import { Dropdown, IconBtn } from "../components/ui";

// 紧凑凭据列：一行放 AT+RT 状态（带点色），第二行过期时间；点击单元格展开完整两行 CredentialLine
function CompactCredentialCell({ a, copyState, onCopy }) {
  const meta = credState(a);
  const expired = a.token_expires_at && new Date(a.token_expires_at).getTime() <= Date.now();
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <span className={`inline-flex items-center gap-1 rounded px-1 py-0.5 text-[10px] font-semibold ${a.has_access_token ? "bg-blue-50 text-blue-600" : "bg-slate-100 text-slate-400"}`}>AT</span>
        <span className={`mono min-w-0 flex-1 truncate text-[11px] ${a.has_access_token ? "text-slate-600" : "text-slate-300"}`} title={a.access_token_masked || "AT 缺失"}>
          {a.access_token_masked || "—"}
        </span>
        {a.has_access_token && <CopyBtn state={copyState[`${a.id}:at`]} title="复制 Access Token" onCopy={() => onCopy(a, "at")} />}
      </div>
      <div className="flex items-center gap-2">
        <span className={`inline-flex items-center gap-1 rounded px-1 py-0.5 text-[10px] font-semibold ${a.has_refresh_token ? "bg-emerald-50 text-emerald-600" : "bg-slate-100 text-slate-400"}`}>RT</span>
        <span className={`mono min-w-0 flex-1 truncate text-[11px] ${a.has_refresh_token ? "text-slate-600" : "text-slate-300"}`} title={a.refresh_token_masked || "RT 缺失"}>
          {a.refresh_token_masked || "—"}
        </span>
        {a.has_refresh_token && <CopyBtn state={copyState[`${a.id}:rt`]} title="复制 Refresh Token" onCopy={() => onCopy(a, "rt")} />}
      </div>
      <div className={`text-[11px] ${expired ? "text-red-500" : "text-slate-400"}`}>
        {tokenExpiryText(a)}
      </div>
    </div>
  );
}

export function buildColumns({
  copyState, copyToken, refreshSingleBusyId, refreshSingleRef,
  openSub2apiDetail, openDetail, setEditAccount, setSelected, setBatchAction, setConfirmDelete,
}) {
  const cols = [
    {
      key: "email", title: "账号", width: 260, render: (a) => {
        const status = accStatus(a);
        return (
          <div className="min-w-0 py-0.5">
            <div className="flex min-w-0 items-center gap-2">
              <span className="mono truncate text-[13px] font-semibold text-slate-800" title={a.email || a.phone || `acc_${a.id}`}>
                {a.email || a.phone || "—"}
              </span>
              <Badge color={status[0]} dot className="shrink-0">{status[1]}</Badge>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
              <span className="mono">acc_{a.id}</span>
              {a.phone && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">手机</span>}
              {a.profile_path ? <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-600">Profile 已绑定</span> : <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">缺 Profile</span>}
            </div>
          </div>
        );
      },
    },
    {
      key: "plan", title: "计划 / 标签", width: 150, render: (a) => (
        <div className="flex flex-wrap items-center gap-1">
          <Badge color={PLAN_COLOR[a.plan_type] || "neutral"}>{a.plan_type || "—"}</Badge>
          {a.tag && <Badge color="info">{a.tag}</Badge>}
        </div>
      ),
    },
    { key: "credentialStatus", title: "凭据状态", width: 110, sortable: true, render: (a) => {
      const meta = credState(a);
      return <Badge color={meta.color} dot>{meta.label}</Badge>;
    } },
    {
      key: "cred", title: "OAuth 凭据", width: 250, render: (a) => (
        <CompactCredentialCell a={a} copyState={copyState} onCopy={copyToken} />
      ),
    },
    { key: "totp", title: "2FA", width: 140, render: (a) => a.totp_secret_masked ? (
      <div className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-slate-200 bg-white px-2 py-1">
        <KeyRound size={12} className="shrink-0 text-slate-400" />
        <span className="mono truncate text-slate-700" title="TOTP Secret 已保存（脱敏）">{a.totp_secret_masked}</span>
        <CopyBtn state={copyState[`${a.id}:totp`]} title="复制 TOTP Secret" onCopy={() => copyToken(a, "totp")} />
      </div>
    ) : <Badge color="neutral">未绑定</Badge> },
    {
      key: "sub2api", title: "Sub2API", width: 140, render: (a) => {
        const badge = sub2apiUploadBadge(a.sub2api_upload_summary);
        return (
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); openSub2apiDetail(a); }}
              title={badge.status === "not_uploaded" ? "尚未上传到 Sub2API（点击查看明细）" : (normalizeSub2APIUploadSummary(a.sub2api_upload_summary).last_error || "点击查看各分组上传明细")}
            >
              <Badge color={badge.color}>{badge.label}</Badge>
            </button>
            <IconBtn icon={<FileJson size={13} />} title="Sub2API 上传明细" onClick={(e) => { e.stopPropagation(); openSub2apiDetail(a); }} />
          </div>
        );
      },
    },
    { key: "created_at", title: "创建", width: 110, sortable: true, render: (a) => <span className="text-xs text-slate-500">{fmtAgo(new Date(a.created_at).getTime())}</span> },
  ];
  const ACTION_COL = {
    key: "_actions",
    title: "操作",
    width: 92,
    render: (a) => (
      <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
        <IconBtn
          icon={refreshSingleBusyId === a.id ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
          title="刷新账号信息与额度"
          onClick={() => refreshSingleRef.current?.(a.id)}
        />
        <Dropdown trigger={<IconBtn icon={<MoreHorizontal size={14} />} title="更多操作" />} items={[
          { label: "编辑元数据", icon: <FileText size={13} />, onClick: () => setEditAccount(a) },
          { label: a.status === "paused" ? "恢复账号" : "暂停账号", danger: a.status !== "paused",
            icon: a.status === "paused" ? <Play size={13} /> : <Pause size={13} />,
            onClick: () => { setSelected([a.id]); setBatchAction(a.status === "paused" ? "resume" : "pause"); } },
          { divider: true },
          { label: "删除账号", danger: true, icon: <Trash2 size={13} />, onClick: () => setConfirmDelete({ kind: "single", account: a }) },
        ]} />
      </div>
    ),
  };
  return { cols, ACTION_COL };
}

export function AccountsTable({
  columns, pageRows, loading, error, reload,
  selected, onSelectAll, onSelectRow, rowClick,
  page, pageSize, total, onPage, onPageSize,
  rowsCount, filters, q, onReloadList, t,
}) {
  return (
    <DataTable
      columns={columns}
      data={pageRows}
      loading={loading}
      error={error}
      onRetry={reload}
      selectable
      selected={selected}
      onSelectAll={(v) => onSelectAll(v)}
      onSelectRow={(id, v) => onSelectRow(id, v)}
      page={page} pageSize={pageSize} total={total}
      onPage={onPage} onPageSize={onPageSize}
      rowClick={rowClick}
      emptyTitle="还没有账号，去创建注册任务"
      toolbar={
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50/70 px-3 py-2">
          <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            <span>{t(`共 ${rowsCount} 个账号`)}</span>
            {filters.cred !== "all" && <Badge color="info">凭据筛选中</Badge>}
            {filters.rt !== "all" && <Badge color="info">{filters.rt === "with_rt" ? "有 RT" : "无 RT"}筛选中</Badge>}
            {filters.tag !== "all" && <Badge color="info">{filters.tag === "none" ? "无标签" : `标签：${filters.tag}`}</Badge>}
            {q && <Badge color="neutral">搜索：{q}</Badge>}
          </div>
          <div className="flex items-center gap-1.5">
            <button variant="ghost" size="sm" className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-slate-500 hover:bg-slate-100"
              onClick={onReloadList} title="重新读取本地账号列表，不发起外部请求">
              <RefreshCw size={13} /> 刷新列表
            </button>
          </div>
        </div>
      }
    />
  );
}
