import "@testing-library/jest-dom/vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  BrowserRouter,
  Link,
  MemoryRouter,
  Route,
  Routes,
  useNavigate,
} from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  installMailHistoryGuard,
  useMailDraftGuard,
} from "../src/lib/useMailDraftGuard";

// Install before any BrowserRouter listener, exactly as main.tsx does.
installMailHistoryGuard();
function Guard({ busy = false }: { busy?: boolean }) {
  useMailDraftGuard(true, busy);
  const navigate = useNavigate();
  return (
    <>
      <p>草稿保留</p>
      <Link to="/workspace">离开模块</Link>
      <button onClick={() => navigate(-1)}>返回前页</button>
    </>
  );
}
beforeEach(() => {
  window.history.replaceState({ idx: 5 }, "", "/mail-config");
});
afterEach(() => vi.restoreAllMocks());

it("rejects native history travel before BrowserRouter can unmount the draft, then permits later travel", () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const go = vi.spyOn(window.history, "go").mockImplementation(() => {});
  render(
    <BrowserRouter>
      <Routes>
        <Route path="/mail-config" element={<Guard />} />
        <Route path="/workspace" element={<h1>其他模块</h1>} />
      </Routes>
    </BrowserRouter>,
  );
  act(() => {
    window.history.replaceState({ idx: 4 }, "", "/workspace");
    window.dispatchEvent(new PopStateEvent("popstate", { state: { idx: 4 } }));
  });
  expect(go).toHaveBeenCalledWith(1);
  expect(screen.getByText("草稿保留")).toBeInTheDocument();
  act(() => {
    window.history.replaceState({ idx: 5 }, "", "/mail-config");
    window.dispatchEvent(new PopStateEvent("popstate", { state: { idx: 5 } }));
  });
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(screen.getByText("草稿保留")).toBeInTheDocument();
  confirm.mockReturnValue(true);
  act(() => {
    window.history.replaceState({ idx: 4 }, "", "/workspace");
    window.dispatchEvent(new PopStateEvent("popstate", { state: { idx: 4 } }));
  });
  expect(screen.getByRole("heading", { name: "其他模块" })).toBeInTheDocument();
});

it("blocks navigation without confirmation during in-flight configuration writes", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/workspace", "/mail-config"]}>
      <Routes>
        <Route path="/mail-config" element={<Guard busy />} />
        <Route path="/workspace" element={<h1>其他模块</h1>} />
      </Routes>
    </MemoryRouter>,
  );
  await user.click(screen.getByRole("link", { name: "离开模块" }));
  await user.click(screen.getByRole("button", { name: "返回前页" }));
  expect(confirm).not.toHaveBeenCalled();
  expect(screen.getByText("草稿保留")).toBeInTheDocument();
});

it("protects hard reloads only while the guarded module is mounted", () => {
  const { unmount } = render(
    <MemoryRouter>
      <Guard />
    </MemoryRouter>,
  );
  const before = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(before);
  expect(before.defaultPrevented).toBe(true);
  unmount();
  const after = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(after);
  expect(after.defaultPrevented).toBe(false);
});
