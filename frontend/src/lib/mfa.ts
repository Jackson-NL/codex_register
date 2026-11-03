// Local-only RFC 6238 utilities. Never send these records to an API or browser storage.
export interface MfaAccount {
  secret: string;
  label: string;
  issuer: string;
  note: string;
  algorithm: "SHA-1" | "SHA-256" | "SHA-512";
  digits: 6 | 8;
  period: number;
}
export const MAX_MFA_RECORDS = 200;
export const MAX_MFA_BYTES = 262144;
// Worst-case valid metadata (200 records including JSON-escaped controls) must round-trip.
export const MAX_MFA_JSON_BYTES = 2 * 1024 * 1024;
const INVALID = "密钥或参数无效";
const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function base32(value: string): Uint8Array<ArrayBuffer> {
  const compact = value.replace(/\s+/g, "").toUpperCase();
  if (!/^[A-Z2-7]{16,256}={0,6}$/.test(compact)) throw new Error(INVALID);
  const raw = compact.replace(/=+$/, "");
  const remainder = raw.length % 8;
  if (![0, 2, 4, 5, 7].includes(remainder)) throw new Error(INVALID);
  if (compact.includes("=") && compact.length !== Math.ceil(raw.length / 8) * 8)
    throw new Error(INVALID);
  const bytes: number[] = [];
  let buffer = 0,
    bits = 0;
  for (const char of raw) {
    buffer = (buffer << 5) | ALPHABET.indexOf(char);
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >>> bits) & 255);
    }
    buffer &= (1 << bits) - 1;
  }
  if (buffer !== 0) throw new Error(INVALID);
  return new Uint8Array(bytes);
}

export function parseSecret(value: string): MfaAccount {
  try {
    if (value.length > 4096) throw new Error(INVALID);
    let secret = value.trim(),
      label = "",
      issuer = "";
    let algorithm: MfaAccount["algorithm"] = "SHA-1";
    let digits: MfaAccount["digits"] = 6,
      period = 30;
    if (/^otpauth:/i.test(secret)) {
      const uri = new URL(secret);
      if (
        uri.protocol !== "otpauth:" ||
        uri.hostname !== "totp" ||
        uri.port ||
        uri.username ||
        uri.password ||
        uri.hash
      )
        throw new Error(INVALID);
      for (const key of ["secret", "issuer", "algorithm", "digits", "period"])
        if (uri.searchParams.getAll(key).length > 1) throw new Error(INVALID);
      secret = uri.searchParams.get("secret") ?? "";
      const decoded = decodeURIComponent(uri.pathname.slice(1));
      const colon = decoded.indexOf(":");
      label = colon < 0 ? decoded : decoded.slice(colon + 1);
      issuer =
        uri.searchParams.get("issuer") ??
        (colon < 0 ? "" : decoded.slice(0, colon));
      const hash = uri.searchParams.get("algorithm")?.toUpperCase() ?? "SHA1";
      if (!["SHA1", "SHA256", "SHA512"].includes(hash))
        throw new Error(INVALID);
      algorithm =
        hash === "SHA1" ? "SHA-1" : hash === "SHA256" ? "SHA-256" : "SHA-512";
      const rawDigits = uri.searchParams.get("digits") ?? "6";
      const rawPeriod = uri.searchParams.get("period") ?? "30";
      if (!["6", "8"].includes(rawDigits) || !/^\d{1,3}$/.test(rawPeriod))
        throw new Error(INVALID);
      digits = Number(rawDigits) as 6 | 8;
      period = Number(rawPeriod);
      if (period < 5 || period > 300) throw new Error(INVALID);
    }
    base32(secret);
    if (label.length > 254 || issuer.length > 100) throw new Error(INVALID);
    return {
      secret: secret.replace(/\s+/g, "").replace(/=+$/, "").toUpperCase(),
      label,
      issuer,
      note: "",
      algorithm,
      digits,
      period,
    };
  } catch {
    throw new Error(INVALID);
  }
}

export function parseEntries(text: string): {
  items: MfaAccount[];
  errors: string[];
} {
  if (
    text.length > MAX_MFA_BYTES ||
    new TextEncoder().encode(text).byteLength > MAX_MFA_BYTES
  )
    throw new Error("输入过大，最多 256 KB");
  const lines = text.split(/\r?\n/);
  if (lines.filter((line) => line.trim()).length > MAX_MFA_RECORDS)
    throw new Error("最多支持 200 条记录");
  const items: MfaAccount[] = [],
    errors: string[] = [];
  lines.forEach((line, index) => {
    if (!line.trim()) return;
    try {
      const fields = line
        .trim()
        .split(/----|\t/)
        .map((field) => field.trim());
      if (![1, 2, 3].includes(fields.length)) throw new Error(INVALID);
      const account = parseSecret(fields[fields.length - 1]);
      if (fields.length > 1) account.label = fields[0].slice(0, 254);
      if (!account.label) account.label = `密钥 ${index + 1}`;
      items.push(account);
    } catch {
      errors.push(`第 ${index + 1} 行：${INVALID}`);
    }
  });
  return { items, errors };
}

export async function generateTotp(
  account: MfaAccount,
  timeMs = Date.now(),
): Promise<string> {
  if (!globalThis.crypto?.subtle)
    throw new Error("浏览器不支持安全加密，请使用 HTTPS 或 localhost");
  const counter = new ArrayBuffer(8);
  new DataView(counter).setBigUint64(
    0,
    BigInt(Math.floor(timeMs / 1000 / account.period)),
  );
  const key = await crypto.subtle.importKey(
    "raw",
    base32(account.secret),
    { name: "HMAC", hash: account.algorithm },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, counter),
  );
  const offset = signature[signature.length - 1] & 15;
  const binary =
    ((signature[offset] & 127) << 24) |
    (signature[offset + 1] << 16) |
    (signature[offset + 2] << 8) |
    signature[offset + 3];
  return String(binary % 10 ** account.digits).padStart(account.digits, "0");
}

export function remainingSeconds(timeMs: number, period: number): number {
  return period - (Math.floor(timeMs / 1000) % period);
}

export function importAccounts(text: string): MfaAccount[] {
  try {
    if (
      text.length > MAX_MFA_JSON_BYTES ||
      new TextEncoder().encode(text).byteLength > MAX_MFA_JSON_BYTES
    )
      throw new Error(INVALID);
    const parsed: unknown = JSON.parse(text);
    const rows: unknown = Array.isArray(parsed)
      ? parsed
      : parsed && typeof parsed === "object" && "accounts" in parsed
        ? parsed.accounts
        : null;
    if (!Array.isArray(rows) || !rows.length || rows.length > MAX_MFA_RECORDS)
      throw new Error(INVALID);
    return rows.map((value: unknown) => {
      if (!value || typeof value !== "object") throw new Error(INVALID);
      const row = value as Record<string, unknown>;
      if (typeof row.secret !== "string") throw new Error(INVALID);
      const account = parseSecret(row.secret);
      for (const field of ["label", "issuer", "note"] as const) {
        if (row[field] !== undefined) {
          if (
            typeof row[field] !== "string" ||
            row[field].length >
              (field === "note" ? 500 : field === "issuer" ? 100 : 254)
          )
            throw new Error(INVALID);
          account[field] = row[field];
        }
      }
      if (row.algorithm !== undefined) {
        if (
          typeof row.algorithm !== "string" ||
          !["SHA-1", "SHA-256", "SHA-512"].includes(row.algorithm)
        )
          throw new Error(INVALID);
        account.algorithm = row.algorithm as MfaAccount["algorithm"];
      }
      if (row.digits !== undefined) {
        if (row.digits !== 6 && row.digits !== 8) throw new Error(INVALID);
        account.digits = row.digits;
      }
      if (row.period !== undefined) {
        if (
          typeof row.period !== "number" ||
          !Number.isInteger(row.period) ||
          row.period < 5 ||
          row.period > 300
        )
          throw new Error(INVALID);
        account.period = row.period;
      }
      return account;
    });
  } catch {
    throw new Error("JSON 格式或账号参数无效（最多 200 条 / 2 MB）");
  }
}

export function exportAccounts(accounts: MfaAccount[]): string {
  return JSON.stringify(
    {
      version: 1,
      accounts: accounts.map(
        ({ secret, label, issuer, note, algorithm, digits, period }) => ({
          secret,
          label,
          issuer,
          note,
          algorithm,
          digits,
          period,
        }),
      ),
    },
    null,
    2,
  );
}

export function parseEmails(value: string): string[] {
  if (value.length > 30000) throw new Error("邮箱输入过长");
  const emails = [
    ...new Set(
      value
        .split(/[\s,;，；]+/)
        .filter(Boolean)
        .map((email) => email.toLowerCase()),
    ),
  ];
  if (
    !emails.length ||
    emails.length > 100 ||
    emails.some(
      (email) =>
        email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email),
    )
  )
    throw new Error("请输入有效邮箱，最多 100 个");
  return emails;
}
