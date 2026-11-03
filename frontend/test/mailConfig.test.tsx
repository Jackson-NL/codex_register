import { expect, it } from "vitest";
import {
  buildMailPatch,
  createMailDraft,
  IMPORT_MAX_BYTES,
  mailTime,
  parseMailImport,
  validateMailProvider,
  type MailSnapshot,
} from "../src/lib/mailConfig";

function snapshot(): MailSnapshot {
  return {
    provider: "cf_temp_email",
    cf_temp_email: {
      enabled: true,
      base_url: "https://mail.example.invalid",
      domain: "example.invalid",
      address_mode: "generated",
      name_prefix: "reg",
      random_length: 10,
      poll_interval: 4,
      poll_timeout: 180,
      max_retries: 3,
      rate_limit_backoff: 10,
      inbox_address: "inbox@example.invalid",
      has_inbox_jwt: true,
      has_site_password: true,
      custom_pool_count: 2,
      custom_pool_items: [],
      custom_pool_status_counts: {},
      custom_pool_summary: {},
    },
    icloud: {
      enabled: true,
      mode: "manual_pool",
      poll_interval: 4,
      poll_timeout: 180,
      split_enabled: false,
      split_prefix: "",
      split_tag_length: 6,
      split_max_per_base: 0,
      accounts_count: 3,
      accounts_sample: [],
      remaining_rounds: 2,
      exhausted_roots: 1,
    },
  };
}
it("normalizes only the editable whitelist and does not recreate secret masks", () => {
  const data = snapshot();
  const draft = createMailDraft(data);
  expect(draft.cf_temp_email).not.toHaveProperty("custom_pool_items");
  expect(draft.icloud).not.toHaveProperty("accounts_count");
  expect(draft.cf_temp_email.inbox_jwt).toBe("");
  expect(draft.cf_temp_email.site_password).toBe("");
  expect(buildMailPatch(draft, createMailDraft(data))).toEqual({});
});
it("omits blank and mask secrets while retaining new literal credentials", () => {
  const saved = createMailDraft(snapshot()),
    draft = createMailDraft(snapshot());
  draft.cf_temp_email.site_password = "••••••••";
  draft.cf_temp_email.inbox_jwt = "   ";
  expect(buildMailPatch(draft, saved)).toEqual({});
  draft.cf_temp_email.site_password = "CaseSensitiveFixture";
  draft.icloud.split_max_per_base = 0;
  expect(buildMailPatch(draft, saved)).toEqual({
    cf_temp_email: { site_password: "CaseSensitiveFixture" },
  });
});
it("keeps iCloud zero alias limit valid without validating another provider", () => {
  const data = snapshot(),
    draft = createMailDraft(data);
  draft.cf_temp_email.base_url = "invalid";
  expect(validateMailProvider("icloud", draft, data)).toEqual({});
  expect(validateMailProvider("cf_temp_email", draft, data)).toHaveProperty(
    "base_url",
  );
  draft.icloud.split_max_per_base = -1;
  expect(validateMailProvider("icloud", draft, data)).toHaveProperty(
    "split_max_per_base",
  );
});
it("requires fixed inbox, JWT and a saved pool for custom-pool mode", () => {
  const data = snapshot(),
    draft = createMailDraft(data);
  data.cf_temp_email.has_inbox_jwt = false;
  data.cf_temp_email.custom_pool_count = 0;
  draft.cf_temp_email.address_mode = "custom_pool";
  draft.cf_temp_email.inbox_address = "bad";
  expect(
    Object.keys(validateMailProvider("cf_temp_email", draft, data)),
  ).toEqual(["pool", "inbox_address", "inbox_jwt"]);
});
it("does not turn an empty numeric field or fraction into a valid integer", () => {
  const data = snapshot(),
    draft = createMailDraft(data);
  draft.icloud.poll_interval = NaN;
  draft.icloud.poll_timeout = 0.5;
  expect(validateMailProvider("icloud", draft, data)).toEqual({
    poll_interval: "请输入正整数",
    poll_timeout: "请输入正整数",
  });
});
it("deduplicates CF addresses across mixed separators and comments", () => {
  expect(
    parseMailImport(
      "cf_temp_email",
      " #comment\nA@example.invalid;a@example.invalid, b@example.invalid\ninvalid",
    ),
  ).toEqual({
    values: ["a@example.invalid", "b@example.invalid"],
    duplicates: 1,
    errors: ["第 5 项格式错误，需要有效邮箱地址"],
  });
});
it("canonicalizes email but preserves case-sensitive URLs and repeated separators", () => {
  const url =
    "https://mail.example.invalid/AbC----tail?key=CaseSensitive&E=X%2BY";
  expect(
    parseMailImport(
      "icloud",
      ` DEMO@icloud.com ---- ${url}\n# comment\ndemo@icloud.com----https://other.example.invalid/?key=Different`,
    ),
  ).toEqual({
    values: [`demo@icloud.com----${url}`],
    duplicates: 1,
    errors: [],
  });
});
it.each([
  "not-a-url",
  "javascript:alert(1)",
  "https://",
  "https://mail.example.invalid/has space",
])("rejects invalid iCloud URL %s without reflecting it", (url) => {
  const parsed = parseMailImport("icloud", `demo@icloud.com----${url}`);
  expect(parsed.values).toEqual([]);
  expect(parsed.errors).toEqual([
    "第 1 项格式错误，需要 邮箱----http(s)收信API",
  ]);
});
it("treats iCloud commas/semicolons as URL data, not entry separators", () => {
  const value = "demo@icloud.com----https://mail.example.invalid/?key=a,b;c";
  expect(parseMailImport("icloud", value).values).toEqual([value]);
});
it("enforces byte and line limits without retaining values in errors", () => {
  expect(
    parseMailImport("icloud", "密".repeat(IMPORT_MAX_BYTES / 3 + 1)).errors,
  ).toEqual(["导入内容不能超过 256 KB"]);
  expect(
    parseMailImport("cf_temp_email", "demo@example.invalid\n".repeat(5001))
      .errors,
  ).toEqual(["一次最多导入 5000 行"]);
});
it("formats naive database timestamps as UTC and handles invalid timestamps", () => {
  expect(mailTime("invalid")).toBe("暂无记录");
  expect(mailTime(null)).toBe("暂无记录");
  expect(mailTime("2026-11-02T00:00:00")).toBe(
    mailTime("2026-11-02T00:00:00Z"),
  );
});
