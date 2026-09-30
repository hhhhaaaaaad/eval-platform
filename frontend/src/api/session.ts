// 登录会话的本地持久化。token 与用户信息存 localStorage，
// 键名集中在此定义，避免散落各处导致「清 token」漏掉某个键。
//
// 为什么放 localStorage 而非内存：刷新页面后仍能保持登录态，
// 否则每次刷新都要重新登录，开发体验差。

const TOKEN_KEY = 'eval_platform_token';
const USER_KEY = 'eval_platform_user';

export interface SessionUser {
  username: string;
  role: string;
}

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function getSessionUser(): SessionUser | null {
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as SessionUser;
  } catch {
    return null;
  }
}

export function setSessionUser(user: SessionUser): void {
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

// 清空全部会话状态（token + 用户信息），用于登出与 401 兜底。
export function clearSession(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

export function isAuthenticated(): boolean {
  return getToken() !== null;
}
