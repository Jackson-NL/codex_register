import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw, Loader2, StopCircle, ChevronDown, ChevronUp } from "lucide-react";
import { Panel, Button, Badge, ProgressBar, Modal } from "../components/ui";
import { api } from "../api";

// ============================================================
// 账号信息刷新面板：单账号 / 选中 / 全部 批量刷新 + 进度轮询 + 停止
// 只处理「远端身份/套餐/额度 + 凭据续期」，与「刷新列表」（重读本地）区分。
// 敏感凭据（AT/RT/ID Token）不进入本组件任何状态或展示。
// ============================================================

interface RefreshItem {
  account_id: number;
  status: string;
  error_code: string;
  error: string;
  credential_rotated: boolean;
  plan_type: string;
  quota_status: string;
  quota_label: string;
  quota_value: number | null;
  quota_kind: string;
  restore_at: string;
  email: string;
  identity_matched: boolean;
}

interface RefreshJob {
  id: string;
  scope: string;
  status: string;
  created_at: string;
  total: number;
  processed: number;
  succeeded: number;
  partial: number;
  failed: number;
  canceled: number;
  stop_requested: boolean;
  items: RefreshItem[];
  _lost?: boolean;
}

interface ConfirmPayload {
  scope: "selected" | "all";
  ids: number[];
  count: number;
  title: string;
  note: string;
}

type Toast = (message: string, level?: string) => void;

interface Props {
  selectedIds: number[];
  allTotal: number;
  filteredCount: number;
  onRefreshed: () => void;
  registerSingleRefresh: (fn: (id: number) => Promise<void>, busyId: number | null) => void;
  toast: Toast;
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: "等待", color: "neutral" },
  running: { label: "刷新中", color: "info" },
  succeeded: { label: "成功", color: "success" },
  partial: { label: "部分成功", color: "warning" },
  failed: { label: "失败", color: "danger" },
  canceled: { label: "已跳过", color: "neutral" },
};

const QUOTA_TEXT = (item: RefreshItem): string => {
  if (item.quota_status === "ok") {
    const kind = item.quota_kind === "image_gen" ? "图像额度" : "额度";
    return `${kind} ${item.quota_value}`;
  }
  if (item.quota_status === "failed") return "查询失败";
  return "未知"; // unknown：上游未提供，与 0 明确区分
};

const JOB_TERMINAL = new Set(["succeeded", "partial", "failed", "canceled"]);

export default function AccountsRefreshPanel({
  selectedIds,
  allTotal,
  filteredCount,
  onRefreshed,
  registerSingleRefresh,
  toast,
}: Props) {
  const [job, setJob] = useState<RefreshJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [singleBusyId, setSingleBusyId] = useState<number | null>(null);
  // 同步互斥用 ref：闭包里读 state 会因渲染时序过期，导致并发漏拦。
  const singleBusyRef = useRef<number | null>(null);
  const [confirm, setConfirm] = useState<ConfirmPayload | null>(null);
  const [expanded, setExpanded] = useState(false);
  const pollRef = useRef<number | null>(null);
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (pollRef.current) window.clearTimeout(pollRef.current);
    };
  }, []);

  // 页面重新进入：恢复服务端仍在跑的任务
  useEffect(() => {
    let alive = true;
    api.accounts
      .refreshJobActive()
      .then((payload: { job: RefreshJob | null }) => {
        if (alive && payload?.job) setJob(payload.job);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  const startPolling = (jobId: string) => {
    if (pollRef.current) window.clearTimeout(pollRef.current);
    const tick = async () => {
      if (!aliveRef.current) return;
      try {
        const snapshot = await api.accounts.refreshJob(jobId);
        if (!aliveRef.current) return;
        setJob(snapshot);
        if (!JOB_TERMINAL.has(snapshot.status)) {
          pollRef.current = window.setTimeout(tick, 1200);
        } else {
          onRefreshed();
        }
      } catch {
        // 404 = 服务重启导致任务丢失；按"未完成，请重新发起"处理
        if (aliveRef.current) {
          setJob((prev) => (prev ? { ...prev, status: "failed", _lost: true } : prev));
          onRefreshed();
        }
      }
    };
    pollRef.current = window.setTimeout(tick, 400);
  };

  const startJob = async (payload: ConfirmPayload | null) => {
    if (!payload) return;
    setConfirm(null);
    setStarting(true);
    try {
      const snapshot = await api.accounts.refreshStart(payload.ids, payload.scope);
      setJob(snapshot);
      startPolling(snapshot.id);
      toast(`${payload.title}已开始，共 ${payload.count} 个账号`, "info");
    } catch (error) {
      toast(`启动刷新失败: ${(error as Error).message}`, "error");
    } finally {
      setStarting(false);
    }
  };

  const refreshSelected = () => {
    const ids = [...new Set(selectedIds)];
    if (!ids.length) {
      toast("请先选择账号", "warning");
      return;
    }
    setConfirm({
      scope: "selected",
      ids,
      count: ids.length,
      title: "刷新选中账号",
      note: `将向外部 ChatGPT 服务发起 ${ids.length} 个账号的信息与额度查询，可能触发凭据续期。`,
    });
  };

  const refreshAll = () => {
    // 「全部」提交 scope=all，由服务端确定真实全集（含当前筛选之外的账号）。
    setConfirm({
      scope: "all",
      ids: [],
      count: allTotal,
      title: "刷新全部账号",
      note: `将对服务端全部 ${allTotal} 个账号发起外部查询（不是仅当前筛选的 ${filteredCount} 个），耗时较长。`,
    });
  };

  const stopJob = async () => {
    if (!job || JOB_TERMINAL.has(job.status)) return;
    try {
      const snapshot = await api.accounts.refreshCancel(job.id);
      setJob(snapshot);
      toast("已请求停止：停止派发后续账号，已完成的刷新结果保留", "info");
    } catch (error) {
      toast(`停止失败: ${(error as Error).message}`, "error");
    }
  };

  const refreshSingle = async (accountId: number) => {
    if (singleBusyRef.current !== null) return; // 防重复提交（ref 保证跨渲染时序正确）
    singleBusyRef.current = accountId;
    setSingleBusyId(accountId);
    try {
      await api.accounts.refreshSingle(accountId);
      toast(`账号 ${accountId} 刷新完成`, "success");
      onRefreshed();
    } catch (error) {
      toast(`账号 ${accountId} 刷新失败: ${(error as Error).message}`, "error");
    } finally {
      singleBusyRef.current = null;
      setSingleBusyId(null);
    }
  };

  // 暴露给父组件：行内「刷新账号」入口
  useEffect(() => {
    registerSingleRefresh(refreshSingle, singleBusyId);
  });

  const running = !!job && !JOB_TERMINAL.has(job?.status ?? "");
  const progress = useMemo(() => {
    if (!job) return 0;
    return job.total ? Math.round((job.processed / job.total) * 100) : 0;
  }, [job]);

  return (
    <>
      <Panel pad={false} className="overflow-hidden shadow-sm" title="" extra={null}>
        <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
          <RefreshCw size={14} className="text-slate-400" />
          <span className="text-[13px] font-medium text-slate-700 dark:text-slate-200">
            账号信息刷新
          </span>
          <span className="text-xs text-slate-400">查询远端身份、套餐与额度；必要时用 RT 续期凭据</span>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <Button size="sm" variant="secondary" icon={<RefreshCw size={13} />} onClick={refreshSelected} disabled={starting || running}>
              刷新选中账号{selectedIds.length ? ` (${selectedIds.length})` : ""}
            </Button>
            <Button size="sm" variant="secondary" icon={<RefreshCw size={13} />} onClick={refreshAll} disabled={starting || running || !allTotal}>
              刷新全部 ({allTotal})
            </Button>
            {running && (
              <Button size="sm" variant="danger" icon={<StopCircle size={13} />} onClick={stopJob}>
                停止后续处理
              </Button>
            )}
          </div>
        </div>

        {job && (
          <div className="border-t border-slate-100 px-3 py-2.5 dark:border-slate-700">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Badge color={running ? "info" : STATUS_META[job.status]?.color || "neutral"} dot>
                {job._lost ? "任务已丢失(服务重启)" : STATUS_META[job.status]?.label || job.status}
              </Badge>
              <span className="text-slate-500 dark:text-slate-400 tnum">
                总数 {job.total} · 已处理 {job.processed} · 成功 {job.succeeded} · 部分成功 {job.partial} · 失败 {job.failed} · 跳过 {job.canceled}
              </span>
              {job.scope === "all" && <Badge color="warning" dot>全部账号（服务端全集）</Badge>}
              {job.stop_requested && !JOB_TERMINAL.has(job.status) && (
                <span className="text-amber-600">停止中：等待在途账号收尾，已完成结果保留</span>
              )}
              <button
                className="ml-auto text-slate-400 hover:text-slate-600"
                onClick={() => setExpanded((v) => !v)}
                aria-label="切换逐账号结果"
              >
                {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
            </div>
            <div className="mt-1.5">
              <ProgressBar value={progress} showText />
            </div>
            {expanded && job.items?.length > 0 && (
              <div className="mt-2 max-h-52 overflow-y-auto rounded border border-slate-100 dark:border-slate-700">
                <table className="w-full text-xs">
                  <thead className="bg-slate-50 text-slate-400 dark:bg-slate-800">
                    <tr>
                      <th className="px-2 py-1 text-left font-normal">账号</th>
                      <th className="px-2 py-1 text-left font-normal">结果</th>
                      <th className="px-2 py-1 text-left font-normal">套餐</th>
                      <th className="px-2 py-1 text-left font-normal">额度</th>
                      <th className="px-2 py-1 text-left font-normal">说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    {job.items.map((item: RefreshItem) => (
                      <tr key={item.account_id} className="border-t border-slate-100 dark:border-slate-700">
                        <td className="px-2 py-1 tnum">acc_{item.account_id}</td>
                        <td className="px-2 py-1">
                          <Badge color={STATUS_META[item.status]?.color || "neutral"} dot>
                            {STATUS_META[item.status]?.label || item.status}
                          </Badge>
                        </td>
                        <td className="px-2 py-1">{item.plan_type || "—"}</td>
                        <td className="px-2 py-1">{item.status === "pending" || item.status === "running" ? "—" : QUOTA_TEXT(item)}</td>
                        <td className="px-2 py-1 text-slate-500 dark:text-slate-400">
                          {item.credential_rotated ? "凭据已续期；" : ""}
                          {item.error || (item.quota_status === "unknown" ? "上游未返回额度" : "")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </Panel>

      <Modal
        open={!!confirm}
        onClose={() => !starting && setConfirm(null)}
        title={confirm?.title || "确认刷新"}
        width={480}
        footer={
          <>
            <Button variant="secondary" onClick={() => setConfirm(null)} disabled={starting}>取消</Button>
            <Button
              icon={starting ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
              onClick={() => startJob(confirm)}
              loading={starting}
            >
              开始刷新
            </Button>
          </>
        }>
        <div className="space-y-3 text-[13px] leading-relaxed">
          <div className="rounded-md border border-amber-100 bg-amber-50/70 p-3 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
            {confirm?.note}
          </div>
          <p className="text-slate-500 dark:text-slate-400 text-xs">
            刷新会向上游查询账号身份与额度；access token 失效且账号有 refresh token 时会自动续期并写回数据库。
            已轮换的新凭据即使后续额度查询失败也会保留。失败不会删除账号或清空原凭据。
          </p>
        </div>
      </Modal>
    </>
  );
}
