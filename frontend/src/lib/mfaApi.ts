export interface OnlineCode {
  email: string;
  status: "ok" | "not_found" | "no_secret" | "invalid_secret" | "duplicate";
  code: string | null;
  period: number;
  expires_at: number;
}
export interface CodeResponse {
  server_time: number;
  items: OnlineCode[];
}

export async function lookupMfa(
  emails: string[],
  signal: AbortSignal,
): Promise<CodeResponse> {
  const response = await fetch("/api/mfa/codes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    cache: "no-store",
    body: JSON.stringify({ emails }),
    signal,
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "请先登录管理员"
        : response.status === 403
          ? "查询来源被拒绝"
          : "MFA 查询失败，请稍后重试",
    );
  const data = (await response.json()) as CodeResponse;
  if (
    !Number.isFinite(data.server_time) ||
    !Array.isArray(data.items) ||
    !data.items.length ||
    data.items.length > 100 ||
    data.items.some(
      (row) =>
        typeof row.email !== "string" ||
        ![
          "ok",
          "not_found",
          "no_secret",
          "invalid_secret",
          "duplicate",
        ].includes(row.status) ||
        !Number.isFinite(row.expires_at) ||
        !Number.isInteger(row.period) ||
        row.period < 5 ||
        row.period > 300 ||
        (row.status === "ok" &&
          (typeof row.code !== "string" || !/^\d{6,8}$/.test(row.code))),
    )
  )
    throw new Error("MFA 返回数据无效");
  return data;
}
