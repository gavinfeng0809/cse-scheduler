/**
 * 统一 API 调用（2026-10-10 新增）：
 *   1) 自动注入 Authorization（从 localStorage 读，避免闭包旧令牌竞态）
 *   2) **滑动续期**：后端在令牌快过期时会在响应头回一个新令牌，这里自动写回 localStorage
 *   3) 401 统一处理：清掉本地登录态并广播 `cse:unauthorized` 事件，由 App 提示"登录已过期"
 *
 * 之前每个组件各写一份 apiCall，行为不一致且没法统一续期，现集中到这里。
 */
export const TOKEN_KEY = "cse_token";
export const USER_KEY = "cse_user";
export const UNAUTHORIZED_EVENT = "cse:unauthorized";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(tk: string) {
  localStorage.setItem(TOKEN_KEY, tk);
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export async function apiCall(url: string, options: any = {}): Promise<any> {
  const headers = { ...(options.headers || {}) };
  const tk = getToken();
  if (tk) headers.Authorization = `Bearer ${tk}`;

  const resp = await fetch(url, { ...options, headers });

  // 滑动续期：后端在有效期过半后会带回新令牌
  const fresh = resp.headers.get("x-refresh-token");
  if (fresh) setToken(fresh);

  let body: any = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }

  if (resp.status === 401) {
    clearAuth();
    const detail = typeof body?.detail === "string" ? body.detail : "登录已过期，请重新登录";
    window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail }));
    throw new Error(detail);
  }

  if (!resp.ok) {
    const d = body?.detail;
    const err: any = new Error(
      typeof d === "object" ? d?.message || JSON.stringify(d) : d || `HTTP ${resp.status}`
    );
    err.detail = d;
    throw err;
  }

  return body;
}
