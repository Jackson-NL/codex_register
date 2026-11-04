// 账号管理主编排组件（已拆分：accountsShared / AccountsToolbar / AccountsTable / AccountsDetailDrawer / AccountsRefreshPanel）
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  ShieldCheck, KeyRound, FileText, Trash2,
  Loader2, Upload, FileJson, Pause, Play, Tag, RefreshCw,
} from "lucide-react";
import {
  Panel, Button, Badge, Input, Modal, Confirm, useAsyncData,
} from "../components/ui";
import { useApp } from "../context/AppContext";
import { api } from "../api";
import AccountsRefreshPanel from "./AccountsRefreshPanel";
import { StatPill, loadSavedViews, persistSavedViews, credentialFilter, isNormalStatus } from "./accountsShared";
import { AccountsToolbar, BatchActionBar } from "./AccountsToolbar";
import { buildColumns, AccountsTable } from "./AccountsTable";
import { AccountsDetailDrawer } from "./AccountsDetailDrawer";
import { PLANS, fmtAgo } from "../mock/data";
import {
  SUB2API_UPLOAD_STATUS_META, buildSub2APIUploadRequest, classifySub2APIUploadSelection, normalizeSub2APIGroupIds,
  normalizeSub2APIGroups, normalizeSub2APIUploadJob, normalizeSub2APIUploadSummary, selectSub2APIUploadableIds,
} from "./sub2apiUtils";

// 上传结果中并发设置未同步的账号信息（result 字段不一致，或 errors 里带未同步文案）。
// load_factor 为 None/0 时 Sub2API 会回退到 concurrency，视为可接受，不判未同步。
const sub2apiConcurrencyMismatch = (result) => {
  if (!result) return null;
  const resultMismatch = (result.results || []).find((item) => {
    const remoteConcurrency = Number(item.remote_concurrency);
    const remoteLoadFactor = Number(item.remote_load_factor);
    const concurrencyMismatch = remoteConcurrency !== Number(item.concurrency);
    const loadFactorMismatch = Number.isFinite(remoteLoadFactor) && remoteLoadFactor !== 0 && remoteLoadFactor !== Number(item.concurrency);
    return concurrencyMismatch || loadFactorMismatch;
  });
  if (resultMismatch) {
    return {
      target: resultMismatch.concurrency,
      remoteConcurrency: resultMismatch.remote_concurrency,
      remoteLoadFactor: resultMismatch.remote_load_factor,
    };
  }
  const errorMismatch = (result.errors || []).find((item) => typeof item.error === "string" && item.error.includes("并发设置未同步"));
  if (errorMismatch) {
    const match = errorMismatch.error.match(/远端 concurrency=([^，,]+)[，,]\s*远端 load_factor=([^，,]+)[，,]\s*目标=([^，,]+)/);
    if (match) {
      return { target: match[3], remoteConcurrency: match[1], remoteLoadFactor: match[2] };
    }
  }
  return null;
};

export default function Accounts() {
  const location = useLocation();
  const preset = location.state?.filter || {};
  const { toast, t } = useApp();
  const { data: raw, loading, error, reload } = useAsyncData(() => api.accounts.list());
  const { data: sub2apiSettings } = useAsyncData(() => api.settings.get().catch(() => null));

  const [q, setQ] = useState(preset.q || "");
  const [filters, setFilters] = useState({
    health: preset.health || "all",
    cred: "all",
    plan: "all",
    sub2api: "all",
    rt: "all",
    tag: "all",
  });
  const setFilter = (key, value) => setFilters((f) => ({ ...f, [key]: value }));
  const [visibleCols, setVisibleCols] = useState(["email", "plan", "credentialStatus", "cred", "totp", "sub2api", "created_at"]);
  const [savedViews, setSavedViews] = useState(() => loadSavedViews());
  const [selected, setSelected] = useState([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(10);
  const [sort, setSort] = useState({ key: "", dir: "" });
  const [editAccount, setEditAccount] = useState(null);
  const [batchAction, setBatchAction] = useState(null);
  const [confirmDelete, setConfirmDelete] = useState(null); // { kind: "single", account } | { kind: "batch", ids }
  const [tagInput, setTagInput] = useState("");
  const [savingView, setSavingView] = useState(false);
  const [viewName, setViewName] = useState("");
  const [deleting, setDeleting] = useState(false);
  // 账号详情抽屉
  const [detail, setDetail] = useState(null);
  const [detailTab, setDetailTab] = useState("info");
  const [detailData, setDetailData] = useState(null);
  const [totpInput, setTotpInput] = useState("");
  const [savingTotp, setSavingTotp] = useState(false);
  const [detailBusy, setDetailBusy] = useState(false);
  const [transferModal, setTransferModal] = useState(null); // import | export
  const [transferFormat, setTransferFormat] = useState("cpa");
  const [transferFile, setTransferFile] = useState(null);
  const [transferDedup, setTransferDedup] = useState("skip");
  const [transferBusy, setTransferBusy] = useState(false);
  const transferFileRef = useRef(null);
  const [sub2apiModal, setSub2apiModal] = useState(false);
  const [sub2apiGroups, setSub2apiGroups] = useState([]);
  const [sub2apiGroupIds, setSub2apiGroupIds] = useState([]);
  const [sub2apiGroupIdsInput, setSub2apiGroupIdsInput] = useState("");
  const [sub2apiConcurrency, setSub2apiConcurrency] = useState(3);
  const [sub2apiGroupsLoading, setSub2apiGroupsLoading] = useState(false);
  const [sub2apiGroupsError, setSub2apiGroupsError] = useState("");
  const [sub2apiBusy, setSub2apiBusy] = useState(false);
  const [sub2apiUploadJob, setSub2apiUploadJob] = useState(null);
  const [sub2apiResult, setSub2apiResult] = useState(null);
  const sub2apiSelectionDirty = useRef(false);
  const [sub2apiSyncModal, setSub2apiSyncModal] = useState(false);
  const [sub2apiSyncBusy, setSub2apiSyncBusy] = useState(false);
  const [sub2apiSyncResult, setSub2apiSyncResult] = useState(null);
  const [sub2apiDetailAccount, setSub2apiDetailAccount] = useState(null);
  const [sub2apiDetailRows, setSub2apiDetailRows] = useState([]);
  const [sub2apiDetailLoading, setSub2apiDetailLoading] = useState(false);
  const [sub2apiDetailError, setSub2apiDetailError] = useState("");
  const [sub2apiUploadOptions, setSub2apiUploadOptions] = useState({ onlyNotUploaded: false, overwriteExisting: true, includeTokenError: false });

  // ---------- 账号信息刷新（与「刷新列表」区分） ----------
  const refreshSingleRef = useRef(null); // 由 AccountsRefreshPanel 注册
  const [refreshSingleBusyId, setRefreshSingleBusyId] = useState(null);
  const registerRefreshSingle = (fn, busyId) => {
    refreshSingleRef.current = fn;
    setRefreshSingleBusyId(busyId || null);
  };

  useEffect(() => {
    if (!sub2apiModal && !sub2apiSyncModal) return undefined;
    let alive = true;
    setSub2apiGroupsLoading(true);
    setSub2apiGroupsError("");
    api.sub2api.groups()
      .then((payload) => {
        if (!alive) return;
        const groups = normalizeSub2APIGroups(payload);
        setSub2apiGroups(groups);
        setSub2apiGroupIds((current) => current.length ? current : (groups[0] ? [groups[0].id] : []));
        setSub2apiGroupIdsInput((current) => current || (groups[0] ? String(groups[0].id) : ""));
      })
      .catch((err) => {
        if (alive) setSub2apiGroupsError(err?.message || "Sub2API 分组加载失败");
      })
      .finally(() => {
        if (alive) setSub2apiGroupsLoading(false);
      });
    return () => { alive = false; };
  }, [sub2apiModal, sub2apiSyncModal]);

  useEffect(() => {
    if (!sub2apiModal || !sub2apiSettings || sub2apiSelectionDirty.current) return;
    const configuredGroupIds = normalizeSub2APIGroupIds(sub2apiSettings.sub2api_group_ids);
    if (!configuredGroupIds.length) return;
    setSub2apiGroupIds(configuredGroupIds);
    setSub2apiGroupIdsInput(configuredGroupIds.join(", "));
  }, [sub2apiModal, sub2apiSettings]);

  const rows = useMemo(() => {
    if (!raw) return [];
    let list = raw.filter((a) => {
      if (filters.health !== "all") {
        // 两态判定与徽标同源（isNormalStatus），不再在此重复列一遍底层取值。
        const isNormal = isNormalStatus(a.status);
        if (filters.health === "normal" && !isNormal) return false;
        if (filters.health === "abnormal" && isNormal) return false;
      }
      if (filters.plan !== "all" && a.plan_type !== filters.plan) return false;
      if (filters.cred !== "all" && credentialFilter(a) !== filters.cred) return false;
      if (filters.sub2api !== "all") {
        const s = normalizeSub2APIUploadSummary(a.sub2api_upload_summary);
        if (filters.sub2api === "uploaded" && s.status !== "uploaded") return false;
        if (filters.sub2api === "partial" && s.status !== "partial") return false;
        if (filters.sub2api === "not_uploaded" && s.status !== "not_uploaded") return false;
        if (filters.sub2api === "error" && s.status !== "error") return false;
        if (filters.sub2api === "token_error" && !(s.status === "error" && s.last_error && s.last_error.includes("No access token available"))) return false;
      }
      if (filters.rt === "with_rt" && !a.has_refresh_token) return false;
      if (filters.rt === "no_rt" && a.has_refresh_token) return false;
      if (filters.tag === "none" && a.tag) return false;
      if (filters.tag !== "all" && filters.tag !== "none" && (a.tag || "") !== filters.tag) return false;
      if (q && !`${a.id} ${a.email || ""} ${a.phone || ""} ${a.proxy || ""} ${a.tag || ""}`.toLowerCase().includes(q.toLowerCase())) return false;
      return true;
    });
    if (sort.key) {
      list = [...list].sort((x, y) => {
        const v = (x[sort.key] || "") > (y[sort.key] || "") ? 1 : (x[sort.key] || "") < (y[sort.key] || "") ? -1 : 0;
        return sort.dir === "asc" ? v : -v;
      });
    }
    return list;
  }, [raw, q, filters, sort]);

  const tagOptions = useMemo(() => {
    const tags = new Set();
    (raw || []).forEach((a) => { if (a.tag) tags.add(a.tag); });
    return [...tags].sort();
  }, [raw]);

  const pageRows = useMemo(() => rows.slice((page - 1) * pageSize, page * pageSize), [rows, page, pageSize]);
  const stats = useMemo(() => {
    const list = raw || [];
    const count = (fn) => list.filter(fn).length;
    return {
      total: list.length,
      filtered: rows.length,
      withAt: count((a) => a.has_access_token),
      withRt: count((a) => a.has_refresh_token),
      problem: count((a) => credentialFilter(a) === "problem"),
      missingProfile: count((a) => !a.profile_path),
    };
  }, [raw, rows.length]);
  useEffect(() => setPage(1), [q, filters]);

  const toggleSort = (key) => {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
  };

  const doBatchPause = async (ids) => {
    try {
      await api.accounts.batch({ ids, action: "pause" });
      toast(`已暂停 ${ids.length} 个账号`, "success");
      reload();
    } catch (e) {
      toast(`暂停失败: ${e.message}`, "error");
    }
  };

  const doBatchResume = async (ids) => {
    try {
      await api.accounts.batch({ ids, action: "resume" });
      toast(`已恢复 ${ids.length} 个账号`, "success");
      reload();
    } catch (e) {
      toast(`恢复失败: ${e.message}`, "error");
    }
  };

  const [copyState, setCopyState] = useState({}); // `${id}:at|rt` -> "busy" | "done"
  const tokenCache = useRef(new Map()); // id -> { access_token, refresh_token, totp_secret } 完整凭证缓存
  // 列表刷新后清掉旧缓存，避免复制到过期 token
  useEffect(() => { tokenCache.current.clear(); }, [raw]);

  const writeClipboard = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // 非 secure context（如局域网 IP 访问）降级方案
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (!ok) throw new Error("浏览器拒绝了剪贴板写入");
  };

  const TOKEN_LABEL = { at: "Access Token", rt: "Refresh Token", totp: "TOTP Secret" };

  const copyToken = async (a, which) => {
    const key = `${a.id}:${which}`;
    if (copyState[key] === "busy") return; // 防重复点击
    setCopyState((s) => ({ ...s, [key]: "busy" }));
    try {
      let d = tokenCache.current.get(a.id);
      if (!d) {
        d = await api.accounts.detail(a.id);
        tokenCache.current.set(a.id, d);
      }
      const v = which === "at" ? d.access_token : which === "rt" ? d.refresh_token : d.totp_secret;
      if (!v) { toast(`该账号没有 ${TOKEN_LABEL[which]}`, "info"); return; }
      await writeClipboard(v);
      setCopyState((s) => ({ ...s, [key]: "done" }));
      setTimeout(() => setCopyState((s) => { const n = { ...s }; delete n[key]; return n; }), 1600);
      toast(`已复制 ${TOKEN_LABEL[which]}`, "success");
    } catch (e) {
      toast(`复制失败: ${e.message}`, "error");
    } finally {
      setCopyState((s) => { if (s[key] !== "done") { const n = { ...s }; delete n[key]; return n; } return s; });
    }
  };

  const doDelete = async (id) => {
    setDeleting(true);
    try {
      const res = await api.accounts.del(id);
      toast(`已删除账号 ${id}（${res.deleted}）`, "success");
      reload();
    } catch (e) {
      toast(`删除失败: ${e.message}`, "error");
    } finally {
      setDeleting(false);
    }
  };

  const doBatchDelete = async (ids) => {
    setDeleting(true);
    try {
      const results = await Promise.allSettled(ids.map((id) => api.accounts.del(id)));
      const ok = results.filter((r) => r.status === "fulfilled").length;
      toast(`删除完成: 成功 ${ok} / 失败 ${results.length - ok}`, ok === results.length ? "success" : "warning");
      setSelected([]);
      reload();
    } catch (e) {
      toast(`批量删除失败: ${e.message}`, "error");
    } finally {
      setDeleting(false);
    }
  };

  // 打开账号详情抽屉：拉取完整详情
  const openDetail = async (a) => {
    setDetail(a);
    setDetailTab("info");
    setDetailData(null);
    setTotpInput("");
    setDetailBusy(true);
    try {
      const d = await api.accounts.detail(a.id);
      setDetailData(d);
    } catch (e) {
      toast(`加载详情失败: ${e.message}`, "error");
    }
    setDetailBusy(false);
  };

  // 保存备注（真实 PATCH）
  const saveNote = async () => {
    if (!editAccount) return;
    try {
      await api.accounts.patch(editAccount.id, { note: editAccount.note || "" });
      toast(`已保存备注（acc_${editAccount.id}）`, "success");
      setEditAccount(null);
      reload();
    } catch (e) {
      toast(`保存失败: ${e.message}`, "error");
    }
  };

  // 手工写入 / 清空 TOTP
  const saveTotp = async () => {
    if (!detail) return;
    setSavingTotp(true);
    try {
      const d = await api.accounts.writeTotp(detail.id, totpInput.trim());
      setDetailData(d);
      toast(totpInput.trim() ? "TOTP Secret 已写入" : "TOTP 已清空", "success");
      setTotpInput("");
      reload();
    } catch (e) {
      toast(`写入失败: ${e.message}`, "error");
    } finally {
      setSavingTotp(false);
    }
  };

  const columns = useMemo(() => {
    const { cols, ACTION_COL } = buildColumns({
      copyState, copyToken, refreshSingleBusyId, refreshSingleRef,
      openSub2apiDetail: (a) => openSub2apiDetailRef.current?.(a),
      setEditAccount, setSelected, setBatchAction, setConfirmDelete,
    });
    const visible = cols.filter((c) => visibleCols.includes(c.key)).map((c) =>
      c.sortable ? { ...c, sortDir: sort.key === c.key ? sort.dir : null, onClick: () => toggleSort(c.key) } : c,
    );
    return [...visible, ACTION_COL];
  }, [copyState, refreshSingleBusyId, visibleCols, sort]);

  // openSub2apiDetail 定义在下方（含 async 数据加载），用 ref 让 columns 稳定引用它
  const openSub2apiDetailRef = useRef(null);

  const doBatch = (action) => {
    setBatchAction(null);
    if (selected.length === 0) { toast("请先选择账号", "warning"); return; }
    if (action === "pause") doBatchPause(selected);
    if (action === "resume") doBatchResume(selected);
    if (action !== "tag") setSelected([]);
  };

  const [batchTagBusy, setBatchTagBusy] = useState(false);
  const doBatchTag = async () => {
    const tag = (tagInput || "").trim();
    if (!selected.length) { toast("请先选择账号", "warning"); return; }
    setBatchTagBusy(true);
    try {
      const r = await api.accounts.bulkTag({ ids: selected, tag });
      toast(`已为 ${r.updated} 个账号设置标签「${tag || "（清除）"}」`, "success");
      setBatchAction(null);
      setTagInput("");
      reload();
    } catch (e) {
      toast(e?.message || "打标签失败", "error");
    } finally {
      setBatchTagBusy(false);
    }
  };

  const saveView = () => {
    if (!viewName.trim()) { toast("请输入视图名称", "warning"); return; }
    const view = { name: viewName, filters: { ...filters }, cols: visibleCols };
    const next = [...savedViews, view];
    setSavedViews(next);
    persistSavedViews(next);
    setViewName("");
    setSavingView(false);
    toast("视图已保存（浏览器本地持久化）", "success");
  };

  const applyView = (v) => {
    setFilters((f) => ({ ...f, ...(v.filters || {}) }));
    if (v.cols) setVisibleCols(v.cols);
    toast(`已应用视图「${v.name}」`, "info");
  };

  const openTransfer = (mode) => {
    setTransferModal(mode);
    setTransferFormat("cpa");
    setTransferFile(null);
    setTransferDedup("skip");
  };

  const closeTransfer = () => {
    if (transferBusy) return;
    setTransferModal(null);
    setTransferFile(null);
  };

  const handleTransferFile = (event) => {
    setTransferFile(event.target.files?.[0] || null);
  };

  const doImport = async () => {
    if (!transferFile) {
      toast("请选择要导入的 JSON 文件", "warning");
      return;
    }
    setTransferBusy(true);
    try {
      const content = await transferFile.text();
      const result = await api.accounts.importData({ format: transferFormat, content, dedup: transferDedup });
      toast(
        `导入完成：成功 ${result.success} / 跳过 ${result.skipped} / 失败 ${result.failed}`,
        result.failed > 0 ? "warning" : "success",
      );
      setSelected([]);
      setTransferModal(null);
      setTransferFile(null);
      reload();
    } catch (err) {
      toast(`导入失败: ${err.message}`, "error");
    } finally {
      setTransferBusy(false);
    }
  };

  const doExport = async () => {
    if (selected.length === 0) {
      toast("请先选择要导出的账号", "warning");
      return;
    }
    setTransferBusy(true);
    try {
      const result = await api.accounts.exportData({ ids: selected, format: transferFormat });
      const blob = new Blob([result.content], { type: "application/json;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename || `accounts_${transferFormat}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      toast(`已导出 ${result.count} 个账号 · ${transferFormat} 格式`, "success");
      setTransferModal(null);
    } catch (err) {
      toast(`导出失败: ${err.message}`, "error");
    } finally {
      setTransferBusy(false);
    }
  };

  const openSub2API = () => {
    if (selected.length === 0) {
      toast("请先选择要上传的账号", "warning");
      return;
    }
    sub2apiSelectionDirty.current = false;
    const configuredGroupIds = normalizeSub2APIGroupIds(sub2apiSettings?.sub2api_group_ids);
    setSub2apiGroupIds(configuredGroupIds);
    setSub2apiGroupIdsInput(configuredGroupIds.join(", "));
    setSub2apiConcurrency(3);
    setSub2apiResult(null);
    setSub2apiGroupsError("");
    setSub2apiModal(true);
  };

  const closeSub2API = () => {
    if (sub2apiBusy) return;
    setSub2apiModal(false);
    setSub2apiResult(null);
    setSub2apiUploadJob(null);
  };

  const openSub2apiSync = () => {
    sub2apiSelectionDirty.current = false;
    const configuredGroupIds = normalizeSub2APIGroupIds(sub2apiSettings?.sub2api_group_ids);
    setSub2apiGroupIds(configuredGroupIds);
    setSub2apiGroupIdsInput(configuredGroupIds.join(", "));
    setSub2apiSyncResult(null);
    setSub2apiGroupsError("");
    setSub2apiSyncModal(true);
  };

  const closeSub2apiSync = () => {
    if (sub2apiSyncBusy) return;
    setSub2apiSyncModal(false);
    setSub2apiSyncResult(null);
  };

  const doSub2apiSync = async () => {
    const groupIds = sub2apiGroupIdsInput.trim()
      ? normalizeSub2APIGroupIds(sub2apiGroupIdsInput)
      : normalizeSub2APIGroupIds(sub2apiGroupIds);
    if (groupIds.length === 0) {
      toast("请至少指定一个有效的 Sub2API 分组 ID", "warning");
      return;
    }
    setSub2apiSyncBusy(true);
    try {
      const result = await api.sub2api.syncUploadStatus({ group_ids: groupIds });
      setSub2apiSyncResult(result);
      toast(`Sub2API 状态同步完成：上传 ${result.uploaded} / 未上传 ${result.not_uploaded} / No access token ${result.token_error} / 远端异常 ${result.remote_error}`, "success");
      reload();
    } catch (err) {
      toast(`Sub2API 状态同步失败: ${err.message}`, "error");
    } finally {
      setSub2apiSyncBusy(false);
    }
  };

  const openSub2apiDetail = async (a) => {
    setSub2apiDetailAccount(a);
    setSub2apiDetailRows([]);
    setSub2apiDetailError("");
    setSub2apiDetailLoading(true);
    try {
      const res = await api.sub2api.uploadStatus({ accountId: a.id, pageSize: 100 });
      setSub2apiDetailRows(res.items || []);
    } catch (err) {
      setSub2apiDetailError(err?.message || "加载 Sub2API 上传明细失败");
    } finally {
      setSub2apiDetailLoading(false);
    }
  };
  // 让 columns(useMemo)与详情抽屉在 openSub2apiDetail 定义前也能安全引用
  openSub2apiDetailRef.current = openSub2apiDetail;

  const doSub2APIUpload = async () => {
    const uploadSelection = selectSub2APIUploadableIds(raw || [], selected);
    if (!uploadSelection.ids.length) {
      toast("所选账号缺少 access token，无法上传", "warning");
      return;
    }
    const groupIds = sub2apiGroupIdsInput.trim()
      ? normalizeSub2APIGroupIds(sub2apiGroupIdsInput)
      : normalizeSub2APIGroupIds(sub2apiGroupIds);
    if (groupIds.length === 0) {
      toast("请至少指定一个有效的 Sub2API 目标分组 ID", "warning");
      return;
    }
    setSub2apiBusy(true);
    setSub2apiUploadJob(null);
    try {
      const created = await api.sub2api.createUploadJob(buildSub2APIUploadRequest(uploadSelection.ids, groupIds, sub2apiConcurrency, sub2apiUploadOptions));
      let job = normalizeSub2APIUploadJob(created);
      setSub2apiUploadJob(job);
      while (job.status === "pending" || job.status === "running") {
        await new Promise((resolve) => setTimeout(resolve, 750));
        job = normalizeSub2APIUploadJob(await api.sub2api.uploadJob(created.job_id));
        setSub2apiUploadJob(job);
      }
      if (job.status !== "completed" || !job.result) {
        throw new Error(job.error || "Sub2API 上传任务失败");
      }
      const result = job.result;
      setSub2apiResult(result);
      const skippedNote = result.skipped && result.skipped.length ? ` / 过滤跳过 ${result.skipped.length}` : "";
      toast(
        `Sub2API 上传完成：成功 ${result.success} / 失败 ${result.failed}${uploadSelection.skipped.length ? ` / 跳过缺少 AT ${uploadSelection.skipped.length}` : ""}${skippedNote}`,
        result.failed > 0 || uploadSelection.skipped.length > 0 || (result.skipped && result.skipped.length) ? "warning" : "success",
      );
      reload();
    } catch (err) {
      toast(`Sub2API 上传失败: ${err.message}`, "error");
    } finally {
      setSub2apiBusy(false);
    }
  };

  const activeSub2APIGroupIds = sub2apiGroupIdsInput.trim()
    ? normalizeSub2APIGroupIds(sub2apiGroupIdsInput)
    : normalizeSub2APIGroupIds(sub2apiGroupIds);
  const sub2apiUploadSelection = useMemo(
    () => selectSub2APIUploadableIds(raw || [], selected),
    [raw, selected],
  );
  const sub2apiSummaryByAccountId = useMemo(() => {
    const map = {};
    for (const a of raw || []) map[Number(a.id)] = a.sub2api_upload_summary;
    return map;
  }, [raw]);
  const sub2apiUploadCounts = useMemo(
    () => classifySub2APIUploadSelection(raw || [], selected, sub2apiSummaryByAccountId),
    [raw, selected, sub2apiSummaryByAccountId],
  );
  const sub2apiResultActions = useMemo(() => {
    const counts = { created: 0, updated: 0, other: 0 };
    for (const item of sub2apiResult?.results || []) {
      if (item.action === "created") counts.created += 1;
      else if (item.action === "updated") counts.updated += 1;
      else counts.other += 1;
    }
    return counts;
  }, [sub2apiResult]);

  const toggleSub2APIGroup = (groupId) => {
    sub2apiSelectionDirty.current = true;
    const current = activeSub2APIGroupIds;
    const next = current.includes(groupId) ? current.filter((id) => id !== groupId) : [...current, groupId];
    setSub2apiGroupIds(next);
    setSub2apiGroupIdsInput(next.join(", "));
  };

  // 详情抽屉里的「刷新信息」：复用单账号刷新
  const drawerRefreshBusy = refreshSingleBusyId === detail?.id;
  const drawerRefresh = () => {
    if (detail?.id) refreshSingleRef.current?.(detail.id);
  };

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wide text-blue-600">
              <ShieldCheck size={13} /> AccountOps
            </div>
            <h1 className="mt-1 text-lg font-semibold text-slate-900">账号管理</h1>
            <p className="mt-1 text-xs text-slate-500">集中查看账号、OAuth 凭据、2FA 和 profile 状态；列表只展示脱敏值。</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <StatPill label="账号总数" value={stats.total} tone="blue" helper={`当前筛选 ${stats.filtered}`} onClick={() => { setFilters((f) => ({ ...f, cred: "all", health: "all" })); }} active={filters.cred === "all" && filters.health === "all"} />
            <StatPill label="已保存 AT / RT" value={`${stats.withAt}/${stats.withRt}`} tone="emerald" helper="可复制完整值" />
            <StatPill label="凭据已过期" value={stats.problem} tone="red" helper={`缺 Profile ${stats.missingProfile}`} onClick={() => setFilter("cred", "problem")} active={filters.cred === "problem"} />
          </div>
        </div>
      </div>

      {/* 账号信息刷新面板（远端身份/套餐/额度 + 凭据续期） */}
      <AccountsRefreshPanel
        selectedIds={selected}
        allTotal={stats.total}
        filteredCount={stats.filtered}
        onRefreshed={reload}
        registerSingleRefresh={registerRefreshSingle}
        toast={toast}
      />

      {/* 筛选工具栏（chip 化筛选 + localStorage 视图） */}
      <AccountsToolbar
        q={q} setQ={setQ}
        filters={filters} setFilter={setFilter}
        tagOptions={tagOptions}
        visibleCols={visibleCols} setVisibleCols={setVisibleCols}
        columns={[...buildColumns({ copyState, copyToken, refreshSingleBusyId, refreshSingleRef, openSub2apiDetail, setEditAccount, setSelected, setBatchAction, setConfirmDelete }).cols]}
        savedViews={savedViews}
        onSaveView={() => setSavingView(true)}
        onApplyView={applyView}
        onSub2apiSync={openSub2apiSync}
        onImport={() => openTransfer("import")}
        onExport={() => openTransfer("export")}
        onSub2apiUpload={openSub2API}
      />

      {/* 数据表 */}
      <Panel pad={false} className="overflow-hidden shadow-sm">
        <AccountsTable
          columns={columns}
          pageRows={pageRows}
          loading={loading} error={error} reload={reload}
          selected={selected}
          onSelectAll={(v) => setSelected(v ? pageRows.map((r) => r.id) : [])}
          onSelectRow={(id, v) => setSelected((s) => v ? [...s, id] : s.filter((x) => x !== id))}
          rowClick={openDetail}
          page={page} setPage={setPage}
          pageSize={pageSize} onPageSize={setPageSize}
          total={rows.length}
          rowsCount={rows.length}
          filters={filters}
          q={q}
          onReloadList={reload}
          t={t}
        />
        {/* 批量操作条 */}
        <BatchActionBar
          selected={selected}
          onBatchAction={setBatchAction}
          onSub2apiUpload={openSub2API}
          onCancel={() => setSelected([])}
        />
      </Panel>

      <Modal
        open={!!transferModal}
        onClose={closeTransfer}
        title={transferModal === "import" ? "导入账号" : "导出账号"}
        width={540}
        footer={
          <>
            <Button variant="secondary" onClick={closeTransfer} disabled={transferBusy}>取消</Button>
            {transferModal === "import" ? (
              <Button icon={<Upload size={13} />} onClick={doImport} loading={transferBusy}>开始导入</Button>
            ) : (
              <Button icon={<FileJson size={13} />} onClick={doExport} loading={transferBusy}>下载导出文件</Button>
            )}
          </>
        }>
        {transferModal === "import" ? (
          <div className="space-y-4 pt-4">
            <div className="rounded-md border border-blue-100 bg-blue-50/60 p-3 text-xs leading-relaxed text-blue-800">
              支持 Cockpit Tools 导出的 CPA 或 Sub2API JSON。导入内容会写入账号凭据；页面不会展示文件中的完整 token。
            </div>
            <input ref={transferFileRef} type="file" accept=".json,application/json" className="hidden" onChange={handleTransferFile} />
            <div className="flex items-center gap-3 rounded-md border border-slate-200 px-3 py-3">
              <FileJson size={20} className="shrink-0 text-slate-400" />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13px] text-slate-700">{transferFile?.name || "尚未选择 JSON 文件"}</div>
                <div className="mt-0.5 text-[11px] text-slate-400">{transferFile ? `${(transferFile.size / 1024).toFixed(1)} KB` : "请选择 CPA 或 Sub2API 导出文件"}</div>
              </div>
              <Button variant="secondary" size="sm" icon={<Upload size={13} />} onClick={() => transferFileRef.current?.click()}>选择文件</Button>
            </div>
            <div className="flex items-center gap-2 text-[13px]">
              <span className="text-slate-500">重复账号处理</span>
              <select value={transferDedup} onChange={(e) => setTransferDedup(e.target.value)}
                className="rounded-md border border-slate-200 bg-white px-2 py-1.5 text-[13px] text-slate-700">
                <option value="skip">跳过已存在账号</option>
                <option value="overwrite">覆盖已有凭据</option>
              </select>
            </div>
          </div>
        ) : (
          <div className="space-y-4 pt-4">
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs leading-relaxed text-amber-800">
              将导出所选账号的完整 OAuth 凭据、密码和 2FA 信息。文件可被其他工具直接导入，请按密码级别保管。
            </div>
            <div className="flex items-center justify-between rounded-md bg-slate-50 px-3 py-3 text-[13px] text-slate-600">
              <span>待导出账号</span>
              <Badge color={selected.length ? "info" : "warning"} dot>{selected.length ? `${selected.length} 个` : "未选择"}</Badge>
            </div>
            {!selected.length && <div className="text-xs text-red-600">请先在账号列表勾选账号，再下载导出文件。</div>}
          </div>
        )}
      </Modal>

      <Modal
        open={sub2apiModal}
        onClose={closeSub2API}
        title="上传到 Sub2API"
        width={560}
        footer={
          <>
            <Button variant="secondary" onClick={closeSub2API} disabled={sub2apiBusy}>关闭</Button>
            {sub2apiResult && <Button variant="secondary" icon={<RefreshCw size={13} />} onClick={() => { setSub2apiResult(null); setSub2apiUploadJob(null); }} disabled={sub2apiBusy}>重新上传</Button>}
            {!sub2apiResult && <Button icon={<Upload size={13} />} onClick={doSub2APIUpload} loading={sub2apiBusy} disabled={!activeSub2APIGroupIds.length || sub2apiGroupsLoading}>开始上传</Button>}
          </>
        }>
        <Sub2apiUploadBody
          groups={sub2apiGroups} groupsLoading={sub2apiGroupsLoading} groupsError={sub2apiGroupsError}
          groupIdsInput={sub2apiGroupIdsInput} setGroupIdsInput={(v) => { sub2apiSelectionDirty.current = true; setSub2apiGroupIdsInput(v); }}
          activeGroupIds={activeSub2APIGroupIds} toggleGroup={toggleSub2APIGroup}
          concurrency={sub2apiConcurrency} setConcurrency={setSub2apiConcurrency}
          uploadSelection={sub2apiUploadSelection} uploadCounts={sub2apiUploadCounts}
          uploadOptions={sub2apiUploadOptions} setUploadOptions={setSub2apiUploadOptions}
          uploadJob={sub2apiUploadJob} result={sub2apiResult} resultActions={sub2apiResultActions}
        />
      </Modal>

      {/* 同步 Sub2API 状态 */}
      <Modal
        open={sub2apiSyncModal}
        onClose={closeSub2apiSync}
        title="同步 Sub2API 状态"
        width={560}
        footer={
          <>
            <Button variant="secondary" onClick={closeSub2apiSync} disabled={sub2apiSyncBusy}>关闭</Button>
            <Button icon={sub2apiSyncBusy ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />} onClick={doSub2apiSync} loading={sub2apiSyncBusy} disabled={!activeSub2APIGroupIds.length || sub2apiGroupsLoading}>开始同步</Button>
          </>
        }>
        <div className="space-y-4">
          <div className="rounded-md border border-blue-100 bg-blue-50/60 p-3 text-xs leading-relaxed text-blue-800">
            拉取所选分组的远端 Sub2API 账号，按邮箱（小写）匹配本地账号，把「已上传 / 未上传 / No access token / 远端异常 / 分组不匹配」写入本地并持久化。之后无需再次同步也能看到上次上传结果；同步只是刷新远端状态。
          </div>
          {sub2apiGroupsLoading ? (
            <div className="text-xs text-slate-400">正在加载 Sub2API 分组...</div>
          ) : null}
          {sub2apiGroupsError && (
            <div className="rounded-md border border-red-100 bg-red-50 p-3 text-xs leading-relaxed text-red-700">{sub2apiGroupsError}。仍可直接输入分组 ID。</div>
          )}
          {sub2apiGroups.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium text-slate-600">选择要同步的分组</div>
              <div className="max-h-36 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
                {sub2apiGroups.map((group) => (
                  <label key={group.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs text-slate-700 hover:bg-slate-50">
                    <input type="checkbox" checked={activeSub2APIGroupIds.includes(group.id)} onChange={() => toggleSub2APIGroup(group.id)} />
                    <span>{group.name}</span>
                    <span className="ml-auto text-slate-400">#{group.id}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
          <Input
            label="指定分组 ID（可填多个）"
            type="text"
            value={sub2apiGroupIdsInput}
            onChange={(event) => {
              sub2apiSelectionDirty.current = true;
              setSub2apiGroupIdsInput(event.target.value);
            }}
            placeholder="例如 42, 43, 108"
          />
          <div className="text-[11px] text-slate-400">同步会为每个本地账号 × 每个目标分组写入一条状态；重复同步走更新，不会重复插入。</div>
          {sub2apiSyncResult && (
            <div className="space-y-3 border-t border-slate-100 pt-4">
              <div className="grid grid-cols-4 gap-2 text-center">
                <div className="rounded-md bg-emerald-50 px-2 py-2"><div className="text-lg font-semibold text-emerald-700">{sub2apiSyncResult.uploaded}</div><div className="text-[11px] text-emerald-600">已上传</div></div>
                <div className="rounded-md bg-slate-50 px-2 py-2"><div className="text-lg font-semibold text-slate-700">{sub2apiSyncResult.not_uploaded}</div><div className="text-[11px] text-slate-500">未上传</div></div>
                <div className="rounded-md bg-red-50 px-2 py-2"><div className="text-lg font-semibold text-red-700">{sub2apiSyncResult.token_error}</div><div className="text-[11px] text-red-600">No access token</div></div>
                <div className="rounded-md bg-amber-50 px-2 py-2"><div className="text-lg font-semibold text-amber-700">{sub2apiSyncResult.remote_error}</div><div className="text-[11px] text-amber-600">远端异常</div></div>
              </div>
              <div className="text-[11px] text-slate-500">
                本地账号 {sub2apiSyncResult.total_local} · 匹配远端 {sub2apiSyncResult.matched_remote} · 上传异常 {sub2apiSyncResult.uploaded_error} · 分组不匹配 {sub2apiSyncResult.group_mismatch}
              </div>
            </div>
          )}
        </div>
      </Modal>

      {/* Sub2API 上传明细 */}
      <Modal
        open={!!sub2apiDetailAccount}
        onClose={() => setSub2apiDetailAccount(null)}
        title={sub2apiDetailAccount ? `Sub2API 上传明细 · ${sub2apiDetailAccount.email || sub2apiDetailAccount.phone || `acc_${sub2apiDetailAccount.id}`}` : ""}
        width={760}
        footer={<>
          <Button variant="secondary" onClick={() => setSub2apiDetailAccount(null)}>关闭</Button>
          <Button variant="secondary" icon={<RefreshCw size={13} />} onClick={() => sub2apiDetailAccount && openSub2apiDetail(sub2apiDetailAccount)}>刷新</Button>
        </>}>
        {sub2apiDetailLoading ? (
          <div className="py-8 text-center text-xs text-slate-400"><Loader2 size={16} className="mx-auto mb-2 animate-spin" />正在加载上传明细…</div>
        ) : sub2apiDetailError ? (
          <div className="rounded-md border border-red-100 bg-red-50 p-3 text-xs leading-relaxed text-red-700">{sub2apiDetailError}</div>
        ) : sub2apiDetailRows.length === 0 ? (
          <div className="py-8 text-center text-xs text-slate-400">暂无上传记录（尚未同步或从未上传）。点击「同步 Sub2API 状态」后可查看各分组明细。</div>
        ) : (
          <div className="max-h-[60vh] overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-left text-slate-500">
                  <th className="px-2 py-2">分组</th>
                  <th className="px-2 py-2">状态</th>
                  <th className="px-2 py-2">远端 ID</th>
                  <th className="px-2 py-2">AT / RT</th>
                  <th className="px-2 py-2">并发 / 负载</th>
                  <th className="px-2 py-2">最近核验</th>
                  <th className="px-2 py-2">错误信息</th>
                </tr>
              </thead>
              <tbody>
                {sub2apiDetailRows.map((row) => {
                  const meta = SUB2API_UPLOAD_STATUS_META[row.status] || { label: row.status, color: "neutral" };
                  return (
                    <tr key={row.id} className="border-b border-slate-100 align-top">
                      <td className="px-2 py-2"><span className="font-medium text-slate-700">{row.group_name || `分组 ${row.group_id}`}</span><div className="text-[11px] text-slate-400">#{row.group_id}</div></td>
                      <td className="px-2 py-2"><Badge color={meta.color}>{meta.label}</Badge></td>
                      <td className="px-2 py-2"><span className="mono text-slate-600">{row.remote_id || "—"}</span></td>
                      <td className="px-2 py-2">
                        <span className={row.has_access_token ? "text-emerald-600" : "text-slate-400"}>{row.has_access_token === true ? "✓" : row.has_access_token === false ? "✗" : "—"}</span>
                        {" / "}
                        <span className={row.has_refresh_token ? "text-emerald-600" : "text-slate-400"}>{row.has_refresh_token === true ? "✓" : row.has_refresh_token === false ? "✗" : "—"}</span>
                      </td>
                      <td className="px-2 py-2 tnum text-slate-600">{row.remote_concurrency ?? "—"} / {row.remote_load_factor ?? "—"}</td>
                      <td className="px-2 py-2 text-slate-500">{row.verified_at ? fmtAgo(new Date(row.verified_at).getTime()) : "—"}</td>
                      <td className="px-2 py-2"><span className="max-w-[220px] break-all text-red-600" title={row.last_error || ""}>{row.last_error || "—"}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Modal>

      {/* 保存视图 */}
      <Modal open={savingView} onClose={() => setSavingView(false)} title="保存当前视图" width={400}
        footer={<><Button variant="secondary" onClick={() => setSavingView(false)}>取消</Button><Button onClick={saveView}>保存</Button></>}>
        <Input label="视图名称" value={viewName} onChange={(e) => setViewName(e.target.value)} placeholder="例如：待处理告警" />
        <div className="mt-3 rounded-md bg-slate-50 p-3 text-xs text-slate-500">
          将保存当前筛选与列显示配置到浏览器本地（localStorage），下次打开仍然生效。
        </div>
      </Modal>

      {/* 批量确认 */}
      <Confirm open={!!batchAction && batchAction !== "tag"} onClose={() => setBatchAction(null)}
        onConfirm={() => doBatch(batchAction)} danger={batchAction === "pause"}
        title={batchAction === "pause" ? "暂停所选账号" : "恢复所选账号"}
        message={batchAction === "pause"
          ? `确定暂停已选的 ${selected.length} 个账号？暂停后这些账号将停止任务调度。`
          : `确定恢复已选的 ${selected.length} 个账号？`}
        confirmText={batchAction === "pause" ? "确认暂停" : "确认恢复"} />

      {/* 删除确认 */}
      <Confirm open={!!confirmDelete} onClose={() => setConfirmDelete(null)} danger
        title={confirmDelete?.kind === "batch" ? `删除所选 ${confirmDelete.ids.length} 个账号` : "删除账号"}
        message={confirmDelete?.kind === "batch"
          ? `确定永久删除已选的 ${confirmDelete.ids.length} 个账号？此操作不可恢复。`
          : `确定永久删除「${confirmDelete?.account?.email || confirmDelete?.account?.phone || `#${confirmDelete?.account?.id}`}」？此操作不可恢复。`}
        confirmText="确认删除" loading={deleting}
        onConfirm={() => {
          if (confirmDelete?.kind === "batch") doBatchDelete(confirmDelete.ids);
          else doDelete(confirmDelete?.account?.id);
          setConfirmDelete(null);
        }} />

      <Modal open={batchAction === "tag"} onClose={() => setBatchAction(null)} title="为所选账号打标签" width={420}
        footer={<>
          <Button variant="secondary" onClick={() => setBatchAction(null)}>取消</Button>
          <Button onClick={doBatchTag} disabled={batchTagBusy}>{batchTagBusy ? "保存中…" : "应用"}</Button>
        </>}>
        <Input label="标签" value={tagInput} onChange={(e) => setTagInput(e.target.value)} placeholder="例如：post-fix-20260823 / 巴西 / 主池；留空=清除标签" />
        <div className="mt-2 flex flex-wrap gap-1.5">
          {[tagOptions[0] || "pre-fix-20260823", "post-fix-20260823", "巴西", "主池", "备用"].map((tg) => (
            <button key={tg} onClick={() => setTagInput(tg)} className="rounded border border-slate-200 px-2 py-0.5 text-xs text-slate-600 hover:border-blue-300 hover:text-blue-600">{tg}</button>
          ))}
        </div>
      </Modal>

      {/* 编辑元数据 */}
      <Modal open={!!editAccount} onClose={() => setEditAccount(null)} title={`编辑元数据 · acc_${editAccount?.id}`} width={440}
        footer={<><Button variant="secondary" onClick={() => setEditAccount(null)}>取消</Button><Button onClick={saveNote}>保存</Button></>}>
        {editAccount && (
          <div className="space-y-3">
            <div className="text-xs text-slate-400">备注会随账号详情保留。</div>
            <Input label="备注" value={editAccount.note || ""} onChange={(e) => setEditAccount({ ...editAccount, note: e.target.value })} placeholder="可选备注，例如：巴西主池 / 验证过" />
          </div>
        )}
      </Modal>

      {/* 账号详情抽屉（增强概览：资产状态卡 + 刷新信息入口） */}
      <AccountsDetailDrawer
        detail={detail} detailData={detailData}
        detailTab={detailTab} setDetailTab={setDetailTab}
        detailBusy={detailBusy}
        totpInput={totpInput} setTotpInput={setTotpInput}
        savingTotp={savingTotp} saveTotp={saveTotp}
        onClose={() => setDetail(null)}
        onEditNote={() => setEditAccount(detailData || detail)}
        onDelete={() => { setConfirmDelete({ kind: "single", account: detail }); setDetail(null); }}
        onRefreshSingle={drawerRefresh}
        refreshBusy={drawerRefreshBusy}
      />
    </div>
  );
}

// Sub2API 上传弹窗主体（本文件内部小组件，逻辑原样搬移）
function Sub2apiUploadBody({
  groups, groupsLoading, groupsError,
  groupIdsInput, setGroupIdsInput, activeGroupIds, toggleGroup,
  concurrency, setConcurrency,
  uploadSelection, uploadCounts, uploadOptions, setUploadOptions,
  uploadJob, result, resultActions,
}) {
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-blue-100 bg-blue-50/60 p-3 text-xs leading-relaxed text-blue-800">
        将所选账号一次上传到多个指定的 Sub2API 分组。每个账号只创建一次，后端通过 group_ids 同时绑定多个分组；有 access token 的账号即可上传，若同时有 refresh_token / id_token 则保留完整 OAuth 自动续期能力。
      </div>
      <div className="flex items-center justify-between rounded-md bg-slate-50 px-3 py-3 text-[13px] text-slate-600">
        <span>待上传账号</span>
        <div className="flex flex-wrap justify-end gap-2">
          <Badge color={uploadSelection.ids.length ? "info" : "warning"} dot>{uploadSelection.ids.length ? `可上传 ${uploadSelection.ids.length} 个` : "无可上传账号"}</Badge>
          {uploadSelection.skipped.length > 0 && <Badge color="warning" dot>缺少 AT 跳过 {uploadSelection.skipped.length} 个</Badge>}
        </div>
      </div>
      <div className="rounded-md border border-slate-200 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
          <span className="text-[13px] text-slate-600">已选账号上传状态（本地记录）</span>
          <div className="flex flex-wrap justify-end gap-2">
            <Badge color={uploadCounts.uploaded ? "success" : "neutral"} dot>已上传 {uploadCounts.uploaded}</Badge>
            <Badge color={uploadCounts.notUploaded ? "info" : "neutral"} dot>未上传 {uploadCounts.notUploaded}</Badge>
            <Badge color={uploadCounts.error ? "danger" : "neutral"} dot>异常 {uploadCounts.error}</Badge>
            {uploadCounts.tokenIncomplete > 0 && <Badge color="warning" dot>缺少 AT {uploadCounts.tokenIncomplete}</Badge>}
          </div>
        </div>
        <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-slate-600">
          <label className="flex cursor-pointer items-center gap-1.5">
            <input type="checkbox" checked={uploadOptions.onlyNotUploaded} onChange={(e) => setUploadOptions((o) => ({ ...o, onlyNotUploaded: e.target.checked }))} />
            只上传未上传
          </label>
          <label className="flex cursor-pointer items-center gap-1.5">
            <input type="checkbox" checked={uploadOptions.overwriteExisting} onChange={(e) => setUploadOptions((o) => ({ ...o, overwriteExisting: e.target.checked }))} />
            覆盖更新已上传账号
          </label>
          <label className="flex cursor-pointer items-center gap-1.5">
            <input type="checkbox" checked={uploadOptions.includeTokenError} onChange={(e) => setUploadOptions((o) => ({ ...o, includeTokenError: e.target.checked }))} />
            包含 No access token 账号重传
          </label>
        </div>
        <div className="mt-1.5 text-[11px] text-slate-400">按上次同步的本地记录过滤；勾选「只上传未上传」后，任一目标分组已上传的账号都会被跳过。</div>
      </div>
      {groupsLoading ? (
        <div className="text-xs text-slate-400">正在加载 Sub2API 分组...</div>
      ) : null}
      {groupsError && (
        <div className="rounded-md border border-red-100 bg-red-50 p-3 text-xs leading-relaxed text-red-700">{groupsError}。仍可直接输入目标分组 ID。</div>
      )}
      {groups.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-slate-600">选择目标分组</div>
          <div className="max-h-36 space-y-1 overflow-y-auto rounded-md border border-slate-200 p-2">
            {groups.map((group) => (
              <label key={group.id} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs text-slate-700 hover:bg-slate-50">
                <input type="checkbox" checked={activeGroupIds.includes(group.id)} onChange={() => toggleGroup(group.id)} />
                <span>{group.name}</span>
                <span className="ml-auto text-slate-400">#{group.id}</span>
              </label>
            ))}
          </div>
        </div>
      )}
      <Input
        label="指定分组 ID（可填多个）"
        type="text"
        value={groupIdsInput}
        onChange={setGroupIdsInput}
        placeholder="例如 42, 43, 108"
      />
      <Input
        label="账号并发数"
        type="number"
        min={1}
        max={20}
        value={concurrency}
        onChange={(event) => setConcurrency(event.target.value)}
        hint="该值会写入 Sub2API 账号 concurrency 字段（范围 1–20）。"
      />
      <div className="text-[11px] text-slate-400">默认并发数为 3，可在此临时调整。可勾选分组或输入逗号分隔的 ID；已有远端账号会复用并保留原分组，再合并本次目标分组。</div>
      {uploadJob && !result && (
        <div className="min-h-[126px] rounded-md border border-blue-200 bg-blue-50/50 p-3" aria-live="polite">
          <div className="flex items-center justify-between gap-3 text-xs text-slate-600">
            <span className="font-medium">上传进度</span>
            <span className="tnum font-semibold text-blue-700">{uploadJob.progress}%</span>
          </div>
          <div className="mt-2 h-2 overflow-hidden rounded-full bg-blue-100">
            <div className="h-full rounded-full bg-blue-600 transition-[width] duration-300" style={{ width: `${uploadJob.progress}%` }} />
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[11px] text-slate-500">
            <span>已处理 {uploadJob.processed} / {uploadJob.total}</span>
            <span className="text-emerald-700">成功 {uploadJob.success}</span>
            <span className="text-red-700">失败 {uploadJob.failed}</span>
          </div>
          <div className="mt-2 truncate text-[11px] text-slate-500" title={uploadJob.current_email}>
            当前账号：{uploadJob.current_email || (uploadJob.status === "pending" ? "准备中" : "处理中")}
          </div>
          {uploadJob.error && <div className="mt-2 text-xs text-red-700">{uploadJob.error}</div>}
        </div>
      )}
      {result && (
        <div className="space-y-3 border-t border-slate-100 pt-4">
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="rounded-md bg-emerald-50 px-2 py-2"><div className="text-lg font-semibold text-emerald-700">{result.success}</div><div className="text-[11px] text-emerald-600">成功</div></div>
            <div className="rounded-md bg-red-50 px-2 py-2"><div className="text-lg font-semibold text-red-700">{result.failed}</div><div className="text-[11px] text-red-600">失败</div></div>
            <div className="rounded-md bg-slate-50 px-2 py-2"><div className="text-lg font-semibold text-slate-700">{result.count}</div><div className="text-[11px] text-slate-500">处理</div></div>
          </div>
          {(result.skipped || []).length > 0 && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <div className="font-medium">按过滤规则跳过 {result.skipped.length} 个账号：</div>
              <div className="mt-1 max-h-24 space-y-0.5 overflow-y-auto">
                {result.skipped.slice(0, 20).map((item, index) => (
                  <div key={`${item.account_id}-${index}`}>{item.account_id} · {item.email || "本地账号"} · {item.reason}</div>
                ))}
              </div>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-500">
            <span>新建 {resultActions.created}</span>
            <span>更新 {resultActions.updated}</span>
            {resultActions.other > 0 && <span>其他成功 {resultActions.other}</span>}
            <span>目标分组 {Array.isArray(result.group_ids) ? result.group_ids.join(", ") : "—"}</span>
          </div>
          {(result.results || []).length > 0 && (
            <div className="rounded-md border border-emerald-100 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-800">
              <div className="font-medium">已完成账号</div>
              <div className="mt-1 max-h-28 space-y-0.5 overflow-y-auto">
                {result.results.slice(0, 20).map((item, index) => {
                  const targetGroups = Array.isArray(item.group_ids) ? item.group_ids.join(", ") : "—";
                  const remoteGroups = Array.isArray(item.remote_group_ids) && item.remote_group_ids.length
                    ? item.remote_group_ids.join(", ")
                    : "未返回";
                  const action = item.action === "updated" ? "更新" : item.action === "created" ? "新建" : "同步";
                  return (
                    <div key={`${item.account_id}-${index}`}>
                      {item.account_id} · {item.email || "本地账号"} · {action} · 目标分组 {targetGroups} · 远端分组 {remoteGroups}
                    </div>
                  );
                })}
              </div>
              {result.results.length > 20 && <div className="mt-1 text-[11px] text-emerald-700">其余 {result.results.length - 20} 个账号已省略。</div>}
            </div>
          )}
          <div className="text-[11px] text-slate-500">本次上传写入的并发数：<span className="tnum font-semibold text-slate-700">{result.concurrency ?? 3}</span></div>
          {(() => {
            const mismatch = sub2apiConcurrencyMismatch(result);
            if (!mismatch) return null;
            return (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium leading-relaxed text-amber-800">
                远端并发设置未同步：目标 {mismatch.target}，远端 concurrency={mismatch.remoteConcurrency ?? "未知"}，远端 load_factor={mismatch.remoteLoadFactor ?? "未知"}，请检查 Sub2API 更新接口兼容性。
              </div>
            );
          })()}
          {(result.errors || []).length > 0 && (
            <div className="space-y-1 text-xs text-red-700">
              {result.errors.slice(0, 8).map((item, index) => <div key={`${item.account_id}-${index}`}>{item.account_id} · {item.email || "本地账号"} · {item.error}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
