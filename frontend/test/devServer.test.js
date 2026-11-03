import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import config from "../vite.config.js";

test("frontend defaults consistently use 5180 and never silently pick another port", () => {
  assert.equal(config.server.port, 5180);
  assert.equal(config.server.strictPort, true);
  assert.equal(config.preview.port, 5180);
  assert.equal(config.preview.strictPort, true);
  assert.equal(config.server.proxy["/api"].target, process.env.VITE_API_TARGET || "http://127.0.0.1:8000");
  const startup = readFileSync(new URL("../../start.ps1", import.meta.url), "utf8");
  assert.match(startup, /\[int\]\$FrontendPort\s*=\s*5180/);
  assert.match(startup, /--strictPort/);
  const readme = readFileSync(new URL("../../README.md", import.meta.url), "utf8");
  assert.match(readme, /访问 `http:\/\/127\.0\.0\.1:5180\//);
});
