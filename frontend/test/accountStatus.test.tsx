import { describe, expect, it } from "vitest";
import {
  ACC_STATUS,
  CRED_STATUS,
  FILTER_DEFS,
  NORMAL_STATUSES,
  accStatus,
  credState,
  credentialFilter,
  isCredUsable,
  isHealthy,
  isNormalStatus,
  normalizeViewFilters,
} from "../src/pages/accountsShared";

// 账号状态只分两态：正常 / 异常。口径须与 backend/app/services/account_status.py
// 一致（active + cooling = 正常，其余一律异常）。
describe("账号状态两态口径", () => {
  it("正常桶只含 active 与 cooling", () => {
    expect(NORMAL_STATUSES).toEqual(["active", "cooling"]);
    expect(isNormalStatus("active")).toBe(true);
    expect(isNormalStatus("cooling")).toBe(true);
  });

  it("其余一切取值都是异常，含未知值与空值（fail-closed）", () => {
    for (const status of ["paused", "disabled", "unhealthy", "", "suspended", "brand_new"]) {
      expect(isNormalStatus(status)).toBe(false);
    }
    expect(isNormalStatus(undefined)).toBe(false);
    expect(isNormalStatus(null)).toBe(false);
  });

  it("判定对大小写与空格不敏感", () => {
    expect(isNormalStatus("Active")).toBe(true);
    expect(isNormalStatus("  COOLING  ")).toBe(true);
    expect(isNormalStatus(" Paused ")).toBe(false);
  });

  it("徽标只输出正常/异常两种文案", () => {
    // 只给 status、不带凭据信息时，active/cooling 走兜底（无 AT → 异常）；
    // healthy 的对象才显示正常。两种文案都覆盖到。
    const labels = new Set([
      ...["active", "cooling", "paused", "disabled", "unhealthy", ""].map(
        (s) => accStatus({ status: s })[1],
      ),
      accStatus({ status: "active", has_access_token: true })[1],
    ]);
    expect([...labels].sort()).toEqual(["异常", "正常"]);
    // status 正常但没凭据 → 异常（方案 A：正常 = 可用）
    expect(accStatus({ status: "active" })).toEqual(["danger", "异常"]);
    expect(accStatus({ status: "paused" })).toEqual(["danger", "异常"]);
  });

  it("后端 health 字段优先，直接决定徽标", () => {
    // health=normal 即使 status 看起来异常也照 normal 显示（后端才是权威）
    expect(accStatus({ status: "disabled", health: "normal" })).toEqual(["success", "正常"]);
    expect(accStatus({ status: "active", health: "abnormal" })).toEqual(["danger", "异常"]);
  });

  it("无 health 时用「status 正常 且 凭据可用」兜底，不再只看 status", () => {
    const future = new Date(Date.now() + 86400e3).toISOString();
    const past = new Date(Date.now() - 86400e3).toISOString();
    expect(accStatus({ status: "active", has_access_token: true, token_expires_at: future }))
      .toEqual(["success", "正常"]);
    // 这正是用户报的问题：status=active 但凭据已过期，必须显示异常
    expect(accStatus({ status: "active", has_access_token: true, token_expires_at: past }))
      .toEqual(["danger", "异常"]);
    expect(accStatus({ status: "active", has_access_token: false }))
      .toEqual(["danger", "异常"]);
    expect(isHealthy({ status: "active", has_access_token: true, token_expires_at: past })).toBe(false);
  });

  it("accStatus 接受裸 status 字符串时只按 status 判（调用方只给了字段）", () => {
    expect(accStatus("cooling")).toEqual(["success", "正常"]);
    expect(accStatus("disabled")).toEqual(["danger", "异常"]);
  });

  it("状态筛选项只有全部/正常/异常，且与后端伪值同名", () => {
    expect(FILTER_DEFS.health.map((o) => o.value)).toEqual(["all", "normal", "abnormal"]);
    expect(FILTER_DEFS.health.map((o) => o.label)).toEqual(["全部状态", "正常", "异常"]);
    // 旧的双下划线内部值已废弃，不应再出现在选项里。
    expect(JSON.stringify(FILTER_DEFS.health)).not.toMatch(/__/);
  });
});

describe("凭据状态两态口径", () => {
  const future = new Date(Date.now() + 86400e3).toISOString();
  const past = new Date(Date.now() - 86400e3).toISOString();

  it("有 AT 且未过期 = 正常", () => {
    expect(isCredUsable({ has_access_token: true, token_expires_at: future })).toBe(true);
    expect(credState({ has_access_token: true, token_expires_at: future })).toEqual(["success", "正常"]);
  });

  it("无 AT / 已过期 / 过期时间非法 = 异常（fail-closed）", () => {
    expect(isCredUsable({ has_access_token: false, token_expires_at: future })).toBe(false);
    expect(isCredUsable({ has_access_token: true, token_expires_at: past })).toBe(false);
    expect(isCredUsable({ has_access_token: true, token_expires_at: "not-a-date" })).toBe(false);
    expect(credState({ has_access_token: false })).toEqual(["danger", "异常"]);
    expect(credState({ has_access_token: true, token_expires_at: past })).toEqual(["danger", "异常"]);
  });

  it("未记录过期时间时按可用处理（后端无 exp 时如此）", () => {
    expect(isCredUsable({ has_access_token: true })).toBe(true);
  });

  it("徽标只输出正常/异常两种文案，不再有 无凭证/已过期/凭据有效", () => {
    const labels = new Set(
      [
        { has_access_token: true, token_expires_at: future },
        { has_access_token: true, token_expires_at: past },
        { has_access_token: false },
      ].map((a) => credState(a)[1]),
    );
    expect([...labels].sort()).toEqual(["异常", "正常"]);
    expect(Object.keys(CRED_STATUS).sort()).toEqual(["abnormal", "normal"]);
  });

  it("筛选值与判定同源，且与账号状态同名", () => {
    expect(credentialFilter({ has_access_token: true, token_expires_at: future })).toBe("normal");
    expect(credentialFilter({ has_access_token: false })).toBe("abnormal");
    expect(FILTER_DEFS.cred.map((o) => o.value)).toEqual(["all", "normal", "abnormal"]);
    expect(FILTER_DEFS.cred.map((o) => o.label)).toEqual(["全部凭据", "正常", "异常"]);
  });

  it("credState 也接受布尔值，调用方不必自己包对象", () => {
    expect(credState(true)).toEqual(["success", "正常"]);
    expect(credState(false)).toEqual(["danger", "异常"]);
  });
});

describe("历史保存视图的状态值迁移", () => {
  it("旧 __normal/__abnormal/active 归一化为两态值", () => {
    expect(normalizeViewFilters({ health: "__normal" }).health).toBe("normal");
    expect(normalizeViewFilters({ health: "__abnormal" }).health).toBe("abnormal");
    expect(normalizeViewFilters({ health: "active" }).health).toBe("normal");
  });

  it("未知/已正确的值原样保留，其它筛选键不受影响", () => {
    expect(normalizeViewFilters({ health: "abnormal" })).toEqual({ health: "abnormal" });
    expect(normalizeViewFilters({ health: "all" }).health).toBe("all");
    expect(normalizeViewFilters(undefined)).toEqual({});
  });

  it("凭据旧三态值迁移为两态：alive→normal，problem/missing→abnormal", () => {
    expect(normalizeViewFilters({ cred: "alive" }).cred).toBe("normal");
    expect(normalizeViewFilters({ cred: "problem" }).cred).toBe("abnormal");
    expect(normalizeViewFilters({ cred: "missing" }).cred).toBe("abnormal");
    // 已是新值的原样保留
    expect(normalizeViewFilters({ cred: "normal" }).cred).toBe("normal");
    expect(normalizeViewFilters({ cred: "abnormal" }).cred).toBe("abnormal");
  });

  it("异常态徽标颜色与文案对应账号页红色语义", () => {
    expect(ACC_STATUS.abnormal).toEqual(["danger", "异常"]);
    expect(ACC_STATUS.normal).toEqual(["success", "正常"]);
    // 不再按底层取值建表（旧写法会给 active/cooling 各配一个"正常"）。
    expect(Object.keys(ACC_STATUS).sort()).toEqual(["abnormal", "normal"]);
  });
});
