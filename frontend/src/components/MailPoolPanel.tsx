import { useState } from "react";
import {
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Search,
  ShieldCheck,
  Upload,
} from "lucide-react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import {
  mailTime,
  POOL_STATUSES,
  PROVIDER_NAMES,
  type MailProvider,
  type MailSnapshot,
} from "../lib/mailConfig";
export type MailPoolAction =
  | "poolRequeue"
  | "poolRelease"
  | "poolDisable"
  | "poolEnable"
  | "poolVerify"
  | "poolRemove";
export function MailPoolPanel({
  provider,
  snapshot,
  busy,
  onImport,
  onClear,
  onAction,
}: {
  provider: MailProvider;
  snapshot: MailSnapshot;
  busy: boolean;
  onImport: () => void;
  onClear: () => void;
  onAction: (method: MailPoolAction, ids: number[]) => Promise<void>;
}) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const [selection, setSelection] = useState<number[]>([]);
  const filtered = (snapshot.cf_temp_email.custom_pool_items || []).filter(
    (item) =>
      (!status || item.status === status) &&
      item.address.toLowerCase().includes(query.trim().toLowerCase()),
  );
  const pages = Math.max(1, Math.ceil(filtered.length / 25));
  const currentPage = Math.min(page, pages - 1);
  const rows = filtered.slice(currentPage * 25, (currentPage + 1) * 25);
  const selected = rows
    .filter((row) => selection.includes(row.id))
    .map((row) => row.id);
  const all = rows.length > 0 && selected.length === rows.length;
  // Use the DOM property through a ref callback, including partial page selection.
  const selectRef = (node: HTMLInputElement | null) => {
    if (node) node.indeterminate = selected.length > 0 && !all;
  };
  async function action(method: MailPoolAction) {
    await onAction(method, selected);
    setSelection([]);
  }
  const cf = snapshot.cf_temp_email,
    ic = snapshot.icloud;
  return (
    <section aria-label={`${PROVIDER_NAMES[provider]} 邮箱池`}>
      <div className="mail-section-title">
        <div>
          <h2>
            {provider === "icloud"
              ? "iCloud 邮箱池"
              : "Cloudflare 自定义邮箱池"}
          </h2>
          <p>
            {provider === "icloud"
              ? "仅展示服务端返回的脱敏样例，不提供虚构的逐条状态。"
              : "搜索范围为脱敏地址；池操作立即生效，不会保存或清除配置草稿。"}
          </p>
        </div>
        <Button size="sm" disabled={busy} onClick={onImport}>
          <Upload />
          批量导入
        </Button>
      </div>
      {provider === "icloud" ? (
        <>
          <div className="mail-pool-metrics">
            <div>
              <span>已配置基础邮箱</span>
              <strong>{ic.accounts_count}</strong>
            </div>
            <div>
              <span>预计剩余轮数</span>
              <strong>{ic.remaining_rounds ?? "—"}</strong>
            </div>
            <div>
              <span>已耗尽基础邮箱</span>
              <strong>{ic.exhausted_roots ?? "—"}</strong>
            </div>
          </div>
          <div className="mail-notice">
            <ShieldCheck size={17} />
            <span>
              API 仅返回最多 3
              条脱敏样例，不能在这里搜索完整池或逐条操作。导入支持追加与整体替换；替换、清空均需确认。
            </span>
          </div>
          {ic.accounts_sample?.length ? (
            <div className="mail-samples">
              {ic.accounts_sample.map((sample, index) => (
                <div key={`${index}-${sample}`}>
                  <Inbox size={17} />
                  <code>{sample}</code>
                  <span className="mail-pill">脱敏样例</span>
                </div>
              ))}
            </div>
          ) : (
            <div className="mail-empty">
              <Inbox />
              <h3>还没有邮箱资源</h3>
              <p>导入「邮箱----收信API」后即可配置为注册来源。</p>
            </div>
          )}
          <div className="mail-pool-foot">
            <span>剩余轮数是服务端估算，不代表实际收码成功率。</span>
            <Button
              variant="outline"
              size="sm"
              disabled={busy || !ic.accounts_count}
              onClick={onClear}
            >
              清空邮箱池
            </Button>
          </div>
        </>
      ) : (
        <>
          <div className="mail-pool-metrics">
            {["unused", "in_use", "used", "failed", "disabled"].map((key) => (
              <div key={key}>
                <span>{POOL_STATUSES[key]}</span>
                <strong>{cf.custom_pool_status_counts?.[key] ?? 0}</strong>
              </div>
            ))}
          </div>
          {cf.address_mode !== "custom_pool" && (
            <div className="mail-notice">
              <Inbox size={17} />
              <span>
                当前使用自动生成地址。你仍可管理池资源；切换到「自定义邮箱池」并保存后，注册才会使用这些地址。
              </span>
            </div>
          )}
          {cf.custom_pool_summary?.low_water && (
            <div className="mail-notice mail-warning">
              <AlertTriangle size={17} />
              <span>
                可用地址低于水位阈值{" "}
                {Number(cf.custom_pool_summary.low_water_threshold ?? 0)}
                ，建议补充资源。
              </span>
            </div>
          )}
          <div className="mail-pool-filters">
            <div className="mail-search">
              <Search size={15} />
              <Input
                aria-label="搜索脱敏邮箱"
                placeholder="搜索脱敏邮箱…"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPage(0);
                  setSelection([]);
                }}
              />
            </div>
            <label className="mail-filter-label">
              邮箱状态
              <select
                className="input"
                aria-label="邮箱状态"
                value={status}
                onChange={(e) => {
                  setStatus(e.target.value);
                  setPage(0);
                  setSelection([]);
                }}
              >
                <option value="">全部状态</option>
                {Object.entries(POOL_STATUSES).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void onAction("poolVerify", [])}
            >
              核验全部凭据
            </Button>
          </div>
          <div className="mail-batch-bar">
            <span>
              当前页已选 <strong>{selected.length}</strong>
            </span>
            <div>
              {(
                [
                  ["poolRequeue", "回收"],
                  ["poolRelease", "释放"],
                  ["poolDisable", "停用"],
                  ["poolEnable", "启用"],
                  ["poolVerify", "核验凭据"],
                  ["poolRemove", "移除"],
                ] as const
              ).map(([method, label]) => (
                <Button
                  key={method}
                  variant={method === "poolRemove" ? "destructive" : "outline"}
                  size="sm"
                  disabled={busy || !selected.length}
                  onClick={() => void action(method)}
                >
                  {label}
                </Button>
              ))}
            </div>
          </div>
          <div className="mail-table-wrap">
            <table className="mail-table">
              <thead>
                <tr>
                  <th>
                    <input
                      ref={selectRef}
                      type="checkbox"
                      aria-label="选择当前页"
                      checked={all}
                      disabled={busy || !rows.length}
                      onChange={() =>
                        setSelection(all ? [] : rows.map((row) => row.id))
                      }
                    />
                  </th>
                  <th>邮箱地址（脱敏）</th>
                  <th>状态</th>
                  <th>凭据 / 诊断</th>
                  <th>尝试次数</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <input
                        type="checkbox"
                        disabled={busy}
                        aria-label={`选择邮箱 ${item.id}`}
                        checked={selected.includes(item.id)}
                        onChange={() =>
                          setSelection(
                            selected.includes(item.id)
                              ? selection.filter((id) => id !== item.id)
                              : [...selection, item.id],
                          )
                        }
                      />
                    </td>
                    <td>
                      <code>{item.address}</code>
                    </td>
                    <td>
                      <span
                        className={`mail-status mail-status-${item.status}`}
                      >
                        {POOL_STATUSES[item.status] || "未知状态"}
                      </span>
                    </td>
                    <td>
                      {item.status === "used"
                        ? item.has_refresh_token === true
                          ? "RT + AT"
                          : item.has_refresh_token === false
                            ? "仅 AT"
                            : "待核验"
                        : item.status === "failed"
                          ? item.reason_code === "pre_submit"
                            ? "提交前失败 · 可回收"
                            : "需人工确认"
                          : item.status === "in_use"
                            ? `租约至 ${mailTime(item.lease_expires_at)}`
                            : "—"}
                    </td>
                    <td>{item.attempt_count ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!rows.length && (
              <div className="mail-empty">
                <Inbox />
                <h3>{query || status ? "没有匹配的邮箱" : "邮箱池为空"}</h3>
                <p>
                  {query || status
                    ? "尝试调整搜索词或状态筛选。"
                    : "批量导入地址，保留已有资源和使用记录。"}
                </p>
              </div>
            )}
          </div>
          <div className="mail-pool-foot">
            <span>
              共 {filtered.length} 条 · 每页 25 条 · 仅操作当前页选中项
            </span>
            <div className="mail-pagination">
              <Button
                size="icon"
                variant="outline"
                aria-label="上一页"
                disabled={busy || currentPage === 0}
                onClick={() => {
                  setPage(currentPage - 1);
                  setSelection([]);
                }}
              >
                <ChevronLeft />
              </Button>
              <span>
                {currentPage + 1} / {pages}
              </span>
              <Button
                size="icon"
                variant="outline"
                aria-label="下一页"
                disabled={busy || currentPage >= pages - 1}
                onClick={() => {
                  setPage(currentPage + 1);
                  setSelection([]);
                }}
              >
                <ChevronRight />
              </Button>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
