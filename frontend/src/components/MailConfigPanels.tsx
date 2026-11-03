import {
  useId,
  useState,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { Eye, EyeOff, ShieldCheck } from "lucide-react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Switch } from "./ui/switch";
import {
  PROVIDER_NAMES,
  type CfDraft,
  type IcloudDraft,
  type MailDraft,
  type MailProvider,
  type MailSnapshot,
} from "../lib/mailConfig";

type NumericKey<T> = {
  [K in keyof T]: T[K] extends number ? K : never;
}[keyof T];

export function MailTabs<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; icon?: ReactNode }[];
  onChange: (value: T) => void;
}) {
  const id = useId();
  function navigate(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const next =
      event.key === "ArrowRight"
        ? (index + 1) % options.length
        : event.key === "ArrowLeft"
          ? (index + options.length - 1) % options.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? options.length - 1
              : -1;
    if (next < 0) return;
    event.preventDefault();
    onChange(options[next].value);
    document.getElementById(`${id}-${next}`)?.focus();
  }
  return (
    <div className="mail-tabs" role="tablist" aria-label={label}>
      {options.map((option, index) => (
        <button
          id={`${id}-${index}`}
          key={option.value}
          type="button"
          role="tab"
          aria-selected={value === option.value}
          tabIndex={value === option.value ? 0 : -1}
          onKeyDown={(event) => navigate(event, index)}
          onClick={() => onChange(option.value)}
        >
          {option.icon}
          {option.label}
        </button>
      ))}
    </div>
  );
}
export function MailField({
  label,
  hint,
  error,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: string;
  error?: string;
}) {
  const id = useId();
  return (
    <div className="mail-field">
      <label htmlFor={id}>{label}</label>
      <Input
        id={id}
        aria-invalid={!!error}
        aria-describedby={error || hint ? `${id}-help` : undefined}
        {...props}
      />
      {(error || hint) && (
        <p id={`${id}-help`} className={error ? "mail-error" : "mail-muted"}>
          {error || hint}
        </p>
      )}
    </div>
  );
}
function SecretField({
  label,
  value,
  configured,
  error,
  onChange,
}: {
  label: string;
  value: string;
  configured: boolean;
  error?: string;
  onChange: (value: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  const id = useId();
  return (
    <div className="mail-field">
      <label htmlFor={id}>
        {label}
        <span className="mail-pill">{configured ? "已配置" : "未配置"}</span>
      </label>
      <div className="mail-secret">
        <Input
          id={id}
          type={visible ? "text" : "password"}
          autoComplete="new-password"
          spellCheck={false}
          value={value}
          placeholder={configured ? "输入新凭证以替换，留空不修改" : "输入凭证"}
          aria-invalid={!!error}
          aria-describedby={`${id}-help`}
          onChange={(event) => onChange(event.target.value)}
        />
        <Button
          variant="ghost"
          size="icon"
          aria-label={`${visible ? "隐藏" : "显示"}${label}`}
          onClick={() => setVisible(!visible)}
        >
          {visible ? <EyeOff /> : <Eye />}
        </Button>
      </div>
      <p id={`${id}-help`} className={error ? "mail-error" : "mail-muted"}>
        {error || "已保存凭证不回显；清空输入不会删除原凭证。"}
      </p>
    </div>
  );
}
function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const id = useId();
  return (
    <div className="mail-toggle">
      <div>
        <label htmlFor={id}>{label}</label>
        <p>{hint}</p>
      </div>
      <Switch
        id={id}
        aria-label={label}
        checked={checked}
        onCheckedChange={onChange}
      />
    </div>
  );
}
function Advanced({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="mail-advanced">
      <summary>
        {title}
        <span>展开设置</span>
      </summary>
      <div>{children}</div>
    </details>
  );
}
export function MailProviderSettings({
  provider,
  draft,
  snapshot,
  errors,
  patchCf,
  patchIcloud,
}: {
  provider: MailProvider;
  draft: MailDraft;
  snapshot: MailSnapshot;
  errors: Record<string, string>;
  patchCf: <K extends keyof CfDraft>(key: K, value: CfDraft[K]) => void;
  patchIcloud: <K extends keyof IcloudDraft>(
    key: K,
    value: IcloudDraft[K],
  ) => void;
}) {
  const cf = draft.cf_temp_email,
    ic = draft.icloud;
  const numberField = (
    key: string,
    label: string,
    value: number,
    onChange: (value: number) => void,
    min = 1,
  ) => {
    return (
      <MailField
        key={key}
        label={label}
        type="number"
        min={min}
        step="1"
        value={Number.isNaN(value) ? "" : value}
        error={errors[key]}
        onChange={(event) =>
          onChange(event.target.value === "" ? NaN : Number(event.target.value))
        }
      />
    );
  };
  const cfNumber = (key: NumericKey<CfDraft>, label: string) =>
    numberField(key, label, cf[key], (value) => patchCf(key, value));
  const icNumber = (key: NumericKey<IcloudDraft>, label: string, min = 1) =>
    numberField(key, label, ic[key], (value) => patchIcloud(key, value), min);
  return (
    <section
      className="mail-settings"
      aria-label={`${PROVIDER_NAMES[provider]} 服务配置`}
    >
      <div className="mail-section-title">
        <div>
          <h2>服务接入</h2>
          <p>编辑参数不会自动切换注册来源，保存后才会生效。</p>
        </div>
        <ShieldCheck size={20} />
      </div>
      {provider === "cf_temp_email" ? (
        <>
          <Toggle
            label="启用 Cloudflare 服务"
            hint="允许注册流程使用此邮箱服务。"
            checked={cf.enabled}
            onChange={(value) => patchCf("enabled", value)}
          />
          <div className="mail-field-grid">
            <MailField
              label="API 地址"
              value={cf.base_url}
              placeholder="https://mail.example.com"
              error={errors.base_url}
              onChange={(e) => patchCf("base_url", e.target.value)}
            />
            <MailField
              label="邮箱域名"
              value={cf.domain}
              error={errors.domain}
              placeholder="example.com"
              onChange={(e) => patchCf("domain", e.target.value)}
            />
          </div>
          <label className="mail-field">
            <span>地址来源</span>
            <select
              className="input"
              aria-label="地址来源"
              value={cf.address_mode}
              onChange={(e) => patchCf("address_mode", e.target.value)}
            >
              <option value="generated">自动生成地址</option>
              <option value="custom_pool">自定义邮箱池（统一收件）</option>
            </select>
          </label>
          {cf.address_mode === "custom_pool" && (
            <div className="mail-inset">
              <p className="mail-muted">
                邮箱池中的地址需转发到同一个收件箱；请在「邮箱池」页管理地址。
              </p>
              <div className="mail-field-grid">
                <MailField
                  label="固定收件邮箱"
                  value={cf.inbox_address}
                  error={errors.inbox_address}
                  onChange={(e) => patchCf("inbox_address", e.target.value)}
                />
                <SecretField
                  label="固定收件 JWT"
                  value={cf.inbox_jwt}
                  configured={snapshot.cf_temp_email.has_inbox_jwt}
                  error={errors.inbox_jwt}
                  onChange={(value) => patchCf("inbox_jwt", value)}
                />
              </div>
              {errors.pool && <p className="mail-error">{errors.pool}</p>}
            </div>
          )}
          <Advanced title="站点访问凭证">
            <SecretField
              label="站点访问密码"
              value={cf.site_password}
              configured={snapshot.cf_temp_email.has_site_password}
              onChange={(value) => patchCf("site_password", value)}
            />
          </Advanced>
          <Advanced title="地址生成规则">
            <div className="mail-field-grid">
              <MailField
                label="地址前缀"
                value={cf.name_prefix}
                onChange={(e) => patchCf("name_prefix", e.target.value)}
              />
              {cfNumber("random_length", "随机后缀长度")}
            </div>
            <p className="mail-muted">
              仅自动生成模式使用；不会影响已导入的邮箱地址。
            </p>
          </Advanced>
          <Advanced title="收码与重试策略">
            <div className="mail-field-grid">
              {cfNumber("poll_interval", "轮询间隔（秒）")}
              {cfNumber("poll_timeout", "验证码超时（秒）")}
              {cfNumber("max_retries", "最大重试次数")}
              {cfNumber("rate_limit_backoff", "限流退避（秒）")}
            </div>
          </Advanced>
        </>
      ) : (
        <>
          <Toggle
            label="启用 iCloud 服务"
            hint="从预置邮箱池取号，使用每条账号的收信 API 获取验证码。"
            checked={ic.enabled}
            onChange={(value) => patchIcloud("enabled", value)}
          />
          <div className="mail-inset">
            <strong>预置邮箱池 · manual_pool</strong>
            <p className="mail-muted">
              每条资源由邮箱和收信 API
              组成。前往「邮箱池」批量导入，服务端只返回脱敏样例，不回传收信凭证。
            </p>
          </div>
          <div className="mail-field-grid">
            {icNumber("poll_interval", "轮询间隔（秒）")}
            {icNumber("poll_timeout", "验证码超时（秒）")}
          </div>
          <Advanced title="别名派生设置">
            <Toggle
              label="启用 + 别名派生"
              hint="需收信服务支持将 root+tag 归一到基础邮箱；启用不代表上游一定支持。"
              checked={ic.split_enabled}
              onChange={(value) => patchIcloud("split_enabled", value)}
            />
            {ic.split_enabled && (
              <div className="mail-field-grid">
                <MailField
                  label="别名 tag 前缀"
                  value={ic.split_prefix}
                  onChange={(e) => patchIcloud("split_prefix", e.target.value)}
                />
                {icNumber("split_tag_length", "别名 tag 长度")}
                {icNumber(
                  "split_max_per_base",
                  "每基础邮箱上限（0 = 不限）",
                  0,
                )}
              </div>
            )}
          </Advanced>
        </>
      )}
    </section>
  );
}
