import { useContext, useEffect } from "react";
import { UNSAFE_NavigationContext } from "react-router-dom";

interface HistoryGuard {
  installed: boolean;
  active: ((event: PopStateEvent) => void) | null;
}
// Preserve the early listener's dispatch object across Vite HMR updates as well.
const historyGuard: HistoryGuard = import.meta.hot?.data.mailHistoryGuard ?? {
  installed: false,
  active: null,
};
if (import.meta.hot) import.meta.hot.data.mailHistoryGuard = historyGuard;

/** Must run before BrowserRouter mounts: at Window's event target, listener
 * registration order matters even when a later listener requests capture. */
export function installMailHistoryGuard() {
  if (historyGuard.installed) return;
  historyGuard.installed = true;
  window.addEventListener(
    "popstate",
    (event) => historyGuard.active?.(event),
    true,
  );
}

/** Scoped BrowserRouter adapter: this page alone protects unsaved in-memory credentials.
 * BrowserRouter has no useBlocker support (it requires a data router). Restore the
 * exact navigator functions on exit rather than changing the app's router architecture.
 */
export function useMailDraftGuard(dirty: boolean, busy: boolean) {
  const { navigator } = useContext(UNSAFE_NavigationContext);
  useEffect(() => {
    if (!dirty && !busy) return;
    const approve = () =>
      !busy && window.confirm("邮箱配置尚未保存，确认离开并放弃修改？");
    const push = navigator.push,
      replace = navigator.replace,
      go = navigator.go;
    let approvedPop = false;
    let restoring = false;
    let popTimer: ReturnType<typeof setTimeout> | undefined;
    const index =
      typeof window.history.state?.idx === "number"
        ? window.history.state.idx
        : null;
    const guardedPush: typeof push = (...args) => {
      if (approve()) push.apply(navigator, args);
    };
    const guardedReplace: typeof replace = (...args) => {
      if (approve()) replace.apply(navigator, args);
    };
    const guardedGo: typeof go = (...args) => {
      if (!approve()) return;
      approvedPop = true;
      popTimer = setTimeout(() => {
        approvedPop = false;
      }, 1000);
      go.apply(navigator, args);
    };
    navigator.push = guardedPush;
    navigator.replace = guardedReplace;
    navigator.go = guardedGo;
    const pop = (event: PopStateEvent) => {
      if (restoring) {
        restoring = false;
        event.stopImmediatePropagation();
        return;
      }
      if (approvedPop) {
        approvedPop = false;
        return;
      }
      const nextIndex =
        typeof event.state?.idx === "number" ? event.state.idx : null;
      // Indexed same-document entries are maintained by BrowserRouter. Cross-document
      // navigation is covered by beforeunload; hash-only entries need no route guard.
      if (
        index === null ||
        nextIndex === null ||
        nextIndex === index ||
        approve()
      )
        return;
      event.stopImmediatePropagation();
      restoring = true;
      window.history.go(index - nextIndex);
    };
    const unload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    // Dispatch through the early bootstrap listener; a page-mounted listener is
    // too late and can allow BrowserRouter to unmount the draft before it runs.
    historyGuard.active = pop;
    window.addEventListener("beforeunload", unload);
    return () => {
      if (navigator.push === guardedPush) navigator.push = push;
      if (navigator.replace === guardedReplace) navigator.replace = replace;
      if (navigator.go === guardedGo) navigator.go = go;
      clearTimeout(popTimer);
      if (historyGuard.active === pop) historyGuard.active = null;
      window.removeEventListener("beforeunload", unload);
    };
  }, [dirty, busy, navigator]);
}
