import { describe, expect, it } from "vitest";
import {
  ACC_STATUS,
  FILTER_DEFS,
  NORMAL_STATUSES,
  accStatus,
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
    const labels = new Set(
      ["active", "cooling", "paused", "disabled", "unhealthy", ""].map((s) => accStatus({ status: s })),
    );
    expect([...labels].map((x) => x[1]).sort()).toEqual(["异常", "正常"]);
    expect(accStatus({ status: "active" })).toEqual(["success", "正常"]);
    expect(accStatus({ status: "paused" })).toEqual(["danger", "异常"]);
  });

  it("accStatus 同时接受账号对象与裸 status 字符串", () => {
    expect(accStatus({ status: "cooling" })).toEqual(accStatus("cooling"));
    expect(accStatus({ status: "disabled" })).toEqual(accStatus("disabled"));
  });

  it("状态筛选项只有全部/正常/异常，且与后端伪值同名", () => {
    expect(FILTER_DEFS.health.map((o) => o.value)).toEqual(["all", "normal", "abnormal"]);
    expect(FILTER_DEFS.health.map((o) => o.label)).toEqual(["全部状态", "正常", "异常"]);
    // 旧的双下划线内部值已废弃，不应再出现在选项里。
    expect(JSON.stringify(FILTER_DEFS.health)).not.toMatch(/__/);
  });
});

describe("历史保存视图的状态值迁移", () => {
  it("旧 __normal/__abnormal/active 归一化为两态值", () => {
    expect(normalizeViewFilters({ health: "__normal" }).health).toBe("normal");
    expect(normalizeViewFilters({ health: "__abnormal" }).health).toBe("abnormal");
    expect(normalizeViewFilters({ health: "active" }).health).toBe("normal");
  });

  it("未知/已正确的值原样保留，其它筛选键不受影响", () => {
    expect(normalizeViewFilters({ health: "abnormal", cred: "alive" })).toEqual({
      health: "abnormal",
      cred: "alive",
    });
    expect(normalizeViewFilters({ health: "all" }).health).toBe("all");
    expect(normalizeViewFilters(undefined)).toEqual({});
    expect(normalizeViewFilters({ cred: "missing" })).toEqual({ cred: "missing" });
  });

  it("异常态徽标颜色与文案对应账号页红色语义", () => {
    expect(ACC_STATUS.abnormal).toEqual(["danger", "异常"]);
    expect(ACC_STATUS.normal).toEqual(["success", "正常"]);
    // 不再按底层取值建表（旧写法会给 active/cooling 各配一个"正常"）。
    expect(Object.keys(ACC_STATUS).sort()).toEqual(["abnormal", "normal"]);
  });
});
