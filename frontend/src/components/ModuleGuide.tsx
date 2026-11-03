import { ArrowUpRight, ChevronDown, Info } from "lucide-react";
import { Link } from "react-router-dom";
import { useApp } from "../context/AppContext";
import { MODULES, type WorkspaceModule } from "../lib/navigation";

export function ModuleGuide({ module }: { module: WorkspaceModule }) {
  const { t, adminEnabled } = useApp();
  if (module.id === "workspace") return null;
  const next = module.next
    .map((id) => MODULES.find((item) => item.id === id))
    .filter(
      (item): item is WorkspaceModule =>
        !!item && (adminEnabled || !item.adminOnly),
    );
  return (
    <details className="ws-module-guide" key={module.id}>
      <summary>
        <Info size={14} />
        <strong>{t("模块指南")}</strong>
        <span>{t(module.shortDescription)}</span>
        <ChevronDown size={13} />
      </summary>
      <div className="ws-module-guide-body">
        <div>
          <p>{t(module.description)}</p>
          <ul>
            {module.capabilities.map((capability) => (
              <li key={capability}>{t(capability)}</li>
            ))}
          </ul>
        </div>
        <div>
          <small>{t("相关模块")}</small>
          <div>
            {next.map((item) => (
              <Link to={item.path} key={item.id}>
                <item.icon size={14} />
                {t(item.label)}
                <ArrowUpRight size={12} />
              </Link>
            ))}
          </div>
        </div>
      </div>
    </details>
  );
}
