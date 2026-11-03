import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { api } from "../src/api/index.js";
import {
  parseReloginAccounts,
  buildReloginPayload,
  clearReloginSecrets,
  reloginJobActive,
  projectReloginJob,
  safeReloginError,
  readReloginJobId,
  saveReloginJobId,
  RELOGIN_JOB_STORAGE_KEY,
} from "../src/pages/accountReloginUtils.js";

test("single relogin normalizes identity and TOTP but preserves password bytes", () => {
  const result = parseReloginAccounts({ email: " Person@Example.COM ", password: " pass word ", totp_secret: "jbsw y3dp ehpk 3pxp" });
  assert.deepEqual(result, { accounts: [{ email: "person@example.com", password: " pass word ", totp_secret: "JBSWY3DPEHPK3PXP" }], errors: [] });
});

test("batch relogin supports optional TOTP and deduplicates identical rows", () => {
  const result = parseReloginAccounts({ mode: "batch", batch: "a@example.com----one\n\nB@example.com----two----JBSWY3DPEHPK3PXP\na@example.com----one" });
  assert.equal(result.errors.length, 0);
  assert.equal(result.accounts.length, 2);
  assert.deepEqual(result.accounts[0], { email: "a@example.com", password: "one", totp_secret: "" });
});

test("parse errors expose line numbers, never submitted credentials", () => {
  const secret = "private-password-value";
  for (const form of [
    { mode: "batch", batch: `invalid----${secret}` },
    { mode: "batch", batch: `a@example.com----${secret}\na@example.com----other` },
    { email: "a@example.com", password: secret, totp_secret: "123456" },
    { mode: "batch", batch: Array.from({ length: 21 }, (_, i) => `p${i}@example.com----${secret}`).join("\n") },
  ]) {
    const parsed = parseReloginAccounts(form);
    assert.ok(parsed.errors.length > 0);
    assert.ok(parsed.errors.every((error) => /第|最多|请输入/.test(error)));
    assert.doesNotMatch(JSON.stringify(parsed.errors), new RegExp(secret));
  }
});

test("relogin payload is RT-first, bounded, and contains no browser or Sub2API options", () => {
  const payload = buildReloginPayload({ email: "a@example.com", password: "pw", concurrency: "9", timeout_s: "999", proxy: " http://127.0.0.1:7890 " });
  assert.deepEqual(payload, { accounts: [{ email: "a@example.com", password: "pw", totp_secret: "" }], proxy: "http://127.0.0.1:7890", concurrency: 3, timeout_s: 180 });
  assert.throws(() => buildReloginPayload({ email: "bad", password: "do-not-echo" }), (error) => !error.message.includes("do-not-echo"));
});

test("submitted secrets are cleared without overwriting useful nonsensitive form fields", () => {
  const cleared = clearReloginSecrets({ email: "a@example.com", password: "pw", totp_secret: "JBSWY3DPEHPK3PXP", batch: "raw-secret", concurrency: 2 });
  assert.deepEqual(cleared, { email: "a@example.com", password: "", totp_secret: "", batch: "", concurrency: 2 });
});

test("job projection omits credentials and bounds logs; AT-only is successful", () => {
  const job = projectReloginJob({ id: "job", status: "succeeded", access_token: "private", items: [{ id: "item", email: "a@example.com", status: "succeeded", has_access_token: true, password: "pw", refresh_token: "private", credentials: {} }], logs: Array.from({ length: 500 }, (_, i) => ({ message: `stage ${i}` })) });
  assert.equal(job.items[0].has_access_token, true);
  assert.equal(job.items[0].has_refresh_token, false);
  assert.equal(job.logs.length, 150);
  assert.equal(reloginJobActive(job.status), false);
  assert.equal(reloginJobActive("pending"), true);
  assert.equal(reloginJobActive("running"), true);
  assert.doesNotMatch(JSON.stringify(job), /private|"password"|"credentials"/);
});

test("unexpected HTTP validation details never render the submitted secrets", () => {
  assert.equal(safeReloginError({ status: 422, message: 'input: private-password' }), "输入格式无效，请检查邮箱、密码和 2FA 密钥");
  assert.equal(safeReloginError({ message: "private-password" }), "请求失败，请检查服务状态后重试");
});

test("automatic save outcome remains visible and retry is available on failure", () => {
  const job = projectReloginJob({ items: [
    { status: "succeeded", has_access_token: true, saved_account_id: 42 },
    { status: "succeeded", stage: "saving", has_access_token: true, save_error: "保存凭据失败，未提交账号修改" },
  ] });
  assert.equal(job.items[0].saved_account_id, 42);
  assert.equal(job.items[0].save_error, "");
  assert.equal(job.items[1].saved_account_id, null);
  assert.equal(job.items[1].stage, "saving");
  assert.equal(job.items[1].save_error, "保存凭据失败，未提交账号修改");
  const page = readFileSync(new URL("../src/pages/AccountRelogin.jsx", import.meta.url), "utf8");
  assert.match(page, /成功后自动保存/);
  assert.match(page, /item\.save_error/);
  assert.match(page, /重试保存/);
  assert.doesNotMatch(page, /点击保存才写入/);
});

test("remount recovery stores only a validated job id", () => {
  const values = new Map();
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
  saveReloginJobId("d6a6ff46-8747-4a3d-9080-c98e6f7d8ba7", storage);
  assert.equal(readReloginJobId(storage), "d6a6ff46-8747-4a3d-9080-c98e6f7d8ba7");
  assert.deepEqual([...values.keys()], [RELOGIN_JOB_STORAGE_KEY]);
  saveReloginJobId("not-a-job-password", storage);
  assert.equal(readReloginJobId(storage), null);
});

test("account relogin API maps endpoints and uses explicit no-store POST for secrets", async () => {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return { ok: true, headers: { get: () => "application/json" }, json: async () => ({}) };
  };
  try {
    await api.accountRelogin.createJob({ accounts: [] });
    await api.accountRelogin.jobs();
    await api.accountRelogin.job("job/id");
    await api.accountRelogin.cancel("job");
    await api.accountRelogin.credentials("job", "item");
    await api.accountRelogin.save("job", "item");
  } finally {
    globalThis.fetch = original;
  }
  assert.deepEqual(calls.map(({ url }) => url), ["/api/account-relogin/jobs", "/api/account-relogin/jobs", "/api/account-relogin/jobs/job%2Fid", "/api/account-relogin/jobs/job/cancel", "/api/account-relogin/jobs/job/items/item/credentials", "/api/account-relogin/jobs/job/items/item/save"]);
  assert.equal(calls[4].options.method, "POST");
  assert.ok(calls.every(({ options }) => options.cache === "no-store"));
});

test("new independent sidebar route does not restore removed workbenches or persist secrets", () => {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const layout = readFileSync(new URL("../src/components/Layout.tsx", import.meta.url), "utf8");
  const navigation = readFileSync(new URL("../src/lib/navigation.ts", import.meta.url), "utf8");
  const page = readFileSync(new URL("../src/pages/AccountRelogin.jsx", import.meta.url), "utf8");
  assert.match(app, /path="\/account-relogin"/);
  assert.match(layout, /WorkspaceSidebar/);
  assert.match(navigation, /path:\s*"\/account-relogin",\s*label:\s*"账号重登"/);
  assert.doesNotMatch(app + layout + navigation, /sub2api-relogin|link-extraction/);
  assert.doesNotMatch(page, /localStorage|JSON\.stringify\(form\)|console\.log/);
  assert.match(page, /clearReloginSecrets/);
});

test("relogin reference layout scopes its two-column workspace and keeps logs collapsible", () => {
  const page = readFileSync(new URL("../src/pages/AccountRelogin.jsx", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/pages/AccountRelogin.css", import.meta.url), "utf8");
  assert.match(page, /import "\.\/AccountRelogin\.css"/);
  for (const marker of ["relogin-workspace", "relogin-grid", "relogin-input-card", "relogin-results-card", "relogin-submit-bar", "relogin-metrics"]) {
    assert.ok(page.includes(marker), `Missing layout region ${marker}`);
  }
  assert.match(page, /<details[^>]*className="relogin-log-panel"/);
  assert.match(page, /aria-label=\{t\("重登结果统计"\)\}/);
  assert.match(page, /已入库/);
  assert.match(css, /grid-template-columns:\s*minmax\(320px,\s*0\.36fr\)\s+minmax\(0,\s*0\.64fr\)/);
  assert.match(css, /@media\s*\(max-width:\s*1179px\)/);
  assert.match(css, /html\.theme-dark \.relogin-workspace/);
  assert.doesNotMatch(page, /文件夹|下载新 JSON|测活所选/);
});
