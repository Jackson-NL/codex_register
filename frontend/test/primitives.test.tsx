import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button } from "../src/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "../src/components/ui/dialog";
import { Switch } from "../src/components/ui/switch";
import { cn } from "../src/lib/utils";

describe("typed shadcn primitives", () => {
  it("merges conflicting Tailwind classes instead of stacking styles", () => {
    expect(cn("p-2 text-sm", false, "p-4")).toBe("text-sm p-4");
  });

  it("supports button refs, disabled state and custom classes", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button disabled onClick={onClick} className="h-12">Submit</Button>);
    await user.click(screen.getByRole("button", { name: "Submit" }));
    expect(onClick).not.toHaveBeenCalled();
    expect(screen.getByRole("button").classList.contains("h-12")).toBe(true);
  });

  it("opens an accessible dialog, closes with Escape and restores focus", async () => {
    const user = userEvent.setup();
    render(<Dialog><DialogTrigger asChild><Button>Open dialog</Button></DialogTrigger><DialogContent aria-describedby={undefined}><DialogTitle>Account details</DialogTitle><input aria-label="Name" /></DialogContent></Dialog>);
    await user.click(screen.getByRole("button", { name: "Open dialog" }));
    expect(screen.getByRole("dialog", { name: "Account details" })).toBeTruthy();
    expect(document.activeElement).toBe(screen.getByRole("textbox", { name: "Name" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Open dialog" }));
  });

  it("uses switch semantics and supports keyboard toggling", async () => {
    const user = userEvent.setup();
    function Example() {
      const [checked, setChecked] = useState(false);
      return <Switch aria-label="Auto refresh" checked={checked} onCheckedChange={setChecked} />;
    }
    render(<Example />);
    const toggle = screen.getByRole("switch", { name: "Auto refresh" });
    await user.tab();
    await user.keyboard(" ");
    expect(toggle.getAttribute("aria-checked")).toBe("true");
  });
});
