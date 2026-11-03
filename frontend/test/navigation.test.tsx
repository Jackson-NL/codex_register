import { describe, expect, it } from "vitest";
import {
  MODULES,
  MODULE_GROUPS,
  searchModules,
  getModule,
  readFavorites,
  writeFavorites,
} from "../src/lib/navigation";

describe("workspace module directory", () => {
  it("maps every existing feature exactly once with a useful purpose and workflow", () => {
    expect(new Set(MODULES.map((module) => module.path)).size).toBe(
      MODULES.length,
    );
    for (const path of [
      "/",
      "/workspace",
      "/accounts",
      "/register",
      "/account-relogin",
      "/codex-oauth",
      "/mfa-codes",
      "/proxies",
      "/mail-config",
      "/settings",
      "/admin",
    ]) {
      const module = getModule(path);
      expect(module?.path).toBe(path);
      expect(module?.description.length).toBeGreaterThan(10);
      expect(module?.capabilities.length).toBeGreaterThan(1);
    }
    expect(
      MODULES.every((module) =>
        MODULE_GROUPS.some((group) => group.id === module.group),
      ),
    ).toBe(true);
  });
  it("searches real modules by labels, keywords and descriptions, never mock accounts", () => {
    expect(
      searchModules("2fa", false).some(
        (module) => module.path === "/mfa-codes",
      ),
    ).toBe(true);
    expect(
      searchModules("refresh token", false).some(
        (module) => module.path === "/account-relogin",
      ),
    ).toBe(true);
    expect(
      searchModules("邮箱", false).some(
        (module) => module.path === "/mail-config",
      ),
    ).toBe(true);
    expect(searchModules("acc_001", false)).toEqual([]);
    expect(
      searchModules("", false).some((module) => module.path === "/admin"),
    ).toBe(false);
    expect(
      searchModules("", true).some((module) => module.path === "/admin"),
    ).toBe(true);
  });
  it("stores only valid module IDs and tolerates unavailable or malformed storage", () => {
    const storage = {
      getItem: () =>
        '["accounts","accounts","private@example.invalid","mfa-codes","unknown"]',
      setItem: () => {},
    };
    expect(readFavorites(storage)).toEqual(["accounts", "mfa-codes"]);
    let saved = "";
    writeFavorites(["accounts", "bad-secret", "accounts"], {
      setItem: (_key, value) => {
        saved = value;
      },
    });
    expect(JSON.parse(saved)).toEqual(["accounts"]);
    expect(readFavorites({ getItem: () => "[broken" })).toEqual([
      "accounts",
      "account-relogin",
      "mfa-codes",
    ]);
    expect(() =>
      writeFavorites(["accounts"], {
        setItem: () => {
          throw new Error("blocked");
        },
      }),
    ).not.toThrow();
  });
  it("searches translated labels and group purposes in the selected language", () => {
    const en = (value: string) =>
      value === "2FA / MFA 验证码" ? "2FA / MFA codes" : value;
    expect(
      searchModules("mfa codes", false, en).some(
        (module) => module.id === "mfa-codes",
      ),
    ).toBe(true);
    expect(
      searchModules("account relogin", false).some(
        (module) => module.id === "account-relogin",
      ),
    ).toBe(true);
  });
});
