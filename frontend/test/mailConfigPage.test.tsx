import "@testing-library/jest-dom/vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import MailConfig from "../src/pages/MailConfig";

const { toast, mail } = vi.hoisted(() => ({
  toast: vi.fn(),
  mail: {
    get: vi.fn(),
    save: vi.fn(),
    test: vi.fn(),
    poolDisable: vi.fn(),
    poolEnable: vi.fn(),
    poolRemove: vi.fn(),
    poolRelease: vi.fn(),
    poolRequeue: vi.fn(),
    poolVerify: vi.fn(),
  },
}));
vi.mock("../src/api", () => ({ api: { mailConfig: mail } }));
vi.mock("../src/context/AppContext", () => ({
  useApp: () => ({ t: (s: string) => s, toast }),
}));

const fixture = () => ({
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
    custom_pool_status_counts: { unused: 1, failed: 1 },
    custom_pool_summary: { unused: 1 },
    custom_pool_items: [
      { id: 1, address: "al***e@example.invalid", status: "unused" },
      {
        id: 2,
        address: "bo***b@example.invalid",
        status: "failed",
        reason_code: "pre_submit",
      },
    ],
  },
  icloud: {
    enabled: true,
    mode: "manual_pool",
    accounts_count: 3,
    accounts_sample: ["de***o@icloud.com"],
    poll_interval: 4,
    poll_timeout: 180,
    split_enabled: false,
    split_prefix: "",
    split_tag_length: 6,
    split_max_per_base: 0,
    remaining_rounds: 2,
    exhausted_roots: 1,
  },
  updated_at: "2026-11-02T00:00:00Z",
  test_status: null,
});
function mount() {
  return render(
    <MemoryRouter>
      <MailConfig />
    </MemoryRouter>,
  );
}
async function ready() {
  await screen.findByRole("heading", { name: "邮箱资源中心" });
  await screen.findByLabelText("API 地址");
}
beforeEach(() => {
  vi.resetAllMocks();
  mail.get.mockResolvedValue(fixture());
  mail.save.mockResolvedValue(fixture());
  mail.test.mockResolvedValue({
    provider: "cf_temp_email",
    ok: true,
    message: "测试通过",
  });
  localStorage.clear();
  sessionStorage.clear();
});

it("guards sidebar-style SPA links against discarding unsaved configuration", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/mail-config"]}>
      <Link to="/workspace">其他模块</Link>
      <Routes>
        <Route path="/mail-config" element={<MailConfig />} />
        <Route path="/workspace" element={<h1>工作导航测试页</h1>} />
      </Routes>
    </MemoryRouter>,
  );
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://draft.example.invalid" },
  });
  await user.click(screen.getByRole("link", { name: "其他模块" }));
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://draft.example.invalid",
  );
  confirm.mockReturnValue(true);
  await user.click(screen.getByRole("link", { name: "其他模块" }));
  expect(
    await screen.findByRole("heading", { name: "工作导航测试页" }),
  ).toBeInTheDocument();
  confirm.mockRestore();
});

it("separates browsing providers from changing the registration source", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(screen.getByRole("tab", { name: "iCloud" }));
  expect(screen.getByTestId("saved-mail-source")).toHaveTextContent(
    "Cloudflare",
  );
  expect(screen.queryByText("有未保存修改")).not.toBeInTheDocument();
  expect(mail.save).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "设为注册来源" }));
  expect(screen.getByText("有未保存修改")).toBeInTheDocument();
  expect(mail.save).not.toHaveBeenCalled();
});

it("imports into the viewed iCloud pool without switching the source or saving unrelated drafts", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://draft.example.invalid" },
  });
  await user.click(screen.getByRole("tab", { name: "iCloud" }));
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByRole("button", { name: "批量导入" }));
  const dialog = screen.getByRole("dialog", { name: "导入 iCloud 邮箱池" });
  fireEvent.change(within(dialog).getByLabelText("导入内容"), {
    target: {
      value:
        "new@icloud.com----https://mail.example.invalid/CaseSensitive?key=AbC",
    },
  });
  await user.click(within(dialog).getByRole("button", { name: "导入并保存" }));
  await waitFor(() =>
    expect(mail.save).toHaveBeenCalledWith({
      icloud: {
        accounts_pool:
          "new@icloud.com----https://mail.example.invalid/CaseSensitive?key=AbC",
        accounts_pool_append: true,
      },
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  await user.click(screen.getByRole("tab", { name: "Cloudflare" }));
  await user.click(screen.getByRole("tab", { name: "服务配置" }));
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://draft.example.invalid",
  );
  expect(screen.getByText("有未保存修改")).toBeInTheDocument();
  expect(localStorage.length + sessionStorage.length).toBe(0);
});

it("keeps configuration drafts after pool actions and selects only filtered rows", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://draft.example.invalid" },
  });
  mail.poolDisable.mockResolvedValue({ affected: 1, skipped: [] });
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.selectOptions(screen.getByLabelText("邮箱状态"), "failed");
  await user.click(screen.getByLabelText("选择当前页"));
  await user.click(screen.getByRole("button", { name: "停用" }));
  await waitFor(() =>
    expect(mail.poolDisable).toHaveBeenCalledWith({ ids: [2] }),
  );
  await waitFor(() => expect(mail.get).toHaveBeenCalledTimes(2));
  await user.click(screen.getByRole("tab", { name: "服务配置" }));
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://draft.example.invalid",
  );
});

it("previews invalid and duplicate input without exposing URLs outside the input", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(screen.getByRole("tab", { name: "iCloud" }));
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByRole("button", { name: "批量导入" }));
  const dialog = screen.getByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("导入内容"), {
    target: {
      value:
        "demo@icloud.com----https://mail.example.invalid/?token=Private\nDEMO@icloud.com----https://mail.example.invalid/?token=Other\nbad----not-a-url",
    },
  });
  expect(within(dialog).getByTestId("import-preview")).toHaveTextContent(
    "有效 1",
  );
  expect(within(dialog).getByTestId("import-preview")).toHaveTextContent(
    "重复 1",
  );
  expect(within(dialog).getByTestId("import-preview")).toHaveTextContent(
    "错误 1",
  );
  expect(
    within(dialog).getByRole("button", { name: "导入并保存" }),
  ).toBeDisabled();
  expect(
    within(dialog)
      .queryAllByText(/token=Private/)
      .filter((node) => node.tagName !== "TEXTAREA"),
  ).toHaveLength(0);
  expect(mail.save).not.toHaveBeenCalled();
});

it("saves only changed configuration and never sends masked/empty secrets or pool metadata", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://new.example.invalid" },
  });
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  await waitFor(() =>
    expect(mail.save).toHaveBeenCalledWith({
      cf_temp_email: { base_url: "https://new.example.invalid" },
    }),
  );
});

it("tests the viewed provider without requiring unrelated provider configuration", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "not a URL" },
  });
  await user.click(screen.getByRole("tab", { name: "iCloud" }));
  await user.click(screen.getByRole("button", { name: "测试此服务" }));
  await waitFor(() =>
    expect(mail.test).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "icloud",
        config: {
          icloud: expect.not.objectContaining({
            accounts_pool: expect.anything(),
          }),
        },
      }),
    ),
  );
});

it("requires explicit confirmation before clearing iCloud and keeps unrelated drafts", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(screen.getByRole("tab", { name: "iCloud" }));
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByRole("button", { name: "清空邮箱池" }));
  expect(mail.save).not.toHaveBeenCalled();
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: "取消" }),
  );
  expect(mail.save).not.toHaveBeenCalled();
});

it("blocks configuration writes after a failed initial load and offers retry", async () => {
  mail.get.mockRejectedValueOnce(new Error("offline"));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("加载失败");
  expect(
    screen.queryByRole("button", { name: "保存配置" }),
  ).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "重试加载" }));
  await ready();
});

it("does not lose a draft on save failure and shows a retryable error", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  mail.save.mockRejectedValueOnce(new Error("HTTP 503"));
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://draft.example.invalid" },
  });
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 503");
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://draft.example.invalid",
  );
  expect(screen.getByText("有未保存修改")).toBeInTheDocument();
});

it("requires replacement confirmation and clears input when reopening import", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByRole("button", { name: "批量导入" }));
  const dialog = screen.getByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("导入内容"), {
    target: { value: "demo@example.invalid" },
  });
  await user.selectOptions(
    within(dialog).getByLabelText("导入方式"),
    "replace",
  );
  expect(
    within(dialog).getByRole("button", { name: "导入并保存" }),
  ).toBeDisabled();
  await user.click(within(dialog).getByRole("checkbox"));
  expect(
    within(dialog).getByRole("button", { name: "导入并保存" }),
  ).toBeEnabled();
  await user.click(within(dialog).getByRole("button", { name: "取消" }));
  await user.click(screen.getByRole("button", { name: "批量导入" }));
  expect(screen.getByLabelText("导入内容")).toHaveValue("");
  expect(mail.save).not.toHaveBeenCalled();
});

it("clears iCloud explicitly without persisting unrelated drafts or provider changes", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://draft.example.invalid" },
  });
  await user.click(screen.getByRole("tab", { name: "iCloud" }));
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByRole("button", { name: "清空邮箱池" }));
  await user.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认操作",
    }),
  );
  await waitFor(() =>
    expect(mail.save).toHaveBeenCalledWith({ icloud: { accounts_pool: "" } }),
  );
  await user.click(screen.getByRole("tab", { name: "Cloudflare" }));
  await user.click(screen.getByRole("tab", { name: "服务配置" }));
  expect(screen.getByLabelText("API 地址")).toHaveValue(
    "https://draft.example.invalid",
  );
});

it("treats successful pool writes plus failed refresh as success requiring a manual refresh", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  mail.poolDisable.mockResolvedValue({ affected: 1 });
  mail.get.mockRejectedValueOnce(new Error("offline"));
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByLabelText("选择邮箱 1"));
  await user.click(screen.getByRole("button", { name: "停用" }));
  expect(await screen.findByRole("status")).toHaveTextContent(
    "操作已执行，但资源刷新失败",
  );
  expect(mail.poolDisable).toHaveBeenCalledTimes(1);
});

it("blocks duplicate writes and configuration editing while a save is pending", async () => {
  let resolve!: (value: ReturnType<typeof fixture>) => void;
  mail.save.mockImplementationOnce(
    () =>
      new Promise((r) => {
        resolve = r;
      }),
  );
  const user = userEvent.setup();
  mount();
  await ready();
  fireEvent.change(screen.getByLabelText("API 地址"), {
    target: { value: "https://draft.example.invalid" },
  });
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(screen.getByLabelText("API 地址")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "保存配置" }));
  expect(mail.save).toHaveBeenCalledTimes(1);
  resolve(fixture());
  await waitFor(() => expect(screen.getByLabelText("API 地址")).toBeEnabled());
});

it("shows import failures inside the dialog and does not discard input", async () => {
  const user = userEvent.setup();
  mount();
  await ready();
  mail.save.mockRejectedValueOnce(new Error("邮箱池格式错误"));
  await user.click(screen.getByRole("tab", { name: "邮箱池" }));
  await user.click(screen.getByRole("button", { name: "批量导入" }));
  const dialog = screen.getByRole("dialog");
  fireEvent.change(within(dialog).getByLabelText("导入内容"), {
    target: { value: "demo@example.invalid" },
  });
  await user.click(within(dialog).getByRole("button", { name: "导入并保存" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "邮箱池格式错误",
  );
  expect(within(dialog).getByLabelText("导入内容")).toHaveValue(
    "demo@example.invalid",
  );
});
