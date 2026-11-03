import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { api } from "../src/api/index.js";

test("removed workbenches have no navigation, routes or API clients", () => {
  for (const file of ["../src/App.tsx", "../src/components/Layout.tsx"]) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    assert.doesNotMatch(source, /sub2api-relogin|link-extraction|Sub2APIRelogin|LinkExtraction/);
  }
  assert.equal(Object.hasOwn(api, "sub2apiRelogin"), false);
  assert.equal(Object.hasOwn(api, "linkExtraction"), false);
  assert.equal(typeof api.sub2api.upload, "function");
  assert.equal(typeof api.accounts.startOAuthJob, "function");
});
