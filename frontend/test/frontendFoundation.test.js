import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = (file) => readFileSync(new URL(`../${file}`, import.meta.url), "utf8");

test("new frontend foundation has strict TypeScript, shadcn tokens and route splitting", () => {
  const tsconfig = JSON.parse(source("tsconfig.json"));
  assert.equal(tsconfig.compilerOptions.strict, true);
  assert.equal(tsconfig.compilerOptions.allowJs, true);
  assert.equal(tsconfig.compilerOptions.checkJs, false);
  assert.equal(tsconfig.compilerOptions.noEmit, true);
  assert.match(source("index.html"), /\/src\/main.tsx/);
  assert.match(source("src/App.tsx"), /lazy\(/);
  assert.match(source("tailwind.config.js"), /tsx/);
  assert.match(source("components.json"), /new-york/);
  assert.match(source("src/styles/design-system.css"), /--background:/);
  assert.match(source("src/styles/design-system.css"), /prefers-reduced-motion/);
});

test("legacy UI uses the new primitives without changing business-page imports", () => {
  const ui = source("src/components/ui/index.jsx");
  assert.match(ui, /from "\.\/button"/);
  assert.match(ui, /from "\.\/card"/);
  assert.match(ui, /from "\.\/dialog"/);
  assert.match(ui, /from "\.\/switch"/);
  const shell = source("src/components/Layout.tsx");
  assert.match(shell, /SheetContent/);
  assert.match(shell, /DropdownMenu/);
  assert.match(shell, /DialogContent/);
  assert.match(shell, /id="main-content"/);
  assert.doesNotMatch(shell, /ops@example.com/);
});
