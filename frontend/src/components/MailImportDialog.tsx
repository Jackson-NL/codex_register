import { useId, useRef, useState } from "react";
import { Check, FileUp, Upload } from "lucide-react";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "./ui/dialog";
import {
  IMPORT_MAX_BYTES,
  parseMailImport,
  PROVIDER_NAMES,
  type MailProvider,
} from "../lib/mailConfig";
export function MailImportDialog({
  provider,
  count,
  busy,
  error,
  onClose,
  onImport,
}: {
  provider: MailProvider;
  count: number;
  busy: boolean;
  error: string;
  onClose: () => void;
  onImport: (text: string, append: boolean) => Promise<boolean>;
}) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState("append");
  const [confirmed, setConfirmed] = useState(false);
  const [fileError, setFileError] = useState("");
  const [reading, setReading] = useState(false);
  const revision = useRef(0);
  const preview = parseMailImport(provider, text);
  const replace = mode === "replace" && count > 0;
  const canImport =
    !busy &&
    !reading &&
    !fileError &&
    preview.values.length > 0 &&
    !preview.errors.length &&
    (!replace || confirmed);
  const inputId = useId();
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy && !reading) {
          revision.current++;
          onClose();
        }
      }}
    >
      <DialogContent
        className="mail-dialog"
        closeLabel="关闭导入"
        showClose={!busy && !reading}
        onEscapeKeyDown={(event) => {
          if (busy || reading) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          if (busy || reading) event.preventDefault();
        }}
      >
        <DialogHeader>
          <DialogTitle>导入 {PROVIDER_NAMES[provider]} 邮箱池</DialogTitle>
          <DialogDescription>
            目标已锁定为 {PROVIDER_NAMES[provider]}
            ，不会改变注册来源。导入立即保存，服务配置草稿不受影响。
          </DialogDescription>
        </DialogHeader>
        <div className="mail-import-body">
          <fieldset disabled={busy || reading}>
            <label className="mail-field">
              <span>导入方式</span>
              <select
                className="input"
                aria-label="导入方式"
                value={mode}
                onChange={(e) => {
                  setMode(e.target.value);
                  setConfirmed(false);
                }}
              >
                <option value="append">追加 · 保留现有 {count} 条资源</option>
                <option value="replace">
                  替换全部 · 移除现有 {count} 条资源
                </option>
              </select>
            </label>
            <div className="mail-import-toolbar">
              <span>仅接收 TXT · 最大 256 KB / 5000 行</span>
              <label className="mail-file-button">
                <FileUp size={14} />
                {reading ? "读取中…" : "从 TXT 导入"}
                <input
                  type="file"
                  accept=".txt,text/plain"
                  aria-label="导入 TXT 文件"
                  onChange={async (event) => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (!file) return;
                    const current = ++revision.current;
                    if (
                      !/\.txt$/i.test(file.name) ||
                      file.size > IMPORT_MAX_BYTES
                    ) {
                      setFileError("请选择不超过 256 KB 的 TXT 文件");
                      return;
                    }
                    setReading(true);
                    setFileError("");
                    try {
                      const value = await file.text();
                      if (current === revision.current) {
                        setText(value);
                        setConfirmed(false);
                      }
                    } catch {
                      if (current === revision.current)
                        setFileError("文件读取失败，请重新选择");
                    } finally {
                      if (current === revision.current) setReading(false);
                    }
                  }}
                />
              </label>
            </div>
            <label className="mail-field" htmlFor={inputId}>
              导入内容
            </label>
            <textarea
              id={inputId}
              className="input mail-import-text"
              value={text}
              spellCheck={false}
              placeholder={
                provider === "icloud"
                  ? "demo@icloud.com----https://mail.example.com/m?key=YourKey\n每行一个账号，收信 URL 大小写会原样保留"
                  : "first@example.com\nsecond@example.com\n支持换行、逗号、分号分隔"
              }
              onChange={(event) => {
                revision.current++;
                setText(event.target.value);
                setFileError("");
                setConfirmed(false);
              }}
            />
            <div
              className="mail-import-preview"
              data-testid="import-preview"
              aria-live="polite"
            >
              <span>
                <Check size={14} />
                有效 {preview.values.length}
              </span>
              <span>输入内重复 {preview.duplicates}</span>
              <span className={preview.errors.length ? "mail-error" : ""}>
                错误 {preview.errors.length}
              </span>
            </div>
            <p className="mail-muted">
              重复统计仅针对本次输入。追加时服务端会忽略已有邮箱，不更新它们的收信
              API。存在错误时请修正后再导入。
            </p>
            {(fileError || preview.errors.length > 0) && (
              <div role="alert" className="mail-notice mail-warning">
                {fileError || (
                  <div>
                    {preview.errors.slice(0, 5).map((error) => (
                      <p key={error}>{error}</p>
                    ))}
                    {preview.errors.length > 5 && (
                      <p>另有 {preview.errors.length - 5} 项错误</p>
                    )}
                  </div>
                )}
              </div>
            )}
            {replace && (
              <label className="mail-replace-confirm">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                />
                我确认替换现有 {count} 条资源，保留账号管理中的历史账号。
              </label>
            )}
          </fieldset>
          {error && (
            <div className="mail-notice mail-warning" role="alert">
              {error}
            </div>
          )}
        </div>
        <div className="mail-dialog-footer">
          <Button
            variant="outline"
            disabled={busy || reading}
            onClick={onClose}
          >
            取消
          </Button>
          <Button
            disabled={!canImport}
            aria-busy={busy}
            onClick={async () => {
              if (
                canImport &&
                (await onImport(preview.values.join("\n"), mode === "append"))
              )
                onClose();
            }}
          >
            <Upload />
            {busy ? "正在导入…" : "导入并保存"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
