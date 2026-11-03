import type { ButtonHTMLAttributes, ReactNode } from "react";

/** Compatibility contract for business pages that still import from components/ui. */
export interface LegacyButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost" | "danger" | "dangerSoft" | "success";
  size?: "sm" | "md" | "lg";
  icon?: ReactNode;
  loading?: boolean;
}

export interface LegacyIconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon: ReactNode;
  danger?: boolean;
  active?: boolean;
}
