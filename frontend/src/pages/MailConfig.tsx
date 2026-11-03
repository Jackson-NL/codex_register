import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Cloud,
  Database,
  Inbox,
  Loader2,
  RefreshCw,
  RotateCcw,
  Save,
  Settings2,
  ShieldCheck,
} from "lucide-react";
import { api } from "../api";
import { useApp } from "../context/AppContext";
import { Button } from "../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import { MailProviderSettings, MailTabs } from "../components/MailConfigPanels";
import { MailImportDialog } from "../components/MailImportDialog";
import {
  MailPoolPanel,
  type MailPoolAction,
} from "../components/MailPoolPanel";
import {
  buildMailPatch,
  createMailDraft,
  mailTime,
  PROVIDER_NAMES,
  validateMailProvider,
  type CfDraft,
  type IcloudDraft,
  type MailDraft,
  type MailProvider,
  type MailSnapshot,
  type MailTestResult,
} from "../lib/mailConfig";
import "./MailConfig.css";
import { useMailDraftGuard } from "../lib/useMailDraftGuard";

interface Confirmation {
  title: string;
  description: string;
  execute: () => Promise<void>;
}
interface PoolResult {
  affected?: number;
  skipped?: { id?: number; reason?: string }[];
}
const ACTION_NAMES: Record<MailPoolAction, string> = {
  poolRequeue: "回收",
  poolRelease: "释放",
  poolDisable: "停用",
  poolEnable: "启用",
  poolVerify: "核验凭据",
  poolRemove: "移除",
};

// Backend validation responses can contain reflected input. Never echo URL credentials or raw validation JSON.
function errorMessage(error: unknown, secrets: string[] = []): string {
  let value = error instanceof Error ? error.message : "未知错误，请重试";
  if (/^[\[{]/.test(value) || /"input"\s*:/.test(value))
    return "请求校验失败，请检查字段格式（敏感输入已隐藏）";
  for (const secret of secrets.filter(Boolean))
    value = value.split(secret).join("[凭据已隐藏]");
  return value
    .replace(/https?:\/\/[^\s<>"']+/gi, "[服务地址已隐藏]")
    .replace(/eyJ[\w-]+\.[\w-]+\.[\w-]+/g, "[令牌已隐藏]")
    .slice(0, 500);
}

export default function MailConfig() {
  const { toast } = useApp();
  const [snapshot, setSnapshot] = useState<MailSnapshot | null>(null);
  const [draft, setDraft] = useState<MailDraft | null>(null);
  const [saved, setSaved] = useState<MailDraft | null>(null);
  const [provider, setProvider] = useState<MailProvider>("cf_temp_email");
  const [section, setSection] = useState<"settings" | "pool">("settings");
  const [loadError, setLoadError] = useState("");
  const [retry, setRetry] = useState(0);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [importTarget, setImportTarget] = useState<MailProvider | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [lastTest, setLastTest] = useState<MailTestResult | null>(null);
  const [operationError, setOperationError] = useState("");
  const [operationNote, setOperationNote] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoadError("");
    api.mailConfig
      .get()
      .then((data: MailSnapshot) => {
        if (cancelled) return;
        const initial = createMailDraft(data);
        setSnapshot(data);
        setDraft(initial);
        setSaved(initial);
        setProvider(data.provider);
        setLastTest(
          data.test_status
            ? {
                ...data.test_status,
                message: errorMessage(new Error(data.test_status.message)),
              }
            : null,
        );
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [retry]);

  const dirty =
    draft && saved
      ? Object.keys(buildMailPatch(draft, saved)).length > 0
      : false;
  useMailDraftGuard(dirty, busy);

  if (!snapshot || !draft || !saved) {
    return (
      <div className="mail-page">
        <h1>邮箱资源中心</h1>
        {loadError ? (
          <div className="mail-load-error" role="alert">
            <AlertTriangle />
            <h2>邮箱配置加载失败</h2>
            <p>{loadError}</p>
            <Button onClick={() => setRetry(retry + 1)}>重试加载</Button>
          </div>
        ) : (
          <div className="mail-empty" role="status">
            <Loader2 className="animate-spin" />
            <p>正在加载邮箱资源…</p>
          </div>
        )}
      </div>
    );
  }
  const currentSnapshot = snapshot,
    currentDraft = draft,
    currentSaved = saved;
  const count = (target: MailProvider) =>
    target === "icloud"
      ? currentSnapshot.icloud.accounts_count
      : currentSnapshot.cf_temp_email.custom_pool_count;

  async function run(task: () => Promise<void>): Promise<boolean> {
    if (lock.current) return false;
    lock.current = true;
    setBusy(true);
    setOperationError("");
    setOperationNote("");
    try {
      await task();
      return true;
    } catch (error) {
      const message = errorMessage(error, [
        currentDraft.cf_temp_email.site_password,
        currentDraft.cf_temp_email.inbox_jwt,
      ]);
      setOperationError(message);
      toast(`邮箱操作失败：${message}`, "error");
      return false;
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  function patchCf<K extends keyof CfDraft>(key: K, value: CfDraft[K]) {
    setDraft(
      (state) =>
        state && {
          ...state,
          cf_temp_email: { ...state.cf_temp_email, [key]: value },
        },
    );
    setErrors({});
  }
  function patchIcloud<K extends keyof IcloudDraft>(
    key: K,
    value: IcloudDraft[K],
  ) {
    setDraft(
      (state) =>
        state && { ...state, icloud: { ...state.icloud, [key]: value } },
    );
    setErrors({});
  }
  async function refreshResources() {
    await run(async () => {
      const data: MailSnapshot = await api.mailConfig.get();
      setSnapshot(data);
      setOperationNote("资源状态已刷新；未保存的服务配置保持不变。");
    });
  }
  function validate(target: MailProvider): boolean {
    const validation = validateMailProvider(
      target,
      currentDraft,
      currentSnapshot,
    );
    if (!Object.keys(validation).length) return true;
    setProvider(target);
    setSection("settings");
    setErrors(validation);
    setOperationError(
      `${PROVIDER_NAMES[target]} 配置需要修正：${Object.values(validation).join("；")}`,
    );
    toast("请修正配置后再操作", "warning");
    return false;
  }
  async function save() {
    const patch = buildMailPatch(currentDraft, currentSaved);
    if (!Object.keys(patch).length) return;
    for (const target of ["cf_temp_email", "icloud"] as const) {
      if ((patch[target] || patch.provider === target) && !validate(target))
        return;
    }
    if (
      patch.provider &&
      (!currentDraft[patch.provider].enabled ||
        (patch.provider === "icloud" && !currentSnapshot.icloud.accounts_count))
    ) {
      setOperationError(
        "注册来源必须已启用；iCloud 还需要至少一条已导入资源。",
      );
      return;
    }
    await run(async () => {
      const data: MailSnapshot = await api.mailConfig.save(patch);
      const normalized = createMailDraft(data);
      setSnapshot(data);
      setDraft(normalized);
      setSaved(normalized);
      setErrors({});
      toast("邮箱配置已保存", "success");
    });
  }
  async function testService() {
    if (!validate(provider)) return;
    await run(async () => {
      const full =
        provider === "cf_temp_email"
          ? { ...currentDraft.cf_temp_email }
          : { ...currentDraft.icloud };
      // Empty inputs mean keep the stored credentials, not replace them with masks or empty strings.
      if ("site_password" in full && !full.site_password)
        delete (full as Partial<CfDraft>).site_password;
      if ("inbox_jwt" in full && !full.inbox_jwt)
        delete (full as Partial<CfDraft>).inbox_jwt;
      const result: MailTestResult = await api.mailConfig.test({
        provider,
        config: { [provider]: full },
      });
      const safe = {
        ...result,
        message: errorMessage(new Error(result.message), [
          currentDraft.cf_temp_email.site_password,
          currentDraft.cf_temp_email.inbox_jwt,
        ]),
      };
      setLastTest(safe);
      if (!result.ok) setOperationError(`连接测试失败：${safe.message}`);
      toast(
        result.ok ? "邮箱连接测试成功" : "邮箱连接测试失败，查看诊断详情",
        result.ok ? "success" : "error",
      );
    });
  }
  async function importPool(text: string, append: boolean): Promise<boolean> {
    const target = importTarget;
    if (!target) return false;
    const oldCount = count(target);
    return run(async () => {
      // Pool writes are deliberately isolated: no draft provider, endpoint, enabled flag, or credentials.
      const data: MailSnapshot = await api.mailConfig.save(
        target === "icloud"
          ? { icloud: { accounts_pool: text, accounts_pool_append: append } }
          : {
              cf_temp_email: { custom_pool: text, custom_pool_append: append },
            },
      );
      setSnapshot(data);
      const after =
        target === "icloud"
          ? data.icloud.accounts_count
          : data.cf_temp_email.custom_pool_count;
      toast(
        append
          ? `已新增 ${Math.max(0, after - oldCount)} 条资源，现有 ${after} 条`
          : `已替换邮箱池，现有 ${after} 条资源`,
        "success",
      );
    });
  }
  async function poolAction(
    method: MailPoolAction,
    ids: number[],
    confirmed = false,
    force = false,
  ): Promise<void> {
    if (!ids.length && method !== "poolVerify") return;
    if (!confirmed && ["poolRemove", "poolRelease"].includes(method)) {
      setConfirmation({
        title: `${ACTION_NAMES[method]} ${ids.length} 个邮箱？`,
        description:
          method === "poolRemove"
            ? "从池中移除选中资源，不会删除账号管理中的历史账号。正在使用的资源可能被服务端跳过。"
            : "释放租约会使邮箱重新可分配；请确认对应任务已停止，避免并发复用。",
        execute: () => poolAction(method, ids, true),
      });
      return;
    }
    await run(async () => {
      const payload = {
        ids,
        ...(method === "poolRelease" ? { outcome: "unused" } : {}),
        ...(force ? { force: true } : {}),
      };
      const result: PoolResult = await api.mailConfig[method](payload);
      const skipped = result.skipped?.length ?? 0;
      toast(
        `${ACTION_NAMES[method]}完成：${result.affected ?? 0} 个${skipped ? `，跳过 ${skipped} 个` : ""}`,
        skipped ? "warning" : "success",
      );
      if (method === "poolVerify" && !ids.length)
        setOperationNote("已核验一批已使用地址（服务端每次最多 500 条）。");
      if (method === "poolRequeue" && !force && skipped) {
        const skippedIds =
          result.skipped?.flatMap((item) =>
            item.id === undefined ? [] : [item.id],
          ) ?? [];
        if (skippedIds.length)
          setConfirmation({
            title: `强制回收 ${skippedIds.length} 个邮箱？`,
            description:
              "这些资源无法证明尚未消费，强制回收可能复用已注册或半消费邮箱。建议先人工确认；取消不会回收被跳过项。",
            execute: () => poolAction(method, skippedIds, true, true),
          });
      }
      try {
        setSnapshot(await api.mailConfig.get());
      } catch {
        setOperationNote(
          "操作已执行，但资源刷新失败。请点「刷新资源」核对结果，不要重复执行刚才的操作。",
        );
      }
    });
  }
  const activeName = PROVIDER_NAMES[snapshot.provider];
  const activeConfig = snapshot[snapshot.provider];
  const poolMissing =
    snapshot.provider === "icloud"
      ? !snapshot.icloud.accounts_count
      : snapshot.cf_temp_email.address_mode === "custom_pool" &&
        !snapshot.cf_temp_email.custom_pool_count;
  const counts = snapshot.cf_temp_email.custom_pool_status_counts || {};
  const testMatches = lastTest?.provider === provider;

  return (
    <div className="mail-page">
      <header className="mail-page-header">
        <div>
          <span className="mail-eyebrow">MAIL RESOURCES</span>
          <h1>邮箱资源中心</h1>
          <p>接入服务、管理邮箱池，让注册资源一目了然。</p>
        </div>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() => void refreshResources()}
        >
          <RefreshCw className={busy ? "animate-spin" : ""} />
          刷新资源
        </Button>
      </header>
      <section className="mail-overview" aria-label="邮箱资源概览">
        <article className="mail-source-card" data-testid="saved-mail-source">
          <span>
            <Cloud size={15} />
            当前注册来源
          </span>
          <strong>{activeName}</strong>
          <p>
            {!activeConfig.enabled
              ? "服务未启用，请检查配置"
              : poolMissing
                ? "缺少邮箱池，请先导入资源"
                : "配置已启用 · 收码能力需单独测试"}
          </p>
        </article>
        <article>
          <span>
            <Database size={15} />
            Cloudflare 地址池
          </span>
          <strong>
            {snapshot.cf_temp_email.custom_pool_count}
            <small>个地址</small>
          </strong>
          <p>
            可用 {counts.unused ?? 0} · 使用中 {counts.in_use ?? 0} · 失败{" "}
            {counts.failed ?? 0}
          </p>
        </article>
        <article>
          <span>
            <Inbox size={15} />
            iCloud 基础邮箱
          </span>
          <strong>
            {snapshot.icloud.accounts_count}
            <small>个邮箱</small>
          </strong>
          <p>
            预计剩余 {snapshot.icloud.remaining_rounds ?? "—"} 轮 · 耗尽{" "}
            {snapshot.icloud.exhausted_roots ?? "—"}
          </p>
        </article>
      </section>
      {(!activeConfig.enabled || poolMissing) && (
        <div className="mail-notice mail-warning">
          <AlertTriangle size={17} />
          <span>
            当前注册来源配置不完整，启动任务前请先检查对应服务和邮箱池。
          </span>
        </div>
      )}
      <div className="mail-workspace">
        <aside className="mail-provider-rail" aria-label="邮箱服务列表">
          <div className="mail-rail-caption">
            已接入服务 <span>02</span>
          </div>
          <MailTabs
            label="选择邮箱服务"
            value={provider}
            onChange={(value) => {
              setProvider(value);
              setErrors({});
            }}
            options={[
              {
                value: "cf_temp_email",
                label: "Cloudflare",
                icon: <Cloud size={17} />,
              },
              { value: "icloud", label: "iCloud", icon: <Inbox size={17} /> },
            ]}
          />
          <div className="mail-rail-tip">
            <ShieldCheck size={18} />
            <strong>配置与资源独立保存</strong>
            <p>
              切换服务仅供查看。导入、回收等资源操作立即生效，不会提交未保存的配置。
            </p>
          </div>
          <Link className="mail-register-link" to="/register">
            前往注册工作台
            <ArrowRight size={14} />
          </Link>
        </aside>
        <div className="mail-work-area">
          <div className="mail-provider-heading">
            <div
              className={`mail-provider-icon ${provider === "icloud" ? "mail-icloud" : ""}`}
            >
              {provider === "icloud" ? <Inbox /> : <Cloud />}
            </div>
            <div className="mail-provider-description">
              <h2>
                {PROVIDER_NAMES[provider]}
                <span className="mail-pill">
                  {snapshot[provider].enabled ? "已启用" : "未启用"}
                </span>
              </h2>
              <p>
                {provider === "icloud"
                  ? "预置邮箱 + 独立收信 API"
                  : "临时邮箱 / 自定义地址池 + 统一收件箱"}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || draft.provider === provider}
              onClick={() => setDraft({ ...draft, provider })}
            >
              {draft.provider === provider ? (
                snapshot.provider === provider ? (
                  <>
                    <CheckCircle2 />
                    当前注册来源
                  </>
                ) : (
                  "待保存为来源"
                )
              ) : (
                "设为注册来源"
              )}
            </Button>
          </div>
          <MailTabs
            label="邮箱资源功能"
            value={section}
            onChange={setSection}
            options={[
              {
                value: "settings",
                label: "服务配置",
                icon: <Settings2 size={15} />,
              },
              { value: "pool", label: "邮箱池", icon: <Database size={15} /> },
            ]}
          />
          <div className="mail-work-content">
            <fieldset disabled={busy}>
              {section === "settings" ? (
                <MailProviderSettings
                  provider={provider}
                  draft={draft}
                  snapshot={snapshot}
                  errors={errors}
                  patchCf={patchCf}
                  patchIcloud={patchIcloud}
                />
              ) : (
                <MailPoolPanel
                  key={provider}
                  provider={provider}
                  snapshot={snapshot}
                  busy={busy}
                  onImport={() => {
                    setOperationError("");
                    setImportTarget(provider);
                  }}
                  onClear={() =>
                    setConfirmation({
                      title: "清空 iCloud 邮箱池？",
                      description: `将移除已保存的 ${snapshot.icloud.accounts_count} 条邮箱及收信 API。此操作立即生效，不会删除历史账号；若仍将 iCloud 设为来源，后续注册将无邮箱可用。`,
                      execute: async () => {
                        await run(async () => {
                          const data: MailSnapshot = await api.mailConfig.save({
                            icloud: { accounts_pool: "" },
                          });
                          setSnapshot(data);
                          toast("iCloud 邮箱池已清空", "success");
                        });
                      },
                    })
                  }
                  onAction={poolAction}
                />
              )}
            </fieldset>
          </div>
          <div className="mail-service-check">
            <div>
              <span
                className={`mail-check-dot ${testMatches && lastTest?.ok ? "mail-check-ok" : ""}`}
              />
              <div>
                <strong>
                  {testMatches
                    ? lastTest.ok
                      ? "最近测试通过"
                      : "最近测试未通过"
                    : "此服务尚无测试记录"}
                </strong>
                <p>
                  {testMatches
                    ? lastTest.message
                    : "测试当前服务及其配置草稿，不会保存配置；可能创建测试邮箱。"}
                </p>
                {testMatches && lastTest.tested_at && (
                  <p>{mailTime(lastTest.tested_at)} · 仅代表当次测试结果</p>
                )}
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => void testService()}
            >
              <RefreshCw />
              测试此服务
            </Button>
          </div>
        </div>
      </div>
      {operationError && (
        <div className="mail-notice mail-warning" role="alert">
          <AlertTriangle size={17} />
          <span>{operationError}</span>
        </div>
      )}
      {operationNote && (
        <div className="mail-notice" role="status">
          <ShieldCheck size={17} />
          <span>{operationNote}</span>
        </div>
      )}
      <footer className="mail-save-bar">
        <div>
          {dirty ? (
            <>
              <span className="mail-dirty-dot" />
              <strong>有未保存修改</strong>
              <span>
                配置保存后生效
                {draft.provider !== snapshot.provider
                  ? ` · 来源将切换为 ${PROVIDER_NAMES[draft.provider]}`
                  : ""}
              </span>
            </>
          ) : (
            <>
              <CheckCircle2 size={15} />
              <strong>服务配置已同步</strong>
              <span>最近保存 {mailTime(snapshot.updated_at)}</span>
            </>
          )}
        </div>
        <div>
          <Button
            variant="outline"
            disabled={!dirty || busy}
            onClick={() => {
              setDraft(createMailDraft(snapshot));
              setSaved(createMailDraft(snapshot));
              setErrors({});
              setOperationError("");
            }}
          >
            <RotateCcw />
            撤销修改
          </Button>
          <Button
            disabled={!dirty || busy}
            aria-busy={busy}
            onClick={() => void save()}
          >
            <Save />
            保存配置
          </Button>
        </div>
      </footer>
      {importTarget && (
        <MailImportDialog
          provider={importTarget}
          count={count(importTarget)}
          busy={busy}
          error={operationError}
          onClose={() => setImportTarget(null)}
          onImport={importPool}
        />
      )}
      <Dialog
        open={!!confirmation}
        onOpenChange={(open) => {
          if (!open && !busy) setConfirmation(null);
        }}
      >
        <DialogContent
          className="mail-dialog mail-confirm-dialog"
          closeLabel="关闭确认"
          showClose={!busy}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onInteractOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <DialogHeader>
            <DialogTitle>{confirmation?.title}</DialogTitle>
            <DialogDescription>{confirmation?.description}</DialogDescription>
          </DialogHeader>
          <div className="mail-dialog-footer">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setConfirmation(null)}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={async () => {
                const pending = confirmation;
                setConfirmation(null);
                await pending?.execute();
              }}
            >
              确认操作
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
