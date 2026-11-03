import { webcrypto } from "node:crypto";
import { beforeAll, describe, expect, it, vi } from "vitest";
import {
  generateTotp,
  parseEntries,
  parseSecret,
  importAccounts,
  exportAccounts,
  remainingSeconds,
} from "../src/lib/mfa";

beforeAll(() => {
  vi.stubGlobal("crypto", webcrypto);
});

describe("MFA local algorithms", () => {
  it.each([
    ["SHA-1", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", "94287082"],
    [
      "SHA-256",
      "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZA",
      "46119246",
    ],
    [
      "SHA-512",
      "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQGEZDGNA",
      "90693936",
    ],
  ])("matches RFC6238 %s at 59 seconds", async (algorithm, secret, code) => {
    expect(
      await generateTotp(
        parseSecret(
          `otpauth://totp/test?secret=${secret}&algorithm=${algorithm.replace("-", "")}&digits=8`,
        ),
        59000,
      ),
    ).toBe(code);
  });
  it.each([
    [1111111109000, "07081804"],
    [1111111111000, "14050471"],
    [1234567890000, "89005924"],
    [2000000000000, "69279037"],
    [20000000000000, "65353130"],
  ])("matches SHA1 RFC timestamp %s", async (time, code) => {
    expect(
      await generateTotp(
        { ...parseSecret("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"), digits: 8 },
        Number(time),
      ),
    ).toBe(code);
  });
  it("parses parameters and discards account passwords", () => {
    const entries = parseEntries(
      "test@example.invalid----do-not-retain----JBSWY3DPEHPK3PXP\nBAD0KEY\notpauth://totp/Acme%3Atest?secret=JBSWY3DPEHPK3PXP&period=60&digits=8",
    );
    expect(entries.items).toHaveLength(2);
    expect(entries.errors).toEqual(["第 2 行：密钥或参数无效"]);
    expect(JSON.stringify(entries)).not.toContain("do-not-retain");
    expect(entries.items[1]).toMatchObject({
      period: 60,
      digits: 8,
      issuer: "Acme",
      label: "test",
    });
  });
  it.each([
    "BAD0KEY",
    "A",
    "ABC",
    "JBSWY3DP=",
    "otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP",
    "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&period=0",
    "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&algorithm=MD5",
    "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&digits=7",
    "otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&secret=OTHER",
  ])("rejects invalid data without reflecting input", (value) => {
    expect(() => parseSecret(value)).toThrow("密钥或参数无效");
  });
  it("round-trips bounded JSON without unexpected fields", () => {
    const account = {
      ...parseSecret("JBSWY3DPEHPK3PXP"),
      label: "demo@example.invalid",
      issuer: "Demo",
      note: "Example",
    };
    expect(importAccounts(exportAccounts([account]))).toEqual([account]);
    expect(() =>
      importAccounts('{"accounts":[{"secret":"BAD0KEY"}]}'),
    ).toThrow();
    expect(() =>
      importAccounts(
        '{"accounts":[{"secret":"JBSWY3DPEHPK3PXP","algorithm":["SHA-1"]}]}',
      ),
    ).toThrow();
    expect(() => parseEntries("x".repeat(262145))).toThrow();
  });
  it("counts down with exact boundary handling", () => {
    expect(remainingSeconds(30000, 30)).toBe(30);
    expect(remainingSeconds(59999, 30)).toBe(1);
    expect(remainingSeconds(60000, 30)).toBe(30);
  });

  it("round-trips maximum-sized multilingual and escaped account metadata", () => {
    const rows = Array.from({ length: 200 }, (_, index) => ({
      ...parseSecret("JBSWY3DPEHPK3PXP"),
      label: `${index}`.padEnd(254, "中"),
      issuer: "文".repeat(100),
      note: "备".repeat(500),
    }));
    expect(
      new TextEncoder().encode(exportAccounts(rows)).byteLength,
    ).toBeGreaterThan(262144);
    expect(importAccounts(exportAccounts(rows))).toEqual(rows);
    const escaped = rows.map((row) => ({
      ...row,
      label: "\u0001".repeat(254),
      issuer: "\u0002".repeat(100),
      note: "\u0003".repeat(500),
    }));
    expect(importAccounts(exportAccounts(escaped))).toEqual(escaped);
  });
});
