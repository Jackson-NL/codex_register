import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import WorkspaceSidebar from "../src/components/WorkspaceSidebar";
import Workspace from "../src/pages/Workspace";

const { context } = vi.hoisted(() => ({
  context: { t: (value: string) => value, adminEnabled: false },
}));
vi.mock("../src/context/AppContext", () => ({ useApp: () => context }));
beforeEach(() => {
  localStorage.clear();
  context.adminEnabled = false;
});

it("filters modules, clears search, toggles favorites and collapses groups without hiding the current route", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/mfa-codes"]}>
      <WorkspaceSidebar />
    </MemoryRouter>,
  );
  const nav = screen.getByRole("navigation", { name: "主导航" });
  expect(
    within(nav)
      .getByRole("link", { name: "2FA / MFA 验证码" })
      .getAttribute("aria-current"),
  ).toBe("page");
  expect(screen.queryByRole("link", { name: "管理员入口" })).toBeNull();
  await user.type(
    screen.getByRole("searchbox", { name: "搜索侧栏模块" }),
    "邮箱",
  );
  expect(within(nav).queryByRole("link", { name: "代理池" })).toBeNull();
  expect(within(nav).getByRole("link", { name: "邮箱配置" })).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "清除模块搜索" }));
  await user.click(within(nav).getByRole("button", { name: "收藏 邮箱配置" }));
  expect(
    within(screen.getByRole("navigation", { name: "常用模块" })).getByRole(
      "link",
      { name: "邮箱配置" },
    ),
  ).toBeTruthy();
  expect(localStorage.getItem("accountops-module-favorites-v1")).toContain(
    "mail-config",
  );
  const group = screen.getByRole("button", { name: "基础资源" });
  await user.click(group);
  expect(group.getAttribute("aria-expanded")).toBe("false");
  expect(within(nav).queryByRole("link", { name: "邮箱配置" })).toBeNull();
  await user.type(
    screen.getByRole("searchbox", { name: "搜索侧栏模块" }),
    "邮箱",
  );
  expect(within(nav).getByRole("link", { name: "邮箱配置" })).toBeTruthy();
});

it("directory exposes real capabilities and filters modules without auto-running operations", async () => {
  const user = userEvent.setup();
  render(
    <MemoryRouter>
      <Routes>
        <Route path="/" element={<Workspace />} />
        <Route path="/mfa-codes" element={<h1>OTP destination</h1>} />
      </Routes>
    </MemoryRouter>,
  );
  expect(screen.getByRole("heading", { name: "工作导航" })).toBeTruthy();
  expect(screen.getByText("RT 优先，AT 兜底")).toBeTruthy();
  await user.type(
    screen.getByRole("searchbox", { name: "搜索功能模块" }),
    "totp",
  );
  const list = screen.getByRole("region", { name: "功能模块目录" });
  expect(
    within(list).getByRole("heading", { name: "2FA / MFA 验证码" }),
  ).toBeTruthy();
  expect(within(list).queryByRole("heading", { name: "代理池" })).toBeNull();
  await user.click(
    within(list).getByRole("link", { name: "打开 2FA / MFA 验证码" }),
  );
  expect(screen.getByRole("heading", { name: "OTP destination" })).toBeTruthy();
});

it("keeps the active group open and filters admin modules by instance configuration", async () => {
  const user = userEvent.setup();
  context.adminEnabled = true;
  const { rerender } = render(
    <MemoryRouter initialEntries={["/admin"]}>
      <WorkspaceSidebar />
    </MemoryRouter>,
  );
  const nav = screen.getByRole("navigation", { name: "主导航" });
  expect(within(nav).getByRole("link", { name: "管理员入口" })).toBeTruthy();
  const group = screen.getByRole("button", { name: "系统管理" });
  await user.click(group);
  expect(group.getAttribute("aria-expanded")).toBe("true");
  context.adminEnabled = false;
  rerender(
    <MemoryRouter initialEntries={["/admin"]}>
      <WorkspaceSidebar />
    </MemoryRouter>,
  );
  expect(screen.queryByRole("link", { name: "管理员入口" })).toBeNull();
});

it("synchronizes navigation favorites across sidebar instances even when storage is blocked", async () => {
  const user = userEvent.setup();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  render(
    <MemoryRouter>
      <section aria-label="desktop">
        <WorkspaceSidebar />
      </section>
      <section aria-label="mobile">
        <WorkspaceSidebar />
      </section>
    </MemoryRouter>,
  );
  const desktop = screen.getByRole("region", { name: "desktop" });
  const mobile = screen.getByRole("region", { name: "mobile" });
  await user.click(
    within(desktop).getByRole("button", { name: "收藏 邮箱配置" }),
  );
  expect(
    within(
      within(mobile).getByRole("navigation", { name: "常用模块" }),
    ).getByRole("link", { name: "邮箱配置" }),
  ).toBeTruthy();
  await user.click(
    within(mobile).getByRole("button", { name: "取消收藏 邮箱配置" }),
  );
  expect(
    within(
      within(desktop).getByRole("navigation", { name: "常用模块" }),
    ).queryByRole("link", { name: "邮箱配置" }),
  ).toBeNull();
});

it("preserves in-memory favorites when the mobile drawer mounts later with storage disabled", async () => {
  const user = userEvent.setup();
  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  function LateDrawer() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <section aria-label="desktop">
          <WorkspaceSidebar />
        </section>
        <button onClick={() => setOpen(true)}>Mount drawer</button>
        {open && (
          <section aria-label="late mobile">
            <WorkspaceSidebar />
          </section>
        )}
      </>
    );
  }
  render(
    <MemoryRouter>
      <LateDrawer />
    </MemoryRouter>,
  );
  await user.click(
    within(screen.getByRole("region", { name: "desktop" })).getByRole(
      "button",
      { name: "收藏 邮箱配置" },
    ),
  );
  await user.click(screen.getByRole("button", { name: "Mount drawer" }));
  expect(
    within(
      within(screen.getByRole("region", { name: "late mobile" })).getByRole(
        "navigation",
        { name: "常用模块" },
      ),
    ).getByRole("link", { name: "邮箱配置" }),
  ).toBeTruthy();
});
