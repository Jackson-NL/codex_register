import { webcrypto } from "node:crypto";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import MfaCodes from "../src/pages/MfaCodes";

const { toast, translate } = vi.hoisted(() => ({
  toast: vi.fn(),
  translate: (text: string) => text,
}));
vi.mock("../src/context/AppContext", () => ({
  useApp: () => ({ t: translate, toast }),
}));

beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue({
        ok: true,
        json: async () => ({
          server_time: Math.floor(Date.now() / 1000),
          items: [
            {
              email: "demo@example.invalid",
              status: "ok",
              code: "123456",
              period: 30,
              expires_at: Math.floor(Date.now() / 30000) * 30 + 30,
            },
          ],
        }),
      }),
  );
  localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
});

it("generates locally, copies, clears and never sends or stores secrets", async () => {
  const user = userEvent.setup();
  render(<MfaCodes />);
  fireEvent.change(screen.getByLabelText("密钥数据"), {
    target: {
      value: "demo@example.invalid----not-retained----JBSWY3DPEHPK3PXP",
    },
  });
  await user.click(screen.getByRole("button", { name: "生成验证码" }));
  await waitFor(() =>
    expect(screen.getByTestId("local-code-0").textContent).toMatch(
      /\d{3} \d{3}/,
    ),
  );
  expect(fetch).not.toHaveBeenCalled();
  expect(localStorage.length).toBe(0);
  expect(screen.queryByText("JBSWY3DPEHPK3PXP")).toBeNull();
  await user.click(
    screen.getByRole("button", { name: "复制验证码 demo@example.invalid" }),
  );
  expect(toast).toHaveBeenCalledWith("已复制", "success");
  await user.click(screen.getByRole("button", { name: "清空密钥数据" }));
  expect(screen.queryByTestId("local-code-0")).toBeNull();
  expect((screen.getByLabelText("密钥数据") as HTMLTextAreaElement).value).toBe(
    "",
  );
});

it("queries database with only emails and clears results", async () => {
  const user = userEvent.setup();
  render(<MfaCodes />);
  await user.type(screen.getByLabelText("查询邮箱"), "demo@example.invalid");
  await user.click(screen.getByRole("button", { name: "查询验证码" }));
  expect(await screen.findByText("123 456")).toBeTruthy();
  expect(fetch).toHaveBeenCalledWith(
    "/api/mfa/codes",
    expect.objectContaining({
      body: JSON.stringify({ emails: ["demo@example.invalid"] }),
      cache: "no-store",
    }),
  );
  await user.click(screen.getByRole("button", { name: "清空邮箱查询" }));
  expect(screen.queryByText("123 456")).toBeNull();
});

it("adds in-memory accounts and warns before JSON export", async () => {
  const user = userEvent.setup();
  render(<MfaCodes />);
  await user.type(
    screen.getByLabelText("邮箱 / 账号"),
    "local@example.invalid",
  );
  await user.type(
    screen.getByLabelText("Base32 / otpauth"),
    "JBSWY3DPEHPK3PXP",
  );
  await user.click(screen.getByRole("button", { name: "加入本页" }));
  expect(await screen.findByText("local@example.invalid")).toBeTruthy();
  expect(
    (screen.getByLabelText("Base32 / otpauth") as HTMLInputElement).value,
  ).toBe("");
  await user.click(screen.getByRole("button", { name: "导出 JSON" }));
  expect(screen.getByRole("dialog").textContent).toContain("明文密钥");
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(fetch).not.toHaveBeenCalled();
});

it("does not overwrite accounts added during asynchronous JSON reading", async () => {
  const user = userEvent.setup();
  render(<MfaCodes />);
  let resolveText!: (value: string) => void;
  const file = new File(["{}"], "mfa.json", { type: "application/json" });
  Object.defineProperty(file, "text", {
    value: () =>
      new Promise<string>((resolve) => {
        resolveText = resolve;
      }),
  });
  fireEvent.change(screen.getByLabelText("导入 MFA JSON"), {
    target: { files: [file] },
  });
  await user.type(screen.getByLabelText("邮箱 / 账号"), "new@example.invalid");
  await user.type(
    screen.getByLabelText("Base32 / otpauth"),
    "JBSWY3DPEHPK3PXP",
  );
  await user.click(screen.getByRole("button", { name: "加入本页" }));
  resolveText(
    JSON.stringify({
      accounts: [
        { label: "import@example.invalid", secret: "JBSWY3DPEHPK3PXP" },
      ],
    }),
  );
  await waitFor(() =>
    expect(screen.getByText("new@example.invalid")).toBeTruthy(),
  );
  await waitFor(() =>
    expect(toast).toHaveBeenCalledWith(
      "读取期间数据已更改，请重新导入文件",
      "info",
    ),
  );
  expect(screen.queryByText("import@example.invalid")).toBeNull();
});

it("hides expired online codes before refreshing and aborts polling on unmount", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(59900);
  vi.mocked(fetch).mockResolvedValue({
    ok: true,
    json: async () => ({
      server_time: 59.9,
      items: [
        {
          email: "demo@example.invalid",
          status: "ok",
          code: "123456",
          period: 30,
          expires_at: 60,
        },
      ],
    }),
  } as Response);
  const { unmount } = render(<MfaCodes />);
  fireEvent.change(screen.getByLabelText("查询邮箱"), {
    target: { value: "demo@example.invalid" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询验证码" }));
  await vi.waitFor(() => expect(screen.getByText("123 456")).toBeTruthy());
  await vi.advanceTimersByTimeAsync(400);
  await vi.waitFor(() => expect(screen.queryByText("123 456")).toBeNull());
  await vi.advanceTimersByTimeAsync(1000);
  expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1);
  unmount();
  const count = vi.mocked(fetch).mock.calls.length;
  await vi.advanceTimersByTimeAsync(30000);
  expect(vi.mocked(fetch).mock.calls.length).toBe(count);
});
