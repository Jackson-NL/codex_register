import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import AccountsRefreshPanel from "../src/pages/AccountsRefreshPanel";

// mock api
vi.mock("../src/api", () => ({
  api: {
    accounts: {
      refreshJobActive: vi.fn().mockResolvedValue({ job: null }),
      refreshStart: vi.fn(),
      refreshJob: vi.fn(),
      refreshCancel: vi.fn(),
      refreshSingle: vi.fn(),
    },
  },
}));

// Panel/Button 等组件内部使用 useApp().t，测试里提供直通翻译
vi.mock("../src/context/AppContext", () => ({
  useApp: () => ({ t: (v: string) => v, toast }),
}));

import { api } from "../src/api";

const toast = vi.fn();
const onRefreshed = vi.fn();
const registerSingleRefresh = vi.fn();

function renderPanel(props = {}) {
  return render(
    <AccountsRefreshPanel
      selectedIds={[1, 2]}
      allTotal={10}
      filteredCount={3}
      onRefreshed={onRefreshed}
      registerSingleRefresh={registerSingleRefresh}
      toast={toast}
      {...props}
    />,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  // resetAllMocks 会连默认实现一起清掉，这里恢复默认：无运行中的任务
    vi.mocked(api.accounts.refreshJobActive).mockResolvedValue({ job: null });
});
afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

describe("AccountsRefreshPanel", () => {
  it("取消确认时不会发起刷新请求", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText(/刷新选中账号/));
    expect(screen.getByText(/将向外部 ChatGPT 服务发起 2 个账号/)).toBeInTheDocument();
    await user.click(screen.getByText("取消"));
    expect(api.accounts.refreshStart).not.toHaveBeenCalled();
  });

  it("确认后按 selected 范围提交选中的账号 ID", async () => {
    const user = userEvent.setup();
    vi.mocked(api.accounts.refreshStart).mockResolvedValue({
      id: "job1", status: "succeeded", total: 2, processed: 2,
      succeeded: 2, partial: 0, failed: 0, canceled: 0, stop_requested: false,
      scope: "selected", items: [],
    });
    renderPanel();
    await user.click(screen.getByText(/刷新选中账号/));
    await user.click(screen.getByText("开始刷新"));
    await waitFor(() => {
      expect(api.accounts.refreshStart).toHaveBeenCalledWith([1, 2], "selected");
    });
  });

  it("「全部」提交 scope=all 且不带前端 ID 列表", async () => {
    const user = userEvent.setup();
    vi.mocked(api.accounts.refreshStart).mockResolvedValue({
      id: "job2", status: "succeeded", total: 10, processed: 10,
      succeeded: 10, partial: 0, failed: 0, canceled: 0, stop_requested: false,
      scope: "all", items: [],
    });
    renderPanel();
    await user.click(screen.getByText(/刷新全部/));
    // 确认文案必须说明「全部」≠「当前筛选」
    expect(screen.getByText(/不是仅当前筛选的 3 个/)).toBeInTheDocument();
    await user.click(screen.getByText("开始刷新"));
    await waitFor(() => {
      expect(api.accounts.refreshStart).toHaveBeenCalledWith([], "all");
    });
  });

  it("未选择账号时提示且不发请求", async () => {
    const user = userEvent.setup();
    renderPanel({ selectedIds: [] });
    await user.click(screen.getByText(/刷新选中账号/));
    expect(toast).toHaveBeenCalledWith("请先选择账号", "warning");
    expect(api.accounts.refreshStart).not.toHaveBeenCalled();
  });

  it("进度轮询：running → 终态时回调 onRefreshed", async () => {
    const user = userEvent.setup();
    vi.mocked(api.accounts.refreshStart).mockResolvedValue({
      id: "job3", status: "running", total: 2, processed: 0,
      succeeded: 0, partial: 0, failed: 0, canceled: 0, stop_requested: false,
      scope: "selected", items: [],
    });
    vi.mocked(api.accounts.refreshJob).mockResolvedValueOnce({
      id: "job3", status: "succeeded", total: 2, processed: 2,
      succeeded: 2, partial: 0, failed: 0, canceled: 0, stop_requested: false,
      scope: "selected", items: [],
    });
    renderPanel();
    await user.click(screen.getByText(/刷新选中账号/));
    await user.click(screen.getByText("开始刷新"));
    await waitFor(() => {
      expect(screen.getByText(/总数 2 · 已处理 2/)).toBeInTheDocument();
    }, { timeout: 4000 });
    expect(onRefreshed).toHaveBeenCalled();
  });

  it("停止按钮调用 cancel 且说明已完成结果保留", async () => {
    const user = userEvent.setup();
    vi.mocked(api.accounts.refreshJobActive).mockResolvedValue({
      job: {
        id: "job4", status: "running", total: 5, processed: 2,
        succeeded: 2, partial: 0, failed: 0, canceled: 0, stop_requested: false,
        scope: "selected", items: [],
      },
    });
    vi.mocked(api.accounts.refreshCancel).mockResolvedValue({
      id: "job4", status: "running", total: 5, processed: 2,
      succeeded: 2, partial: 0, failed: 0, canceled: 0, stop_requested: true,
      scope: "selected", items: [],
    });
    renderPanel();
    await screen.findByText(/停止后续处理/);
    await user.click(screen.getByText(/停止后续处理/));
    await waitFor(() => {
      expect(api.accounts.refreshCancel).toHaveBeenCalledWith("job4");
    });
    expect(screen.getByText(/停止中：等待在途账号收尾/)).toBeInTheDocument();
    // toast 明确说明已完成结果保留（不回滚）
    expect(toast).toHaveBeenCalledWith(
      expect.stringMatching(/已完成的刷新结果保留/),
      "info",
    );
  });

  it("服务重启(404)时显示任务丢失且不误报成功", async () => {
    const user = userEvent.setup();
    vi.mocked(api.accounts.refreshStart).mockResolvedValue({
      id: "job5", status: "running", total: 3, processed: 0,
      succeeded: 0, partial: 0, failed: 0, canceled: 0, stop_requested: false,
      scope: "selected", items: [],
    });
    vi.mocked(api.accounts.refreshJob).mockRejectedValue(new Error("HTTP 404"));
    renderPanel();
    await user.click(screen.getByText(/刷新选中账号/));
    await user.click(screen.getByText("开始刷新"));
    await waitFor(() => {
      expect(screen.getByText(/任务已丢失/)).toBeInTheDocument();
    }, { timeout: 4000 });
    // 快照终态为 failed（任务丢失）而非成功：通过「成功 0」且不出现「成功 3」验证
    expect(screen.getByText(/成功 0/)).toBeInTheDocument();
    expect(screen.queryByText(/成功 3/)).not.toBeInTheDocument();
  });

  it("额度未知显示「未知」，不显示 0", async () => {
    const user = userEvent.setup();
    vi.mocked(api.accounts.refreshStart).mockResolvedValue({
      id: "job6", status: "succeeded", total: 1, processed: 1,
      succeeded: 1, partial: 0, failed: 0, canceled: 0, stop_requested: false,
      scope: "selected",
      items: [{
        account_id: 7, status: "succeeded", error_code: "", error: "",
        credential_rotated: false, plan_type: "plus", quota_status: "unknown",
        quota_label: "图像额度未知", quota_value: null, quota_kind: "image_gen",
        restore_at: "", email: "", identity_matched: true,
      }],
    });
    renderPanel();
    await user.click(screen.getByText(/刷新选中账号/));
    await user.click(screen.getByText("开始刷新"));
    // 展开逐账号结果表
    await waitFor(() => {
      expect(screen.getByText(/总数 1 · 已处理 1/)).toBeInTheDocument();
    }, { timeout: 4000 });
    await user.click(screen.getByRole("button", { name: "切换逐账号结果" }));
    await waitFor(() => {
      expect(screen.getByText(/acc_7/)).toBeInTheDocument();
    });
    expect(screen.getByText("未知")).toBeInTheDocument();
    expect(screen.getByText(/上游未返回额度/)).toBeInTheDocument();
    expect(screen.getByText("plus")).toBeInTheDocument();
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });

  it("注册单账号刷新入口并防重复提交", async () => {
    vi.mocked(api.accounts.refreshSingle).mockResolvedValue({});
    renderPanel();
    // registerSingleRefresh 已在 effect 中注册
    await waitFor(() => {
      expect(registerSingleRefresh).toHaveBeenCalled();
    });
    const calls = registerSingleRefresh.mock.calls;
    const refreshFn = calls[calls.length - 1][0] as (id: number) => Promise<void>;
    // 1) 空闲时可正常调用
    await refreshFn(42);
    expect(api.accounts.refreshSingle).toHaveBeenCalledWith(42);
    // 2) busy 时直接返回（组件内部 singleBusyId 挡住并发）
    vi.mocked(api.accounts.refreshSingle).mockImplementation(
      () => new Promise((resolve) => setTimeout(resolve, 50)),
    );
    const first = refreshFn(43);
    const second = refreshFn(44); // busy 期间应被忽略
    await Promise.all([first, second]);
    // 只成功发起一次（43），44 被 busy 拦截
    expect(api.accounts.refreshSingle).toHaveBeenCalledWith(43);
    expect(api.accounts.refreshSingle).not.toHaveBeenCalledWith(44);
  });

  it("页面重进时恢复运行中的任务", async () => {
    vi.mocked(api.accounts.refreshJobActive).mockResolvedValue({
      job: {
        id: "job7", status: "running", total: 9, processed: 4,
        succeeded: 3, partial: 1, failed: 0, canceled: 0, stop_requested: false,
        scope: "all", items: [],
      },
    });
    renderPanel();
    await screen.findByText(/总数 9 · 已处理 4/);
    expect(screen.getByText(/全部账号（服务端全集）/)).toBeInTheDocument();
  });
});
