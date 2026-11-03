import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Modal, Button, Dropdown, IconBtn } from "../src/components/ui";

vi.mock("../src/context/AppContext", () => ({ useApp: () => ({ t: (value: string) => value }) }));

describe("existing business-page component contracts", () => {
  it("keeps loading buttons disabled and forwards DOM attributes", () => {
    render(<Button loading aria-label="Saving" variant="primary" size="md" icon={null} className="" title="" onClick={vi.fn()} disabled={false}>Save</Button>);
    const button = screen.getByRole("button", { name: "Saving" });
    expect(button.getAttribute("aria-busy")).toBe("true");
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("preserves non-closable modal semantics and restores opener focus", async () => {
    const user = userEvent.setup();
    function Example() {
      const [open, setOpen] = useState(false);
      return <><button onClick={() => setOpen(true)}>Open legacy modal</button><Modal open={open} onClose={() => setOpen(false)} title="Confirm task" width={420} closable={false} footer={null}><button onClick={() => setOpen(false)}>Finish task</button></Modal></>;
    }
    render(<Example />);
    await user.click(screen.getByRole("button", { name: "Open legacy modal" }));
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Confirm task" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "关闭" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Finish task" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Open legacy modal" }));
  });

  it("supports the existing IconBtn dropdown trigger with keyboard navigation", async () => {
    const user = userEvent.setup();
    const action = vi.fn();
    render(<Dropdown align="right" trigger={<IconBtn title="Row actions" icon={<span>…</span>} onClick={undefined} danger={false} active={false} className="" />} items={[{ label: "Edit row", onClick: action }]} />);
    await user.tab();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("menuitem", { name: "Edit row" })).toBeTruthy();
    await user.keyboard("{Enter}");
    expect(action).toHaveBeenCalledOnce();
  });

  it("restores the stable row trigger after a menu item opens a modal", async () => {
    const user = userEvent.setup();
    function Example() {
      const [open, setOpen] = useState(false);
      return <><Dropdown align="right" trigger={<IconBtn title="More row actions" icon={<span>…</span>} />} items={[{ label: "Inspect", onClick: () => setOpen(true) }]} /><Modal open={open} onClose={() => setOpen(false)} title="Inspect row" width={420} footer={null}><input aria-label="Notes" /></Modal></>;
    }
    render(<Example />);
    await user.click(screen.getByRole("button", { name: "More row actions" }));
    await user.click(screen.getByRole("menuitem", { name: "Inspect" }));
    expect(screen.getByRole("dialog", { name: "Inspect row" })).toBeTruthy();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "More row actions" })));
  });
});
