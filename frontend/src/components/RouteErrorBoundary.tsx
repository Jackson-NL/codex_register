import { Component, type ReactNode } from "react";
import { RefreshCw, Unplug } from "lucide-react";
import { Button } from "./ui/button";

interface Props { children: ReactNode; onReload?: () => void }
interface State { failed: boolean }

/** Isolate lazy chunk failures from the navigation shell; never render raw errors. */
export class RouteErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State { return { failed: true }; }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <section role="alert" className="mx-auto flex min-h-80 max-w-lg flex-col items-center justify-center gap-4 rounded-xl border border-border bg-card p-8 text-center text-card-foreground">
        <span className="flex size-12 items-center justify-center rounded-xl bg-muted text-muted-foreground"><Unplug size={24} /></span>
        <h1 className="text-xl font-semibold">页面暂时无法加载</h1>
        <p className="text-sm leading-6 text-muted-foreground">连接中断或页面版本已更新，请刷新后重试。当前数据不会因此被删除。</p>
        <Button onClick={() => this.props.onReload ? this.props.onReload() : window.location.reload()}><RefreshCw size={16} />刷新页面</Button>
      </section>
    );
  }
}
