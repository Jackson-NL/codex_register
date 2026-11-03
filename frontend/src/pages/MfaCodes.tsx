import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type ReactNode,
} from "react";
import {
  Check,
  Clock3,
  Copy,
  Download,
  FileText,
  KeyRound,
  LockKeyhole,
  Mail,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  Upload,
} from "lucide-react";
import { useApp } from "../context/AppContext";
import { Button } from "../components/ui/button";
import { Card } from "../components/ui/card";
import { Input } from "../components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "../components/ui/dialog";
import {
  exportAccounts,
  generateTotp,
  importAccounts,
  MAX_MFA_BYTES,
  MAX_MFA_JSON_BYTES,
  MAX_MFA_RECORDS,
  parseEmails,
  parseEntries,
  parseSecret,
  remainingSeconds,
  type MfaAccount,
} from "../lib/mfa";
import { lookupMfa, type OnlineCode } from "../lib/mfaApi";
import "./MfaCodes.css";

const SAMPLE =
  "demo@example.invalid----JBSWY3DPEHPK3PXP\notpauth://totp/Example%3Aalice%40example.invalid?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Example";
const STATUS = {
  ok: "可用",
  not_found: "账号不存在",
  no_secret: "未绑定 2FA",
  invalid_secret: "密钥无效",
  duplicate: "邮箱重复，请先整理账号",
};
const spaced = (code: string) =>
  `${code.slice(0, code.length / 2)} ${code.slice(code.length / 2)}`;

function useLocalCodes(accounts: MfaAccount[], now: number) {
  const [result, setResult] = useState<{
    accounts: MfaAccount[];
    codes: string[];
    steps: string;
    error: boolean;
  } | null>(null);
  const steps = accounts
    .map((account) => Math.floor(now / 1000 / account.period))
    .join(",");
  useEffect(() => {
    let cancelled = false;
    const time = Date.now();
    if (!accounts.length) setResult(null);
    else
      void Promise.all(accounts.map((account) => generateTotp(account, time)))
        .then((codes) => {
          if (!cancelled)
            setResult({
              accounts,
              codes,
              steps: accounts
                .map((account) => Math.floor(time / 1000 / account.period))
                .join(","),
              error: false,
            });
        })
        .catch(() => {
          if (!cancelled)
            setResult({ accounts, codes: [], steps, error: true });
        });
    return () => {
      cancelled = true;
    };
    // Time step, rather than each countdown tick, determines HMAC recomputation.
  }, [accounts, steps]);
  const current = result?.accounts === accounts && result.steps === steps;
  return { codes: current ? result.codes : [], error: current && result.error };
}

function PanelHeader({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="mfa-panel-header">
      <div>
        <span className="mfa-eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
      </div>
      <div className="mfa-header-actions">{children}</div>
    </div>
  );
}

function Empty({
  kind = "key",
  children,
}: {
  kind?: "key" | "mail";
  children: ReactNode;
}) {
  return (
    <div className="mfa-empty">
      {kind === "mail" ? (
        <Mail size={30} strokeWidth={1.3} />
      ) : (
        <KeyRound size={31} strokeWidth={1.3} />
      )}
      <p>{children}</p>
    </div>
  );
}

export default function MfaCodes() {
  const { t, toast } = useApp();
  const [now, setNow] = useState(Date.now);
  const [text, setText] = useState("");
  const [entries, setEntries] = useState<MfaAccount[]>([]);
  const [parseErrors, setParseErrors] = useState<string[]>([]);
  const [accounts, setAccounts] = useState<MfaAccount[]>([]);
  const [form, setForm] = useState({
    issuer: "",
    label: "",
    note: "",
    secret: "",
  });
  const [emailText, setEmailText] = useState("");
  const [favorites, setFavorites] = useState<string[]>([]);
  const [activeEmails, setActiveEmails] = useState<string[]>([]);
  const [online, setOnline] = useState<OnlineCode[]>([]);
  const [offset, setOffset] = useState(0);
  const [queryBusy, setQueryBusy] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [exportOpen, setExportOpen] = useState(false);
  const [revealed, setRevealed] = useState<number | null>(null);
  const textFile = useRef<HTMLInputElement>(null);
  const jsonFile = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const fileGeneration = useRef(0);
  const exportTrigger = useRef<HTMLButtonElement>(null);
  const local = useLocalCodes(entries, now),
    saved = useLocalCodes(accounts, now);

  useEffect(() => {
    alive.current = true;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    if (!activeEmails.length) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const load = async () => {
      setQueryBusy(true);
      try {
        const started = Date.now();
        const result = await lookupMfa(activeEmails, controller.signal);
        if (cancelled) return;
        const received = Date.now();
        const delta =
          result.server_time * 1000 + (received - started) / 2 - received;
        setOffset(delta);
        setOnline(result.items);
        setQueryError("");
        const deadline = Math.min(
          ...result.items.map((row) => row.expires_at * 1000),
        );
        timer = setTimeout(
          () => {
            void load();
          },
          Math.max(1000, deadline - (Date.now() + delta) + 150),
        );
      } catch {
        if (cancelled) return;
        setOnline([]);
        setQueryError(t("MFA 查询失败，请检查管理员登录与服务状态"));
        timer = setTimeout(() => {
          void load();
        }, 10000);
      } finally {
        if (!cancelled) setQueryBusy(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timer);
    };
  }, [activeEmails, t]);

  const report = (error: unknown) =>
    toast(t(error instanceof Error ? error.message : "操作失败"), "error");
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast(t("已复制"), "success");
    } catch {
      toast(t("复制失败，请检查浏览器剪贴板权限"), "error");
    }
  };
  const generate = () => {
    try {
      const parsed = parseEntries(text);
      setEntries(parsed.items);
      setParseErrors(parsed.errors);
      setRevealed(null);
      setNow(Date.now());
      // Discard pasted password fields after parsing. Only normalized TOTP data remains.
      if (parsed.items.length)
        setText(
          parsed.items
            .map(
              (row) =>
                `otpauth://totp/${encodeURIComponent(row.label)}?secret=${row.secret}&issuer=${encodeURIComponent(row.issuer)}&algorithm=${row.algorithm.replace("-", "")}&digits=${row.digits}&period=${row.period}`,
            )
            .join("\n"),
        );
      if (!parsed.items.length && !parsed.errors.length)
        toast(t("请先输入密钥数据"), "error");
    } catch (error) {
      setEntries([]);
      setParseErrors([]);
      report(error);
    }
  };
  const clearLocal = () => {
    fileGeneration.current++;
    setText("");
    setEntries([]);
    setParseErrors([]);
    setRevealed(null);
  };
  const clearQuery = () => {
    setActiveEmails([]);
    setEmailText("");
    setOnline([]);
    setQueryError("");
    setQueryBusy(false);
  };
  const query = (emails?: string[]) => {
    try {
      const list = emails ?? parseEmails(emailText);
      if (!list.length) throw new Error("请先加入常用邮箱");
      setOnline([]);
      setQueryError("");
      setActiveEmails([...list]);
    } catch (error) {
      report(error);
    }
  };
  const favorite = () => {
    try {
      const emails = [...new Set([...favorites, ...parseEmails(emailText)])];
      if (emails.length > 100) throw new Error("最多支持 100 个常用邮箱");
      setFavorites(emails);
    } catch (error) {
      report(error);
    }
  };
  const addAccount = () => {
    try {
      if (accounts.length >= MAX_MFA_RECORDS)
        throw new Error("最多支持 200 条记录");
      const account = parseSecret(form.secret);
      account.label = form.label.trim() || account.label;
      account.issuer = form.issuer.trim() || account.issuer;
      account.note = form.note.trim();
      if (!account.label) throw new Error("请输入邮箱或账号");
      if (
        accounts.some(
          (row) => row.label === account.label && row.issuer === account.issuer,
        )
      )
        throw new Error("本页已有同平台账号，请先移除旧记录");
      fileGeneration.current++;
      setAccounts([...accounts, account]);
      setForm({ issuer: "", label: "", note: "", secret: "" });
    } catch (error) {
      report(error);
    }
  };
  const readFile = async (
    event: ChangeEvent<HTMLInputElement>,
    json: boolean,
  ) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    const generation = ++fileGeneration.current;
    try {
      if (file.size > (json ? MAX_MFA_JSON_BYTES : MAX_MFA_BYTES))
        throw new Error("文件过大");
      const content = await file.text();
      if (!alive.current) return;
      if (generation !== fileGeneration.current) {
        toast(t("读取期间数据已更改，请重新导入文件"), "info");
        return;
      }
      if (!json) {
        setText(content);
        return;
      }
      const imported = importAccounts(content);
      if (accounts.length + imported.length > MAX_MFA_RECORDS)
        throw new Error("最多支持 200 条记录");
      const all = [...accounts, ...imported];
      const keys = all.map((row) => `${row.issuer}\u0000${row.label}`);
      if (new Set(keys).size !== keys.length)
        throw new Error("导入存在重复平台账号，请先整理文件");
      setAccounts(all);
      toast(t("已导入本页，刷新后清空"), "success");
    } catch {
      toast(
        t("文件读取失败或格式无效（最多 200 条；TXT 256 KB / JSON 2 MB）"),
        "error",
      );
    }
  };
  const download = () => {
    const url = URL.createObjectURL(
      new Blob([exportAccounts(accounts)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "mfa-accounts.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setExportOpen(false);
  };
  const copyCodes = () => {
    const time = Date.now();
    void Promise.all(
      entries.map(
        async (row) => `${row.label}\t${await generateTotp(row, time)}`,
      ),
    )
      .then((codes) => copy(codes.join("\n")))
      .catch(report);
  };
  const copyLocal = (account: MfaAccount) => {
    void generateTotp(account, Date.now()).then(copy).catch(report);
  };
  const copyOnline = (row: OnlineCode) => {
    if (row.code && row.expires_at * 1000 > Date.now() + offset)
      void copy(row.code);
    else {
      toast(t("验证码已过期，正在重新查询"), "info");
      query(activeEmails);
    }
  };
  const countdown = (remaining: number, period: number) => (
    <span className={`mfa-countdown ${remaining <= 5 ? "is-expiring" : ""}`}>
      <Clock3 size={13} />
      <span>{remaining}s</span>
      <progress max={period} value={remaining} aria-label={t("剩余时间")} />
    </span>
  );

  return (
    <div className="mfa-page">
      <header className="mfa-page-header">
        <div>
          <span className="mfa-eyebrow">TIME-BASED ONE-TIME PASSWORDS</span>
          <h1>{t("2FA / MFA 验证码")}</h1>
          <p>{t("批量生成动态口令，集中管理当前页面的双重验证。")}</p>
        </div>
        <span className="mfa-security-badge">
          <ShieldCheck size={15} />
          {t("本地计算 · 刷新不留存")}
        </span>
      </header>

      <div className="mfa-top-grid">
        <Card className="mfa-panel">
          <PanelHeader eyebrow={t("2FA 密钥输入")} title={t("密钥数据")}>
            <Button
              variant="outline"
              size="sm"
              onClick={() => textFile.current?.click()}
            >
              <FileText size={14} />
              {t("文本文件")}
            </Button>
            <input
              ref={textFile}
              type="file"
              accept=".txt,text/plain"
              hidden
              aria-label={t("导入文本文件")}
              onChange={(event) => {
                void readFile(event, false);
              }}
            />
          </PanelHeader>
          <div className="mfa-panel-body">
            <textarea
              aria-label={t("密钥数据")}
              value={text}
              maxLength={MAX_MFA_BYTES}
              onChange={(event) => setText(event.target.value)}
              spellCheck={false}
              autoComplete="off"
              placeholder={t(
                "Base32 密钥 / otpauth URI / 账号文本\n每行一条，可用 邮箱----密码----密钥",
              )}
            />
            <div className="mfa-generate-actions">
              <Button onClick={generate}>
                <RefreshCw size={15} />
                {t("生成验证码")}
              </Button>
              <Button variant="outline" onClick={() => setText(SAMPLE)}>
                {t("载入示例")}
              </Button>
            </div>
            {parseErrors.length > 0 && (
              <div className="mfa-errors" role="alert">
                {parseErrors.slice(0, 5).map((error) => (
                  <p key={error}>{t(error)}</p>
                ))}
                {parseErrors.length > 5 && <p>+{parseErrors.length - 5}</p>}
              </div>
            )}
          </div>
          <footer className="mfa-panel-footer">
            <span>
              {entries.length
                ? `${entries.length} ${t("条有效数据")}`
                : t("支持 Base32、otpauth 与账号文本")}
            </span>
            <Button
              variant="ghost"
              size="sm"
              aria-label={t("清空密钥数据")}
              onClick={clearLocal}
            >
              {t("清空")}
            </Button>
          </footer>
        </Card>

        <Card className="mfa-panel">
          <PanelHeader eyebrow={t("动态结果")} title={t("验证码列表")}>
            <span className="mfa-live">
              <span />
              {t("实时更新")}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={
                !entries.length || local.codes.length !== entries.length
              }
              onClick={copyCodes}
            >
              <Copy size={14} />
              {t("复制全部")}
            </Button>
          </PanelHeader>
          {local.error && (
            <p role="alert" className="mfa-errors">
              {t(
                "验证码计算失败，请使用支持 Web Crypto 的 HTTPS 或 localhost 浏览器",
              )}
            </p>
          )}
          {!entries.length ? (
            <Empty>{t("输入密钥，开始生成验证码")}</Empty>
          ) : (
            <div className="mfa-table-wrap">
              <table className="mfa-table">
                <thead>
                  <tr>
                    <th>{t("账号")}</th>
                    <th>{t("验证码")}</th>
                    <th>{t("剩余时间")}</th>
                    <th>{t("密钥 / 参数")}</th>
                    <th>{t("操作")}</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((row, index) => (
                    <tr key={index}>
                      <td>
                        <strong>{row.label}</strong>
                        <small>{row.issuer || "TOTP"}</small>
                      </td>
                      <td>
                        <code
                          className="mfa-code"
                          data-testid={`local-code-${index}`}
                        >
                          {local.codes[index]
                            ? spaced(local.codes[index])
                            : "—"}
                        </code>
                      </td>
                      <td>
                        {countdown(
                          remainingSeconds(now, row.period),
                          row.period,
                        )}
                      </td>
                      <td>
                        <button
                          className="mfa-secret-toggle"
                          aria-label={t(
                            revealed === index ? "隐藏密钥" : "显示密钥",
                          )}
                          onClick={() =>
                            setRevealed(revealed === index ? null : index)
                          }
                        >
                          {revealed === index ? row.secret : "•••• ••••"}
                        </button>
                        <small>
                          {row.algorithm} · {row.digits} {t("位")} ·{" "}
                          {row.period}s
                        </small>
                      </td>
                      <td>
                        <Button
                          size="icon"
                          variant="ghost"
                          aria-label={`${t("复制验证码")} ${row.label}`}
                          disabled={!local.codes[index]}
                          onClick={() => copyLocal(row)}
                        >
                          <Copy size={15} />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <footer className="mfa-panel-footer">
            <span>
              {entries.length} {t("个验证码")}
            </span>
            <span>
              <span className="mfa-dot" />
              {t("本机时间")}
            </span>
          </footer>
        </Card>
      </div>

      <div className="mfa-bottom-grid">
        <Card className="mfa-panel">
          <PanelHeader
            eyebrow={t("项目账号库查询")}
            title={t("在线 MFA 验证码")}
          >
            <Button
              variant="outline"
              size="sm"
              disabled={!favorites.length}
              onClick={() => query(favorites)}
            >
              <RefreshCw size={14} />
              {t("查询本页常用")}
            </Button>
          </PanelHeader>
          <div className="mfa-panel-body">
            <textarea
              className="mfa-email-input"
              aria-label={t("查询邮箱")}
              value={emailText}
              maxLength={30000}
              onChange={(event) => setEmailText(event.target.value)}
              placeholder={t("输入一个或多个邮箱，支持换行、逗号分隔")}
            />
            <div className="mfa-query-actions">
              <Button onClick={() => query()} disabled={queryBusy}>
                <Search size={15} />
                {t(queryBusy ? "查询中…" : "查询验证码")}
              </Button>
              <Button
                variant="outline"
                onClick={favorite}
                disabled={!emailText.trim()}
              >
                <Plus size={14} />
                {t("加入本页常用")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="mfa-push-right"
                aria-label={t("清空邮箱查询")}
                onClick={clearQuery}
              >
                {t("清空")}
              </Button>
            </div>
            <div className="mfa-favorites">
              {favorites.length ? (
                favorites.map((email) => (
                  <button
                    key={email}
                    onClick={() =>
                      setFavorites(favorites.filter((value) => value !== email))
                    }
                    aria-label={`${t("移除常用")} ${email}`}
                  >
                    {email}
                    <span>×</span>
                  </button>
                ))
              ) : (
                <small>{t("尚未加入常用邮箱，仅本页保留")}</small>
              )}
            </div>
          </div>
          {queryError && (
            <p className="mfa-errors" role="alert">
              {queryError}
            </p>
          )}
          {!online.length ? (
            <Empty kind="mail">
              {t(
                queryBusy
                  ? "正在查询项目账号库…"
                  : "输入邮箱查询已绑定的验证码",
              )}
            </Empty>
          ) : (
            <div className="mfa-online-list">
              {online.map((row) => {
                const remaining = Math.max(
                  0,
                  Math.ceil(row.expires_at - (now + offset) / 1000),
                );
                const valid = row.status === "ok" && row.code && remaining > 0;
                return (
                  <div className="mfa-online-row" key={row.email}>
                    <div>
                      <strong>{row.email}</strong>
                      <small>{t(STATUS[row.status])}</small>
                    </div>
                    <code className="mfa-code">
                      {valid ? spaced(row.code!) : "—"}
                    </code>
                    {valid && countdown(remaining, row.period)}
                    <Button
                      size="icon"
                      variant="ghost"
                      disabled={!valid}
                      aria-label={`${t("复制验证码")} ${row.email}`}
                      onClick={() => copyOnline(row)}
                    >
                      <Copy size={15} />
                    </Button>
                  </div>
                );
              })}
            </div>
          )}
          <footer className="mfa-panel-footer">
            <span>{t(activeEmails.length ? "到期自动刷新" : "等待查询")}</span>
            <span>{t("只读项目账号库，不返回密钥")}</span>
          </footer>
        </Card>

        <Card className="mfa-panel">
          <PanelHeader eyebrow={t("浏览器内存")} title={t("当前页面 MFA 账户")}>
            <Button
              variant="outline"
              size="sm"
              onClick={() => jsonFile.current?.click()}
            >
              <Upload size={14} />
              {t("导入 JSON")}
            </Button>
            <Button
              ref={exportTrigger}
              variant="outline"
              size="sm"
              disabled={!accounts.length}
              onClick={() => setExportOpen(true)}
            >
              <Download size={14} />
              {t("导出 JSON")}
            </Button>
            <input
              ref={jsonFile}
              type="file"
              accept=".json,application/json"
              hidden
              aria-label={t("导入 MFA JSON")}
              onChange={(event) => {
                void readFile(event, true);
              }}
            />
          </PanelHeader>
          <div className="mfa-panel-body">
            <div className="mfa-account-fields">
              <label>
                {t("平台")}
                <Input
                  value={form.issuer}
                  maxLength={100}
                  onChange={(event) =>
                    setForm({ ...form, issuer: event.target.value })
                  }
                  placeholder="OpenAI"
                />
              </label>
              <label>
                {t("邮箱 / 账号")}
                <Input
                  value={form.label}
                  maxLength={254}
                  onChange={(event) =>
                    setForm({ ...form, label: event.target.value })
                  }
                  placeholder="name@example.com"
                  autoComplete="off"
                />
              </label>
              <label>
                {t("备注")}
                <Input
                  value={form.note}
                  maxLength={500}
                  onChange={(event) =>
                    setForm({ ...form, note: event.target.value })
                  }
                  placeholder={t("可选")}
                />
              </label>
            </div>
            <label className="mfa-secret-field">
              Base32 / otpauth
              <Input
                type="password"
                value={form.secret}
                maxLength={4096}
                onChange={(event) =>
                  setForm({ ...form, secret: event.target.value })
                }
                placeholder={t("Base32 密钥或 otpauth:// URI")}
                autoComplete="off"
              />
            </label>
            <Button onClick={addAccount}>
              <KeyRound size={15} />
              {t("加入本页")}
            </Button>
          </div>
          {saved.error && (
            <p role="alert" className="mfa-errors">
              {t(
                "验证码计算失败，请使用支持 Web Crypto 的 HTTPS 或 localhost 浏览器",
              )}
            </p>
          )}
          {!accounts.length ? (
            <Empty>{t("添加当前页面 MFA 账户")}</Empty>
          ) : (
            <div className="mfa-saved-list">
              {accounts.map((row, index) => (
                <div
                  className="mfa-saved-row"
                  key={`${row.issuer}-${row.label}`}
                >
                  <span className="mfa-account-icon">
                    <LockKeyhole size={17} />
                  </span>
                  <div className="mfa-account-label">
                    <strong>{row.label}</strong>
                    <small>
                      {[row.issuer, row.note].filter(Boolean).join(" · ") ||
                        "TOTP"}
                    </small>
                  </div>
                  <div>
                    <code className="mfa-code">
                      {saved.codes[index] ? spaced(saved.codes[index]) : "—"}
                    </code>
                    {countdown(remainingSeconds(now, row.period), row.period)}
                  </div>
                  <Button
                    size="icon"
                    variant="ghost"
                    disabled={!saved.codes[index]}
                    aria-label={`${t("复制验证码")} ${row.label}`}
                    onClick={() => copyLocal(row)}
                  >
                    <Copy size={14} />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={`${t("移除账号")} ${row.label}`}
                    onClick={() => {
                      fileGeneration.current++;
                      setAccounts(accounts.filter((_, i) => i !== index));
                    }}
                  >
                    <Trash2 size={14} />
                  </Button>
                </div>
              ))}
            </div>
          )}
          <footer className="mfa-panel-footer">
            <span>
              {accounts.length} {t("个本页账户")}
            </span>
            <Button
              variant="ghost"
              size="sm"
              disabled={!accounts.length}
              onClick={() => {
                fileGeneration.current++;
                setAccounts([]);
                setForm({ issuer: "", label: "", note: "", secret: "" });
              }}
            >
              {t("清空本页账户")}
            </Button>
          </footer>
        </Card>
      </div>
      <p className="mfa-privacy-note">
        <Check size={14} />
        {t(
          "手动密钥与常用邮箱仅存在当前页面内存中，离开或刷新即清空；JSON 导出包含明文密钥。请校准系统时间。",
        )}
      </p>
      <Dialog open={exportOpen} onOpenChange={setExportOpen}>
        <DialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            exportTrigger.current?.focus();
          }}
        >
          <DialogTitle>{t("导出 MFA 账户")}</DialogTitle>
          <DialogDescription>
            {t(
              "导出 JSON 包含明文密钥。任何取得文件的人都可生成验证码，请妥善保管，不要发送到公开渠道。",
            )}
          </DialogDescription>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setExportOpen(false)}>
              {t("取消")}
            </Button>
            <Button onClick={download}>
              <Download size={15} />
              {t("确认导出")}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
