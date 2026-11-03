import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RouteErrorBoundary } from "../src/components/RouteErrorBoundary";

describe("route loading errors", () => {
  it("keeps the shell mounted and offers recovery without leaking error details", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const suppressExpectedError = (event: ErrorEvent) => event.preventDefault();
    window.addEventListener("error", suppressExpectedError);
    const user = userEvent.setup();
    const reload = vi.fn();
    function Broken(): never { throw new Error("private module URL or stack"); }
    render(<><nav>Workspace navigation</nav><RouteErrorBoundary onReload={reload}><Broken /></RouteErrorBoundary></>);
    expect(screen.getByRole("navigation").textContent).toBe("Workspace navigation");
    expect(screen.getByRole("alert").textContent).not.toContain("private module");
    await user.click(screen.getByRole("button", { name: "刷新页面" }));
    expect(reload).toHaveBeenCalledOnce();
    window.removeEventListener("error", suppressExpectedError);
  });
});
