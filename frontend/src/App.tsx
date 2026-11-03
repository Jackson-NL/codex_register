import { lazy, Suspense, type ReactNode } from "react";
import { Routes, Route } from "react-router-dom";
import { Loader2 } from "lucide-react";
import Layout from "./components/Layout";
import { AppProvider, useApp } from "./context/AppContext";
import { RouteErrorBoundary } from "./components/RouteErrorBoundary";

const Workspace = lazy(() => import("./pages/Workspace"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Register = lazy(() => import("./pages/Register"));
const Accounts = lazy(() => import("./pages/Accounts"));
const Proxies = lazy(() => import("./pages/Proxies"));
const Settings = lazy(() => import("./pages/Settings"));
// Keep the existing route while loading the typed mail resource center.
const MailConfig = lazy(() => import("./pages/MailConfig"));
const CodexOAuth = lazy(() => import("./pages/CodexOAuth"));
const AccountRelogin = lazy(() => import("./pages/AccountRelogin"));
const MfaCodes = lazy(() => import("./pages/MfaCodes"));
const Admin = lazy(() => import("./pages/Admin"));
const AdminLoginScreen = lazy(() => import("./pages/Admin").then((module) => ({ default: module.AdminLoginScreen })));

function PageLoading() {
  const { t } = useApp();
  return <div role="status" className="flex min-h-64 items-center justify-center gap-3 text-sm text-muted-foreground"><Loader2 className="size-5 animate-spin text-primary" />{t("加载中…")}</div>;
}

function AdminGate({ children }: { children: ReactNode }) {
  const { adminChecking, adminEnabled, adminAuthenticated } = useApp();
  if (adminChecking) return <PageLoading />;
  if (!adminEnabled) return children;
  if (!adminAuthenticated) return <AdminLoginScreen />;
  return children;
}

export default function App() {
  return (
    <RouteErrorBoundary><AppProvider>
      <Suspense fallback={<PageLoading />}>
        <AdminGate>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/workspace" element={<Workspace />} />
              <Route path="/register" element={<Register />} />
              <Route path="/codex-oauth" element={<CodexOAuth />} />
              <Route path="/account-relogin" element={<AccountRelogin />} />
              <Route path="/mfa-codes" element={<MfaCodes />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/proxies" element={<Proxies />} />
              <Route path="/mail-config" element={<MailConfig />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/admin" element={<Admin />} />
              <Route path="*" element={<Dashboard />} />
            </Route>
          </Routes>
        </AdminGate>
      </Suspense>
    </AppProvider></RouteErrorBoundary>
  );
}
