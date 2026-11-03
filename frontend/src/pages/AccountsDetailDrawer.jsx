// 账号详情抽屉（从 Accounts.jsx 拆出）
// 概览页增强：集成 Sub2API 上传状态、刷新额度信息（来自账号列表数据的本地摘要）
import { Badge, Button, Tabs, SecretField, Input } from "../components/ui";
import { accStatus, credState, tokenExpiryText } from "./accountsShared";
import { fmtAgo, fmtTime } from "../mock/data";
import { sub2apiUploadBadge, normalizeSub2APIUploadSummary } from "./sub2apiUtils";
import { ShieldAlert, KeyRound, Loader2, FileText, RefreshCw, Trash2 } from "lucide-react";
import { Drawer } from "../components/ui";

function OverviewRow({ k, v, mono = true }) {
  return (
    <div className="flex justify-between gap-4 border-b border-slate-50 py-2 text-[13px]">
      <span className="shrink-0 text-slate-400">{k}</span>
      <span className={`${mono ? "mono " : ""}break-all text-right text-slate-700`}>{v}</span>
    </div>
  );
}

export function AccountsDetailDrawer({
  detail, detailData, detailTab, setDetailTab, detailBusy,
  totpInput, setTotpInput, savingTotp, saveTotp,
  onClose, onEditNote, onDelete, onRefreshSingle, refreshBusy,
}) {
  if (!detail) return null;
  const cred = credState(detail);
  const sub2api = normalizeSub2APIUploadSummary(detail.sub2api_upload_summary);
  const sub2apiBadge = sub2apiUploadBadge(detail.sub2api_upload_summary);

  return (
    <Drawer open={!!detail} onClose={onClose} width={680}
      title={`账号详情 · acc_${detail.id}`}
      footer={
        <>
          <Button variant="dangerSoft" icon={<Trash2 size={13} />} onClick={onDelete}>删除账号</Button>
          <Button variant="secondary" icon={refreshBusy ? <Loader2 size={13} className="animate-spin" /> : <RefreshCw size={13} />}
            onClick={onRefreshSingle} disabled={refreshBusy} title="向远端查询身份/套餐/图像额度，必要时续期凭据">
            {refreshBusy ? "刷新中…" : "刷新信息"}
          </Button>
        </>
      }>
      <div>
        <div className="border-b border-slate-200 px-4 py-3">
          <div className="flex items-center justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="mono truncate text-[15px] font-semibold text-slate-800">{detail.email || detail.phone || `acc_${detail.id}`}</span>
                <Badge color={accStatus(detail)[0]} dot>{accStatus(detail)[1]}</Badge>
              </div>
              <div className="mono mt-0.5 text-xs text-slate-400">acc_{detail.id} · {detail.plan_type || "free"} · 创建 {detail.created_at ? fmtAgo(new Date(detail.created_at).getTime()) : "—"}</div>
            </div>
            <Button variant="ghost" size="sm" icon={<FileText size={13} />} onClick={onEditNote}>编辑备注</Button>
          </div>
        </div>
        <Tabs tabs={[
          { key: "info", label: "概览" },
          { key: "cred", label: "凭据" },
          { key: "totp", label: "2FA" },
        ]} active={detailTab} onChange={setDetailTab} />
        <div className="p-4">
          {detailTab === "info" && (
            <div className="space-y-4">
              {/* 资产状态卡片行：凭据 / 2FA / Sub2API / Profile */}
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-lg border border-slate-200 bg-white p-3">
                  <div className="text-[11px] text-slate-400">凭据状态</div>
                  <div className="mt-1.5 flex items-center gap-2">
                    <Badge color={cred.color} dot>{cred.label}</Badge>
                    <span className="text-[11px] text-slate-400">{tokenExpiryText(detail)}</span>
                  </div>
                </div>
                <div className="rounded-lg border border-slate-200 bg-white p-3">
                  <div className="text-[11px] text-slate-400">2FA</div>
                  <div className="mt-1.5">
                    {(detailData?.totp_secret ?? detail.totp_secret) ? <Badge color="success" dot>已绑定</Badge> : <Badge color="neutral" dot>未绑定</Badge>}
                  </div>
                </div>
                <div className="rounded-lg border border-slate-200 bg-white p-3">
                  <div className="text-[11px] text-slate-400">Sub2API</div>
                  <div className="mt-1.5 flex items-center gap-2">
                    <Badge color={sub2apiBadge.color} dot>{sub2apiBadge.label}</Badge>
                    {sub2api.last_error && <span className="truncate text-[11px] text-red-500" title={sub2api.last_error}>{sub2api.last_error}</span>}
                  </div>
                </div>
                <div className="rounded-lg border border-slate-200 bg-white p-3">
                  <div className="text-[11px] text-slate-400">浏览器 Profile</div>
                  <div className="mt-1.5">
                    {detail.profile_path ? <Badge color="success" dot>已绑定</Badge> : <Badge color="warning" dot>缺失</Badge>}
                  </div>
                </div>
              </div>
              <div>
                <OverviewRow k="账号 ID" v={`acc_${detail.id}`} />
                <OverviewRow k="邮箱" v={detailData?.email || detail.email || "—"} />
                <OverviewRow k="手机号" v={detail.phone || "—"} />
                <OverviewRow k="邮箱来源" v={detailData?.mail_provider || detail.mail_provider || "unknown"} />
                <OverviewRow k="Codex OAuth" v={(detailData?.oauth_eligible ?? detail.oauth_eligible) ? "允许" : `禁止${detailData?.oauth_block_reason ? `：${detailData.oauth_block_reason}` : ""}`} />
                <OverviewRow k="计划类型" v={detailData?.plan_type || detail.plan_type || "—"} />
                <OverviewRow k="代理" v={detail.proxy || "—"} />
                <OverviewRow k="创建时间" v={detail.created_at ? fmtTime(new Date(detail.created_at).getTime()) : "—"} />
                <OverviewRow k="标签" v={detailData?.tag || detail.tag || "—"} mono={false} />
                <OverviewRow k="备注" v={detailData?.note || detail.note || "—"} mono={false} />
              </div>
            </div>
          )}

          {detailTab === "cred" && (
            <div className="space-y-3">
              {detailBusy && !detailData ? (
                <div className="py-8 text-center text-xs text-slate-400"><Loader2 size={16} className="mx-auto mb-2 animate-spin" />加载完整凭据…</div>
              ) : (
                [
                  { k: "password", label: "密码", kind: "password", v: detailData?.password },
                  { k: "access_token", label: "Access Token", kind: "token", v: detailData?.access_token },
                  { k: "refresh_token", label: "Refresh Token", kind: "token", v: detailData?.refresh_token },
                  { k: "id_token", label: "ID Token", kind: "token", v: detailData?.id_token },
                  { k: "totp_secret", label: "TOTP Secret", kind: "totp", v: detailData?.totp_secret },
                ].map((f) => (
                  <div key={f.k} className="rounded-md border border-slate-100 px-3 py-2.5">
                    <div className="mb-1 flex items-center justify-between text-[11px] text-slate-400">
                      <span>{f.label}</span>
                      <span className="flex items-center gap-1"><ShieldAlert size={11} />默认脱敏</span>
                    </div>
                    <SecretField value={f.v || ""} mask="••••••••" label={f.label} kind={f.kind} />
                  </div>
                ))
              )}
              <div className="rounded-md bg-slate-50 px-3 py-2 text-[11px] leading-relaxed text-slate-500">
                完整凭据仅在账号详情中受控展示：点击眼睛显示明文，30 秒后自动隐藏。若 Refresh Token 为空，请到左侧 Codex OAuth 页面统一补授权。
              </div>
            </div>
          )}

          {detailTab === "totp" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between rounded-md border border-slate-100 px-3 py-2.5">
                <div>
                  <div className="text-[13px] font-medium text-slate-700">TOTP 绑定状态</div>
                  <div className="mt-0.5 text-[11px] text-slate-400">{detailData?.totp_secret ? `已绑定（${detailData.totp_secret_masked}）` : "未绑定 2FA"}</div>
                </div>
                {detailData?.totp_secret ? <Badge color="success" dot>已绑定</Badge> : <Badge color="neutral" dot>未绑定</Badge>}
              </div>
              {detailData?.totp_secret && (
                <div className="rounded-md border border-slate-100 px-3 py-2.5">
                  <div className="mb-1 text-[11px] text-slate-400">当前 Secret（默认脱敏）</div>
                  <SecretField value={detailData.totp_secret} mask={detailData.totp_secret_masked || "••••••••"} label="TOTP Secret" kind="totp" />
                </div>
              )}
              <div className="rounded-md border border-slate-200 p-3">
                <div className="mb-1.5 text-xs font-medium text-slate-600">手工写入 / 更新 TOTP Secret</div>
                <div className="flex gap-2">
                  <Input value={totpInput} onChange={(e) => setTotpInput(e.target.value)} placeholder="base32，如 JBSWY3DPEHPK3PXP（留空并保存 = 清空绑定）" className="mono" />
                  <Button icon={savingTotp ? <Loader2 size={13} className="animate-spin" /> : <KeyRound size={13} />} onClick={saveTotp} disabled={savingTotp} className="shrink-0">
                    {savingTotp ? "保存中…" : "保存"}
                  </Button>
                </div>
                <div className="mt-1.5 text-[11px] text-slate-400">Secret 仅接受 A-Z、2-7 的 base32 字符。</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </Drawer>
  );
}
