// 登录 / 登出的业务入口：调用后端 /auth/login，并把会话写入本地存储。
// 与 client 的解耦点：登录请求跳过 401 全局兜底，就地抛友好错误。

import { ApiError, apiPost } from './client';
import { clearSession, getSessionUser, setSessionUser, setToken } from './session';
import type { LoginResponse } from './types';

// 从 401 响应体里提取 FastAPI 的 detail 字段，得到「用户名或密码错误」这类可读文案。
function extractDetail(bodyText: string): string | null {
  try {
    const parsed = JSON.parse(bodyText) as { detail?: unknown };
    if (typeof parsed.detail === 'string') {
      return parsed.detail;
    }
  } catch {
    // 响应体不是 JSON（理论上不会），降级返回 null。
  }
  return null;
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  try {
    const res = await apiPost<LoginResponse>(
      '/auth/login',
      { username, password },
      { skipUnauthorizedRedirect: true },
    );
    setToken(res.access_token);
    setSessionUser({ username: res.username, role: res.role });
    return res;
  } catch (err) {
    // 密码错误/用户不存在都是 401，统一转成后端给出的可读文案，避免把
    // 「HTTP 401：{...}」这种机器文案直接丢给用户。
    if (err instanceof ApiError && err.status === 401) {
      throw new Error(extractDetail(err.bodyText) ?? '用户名或密码错误');
    }
    throw err;
  }
}

export function logout(): void {
  clearSession();
}

export function getCurrentUser() {
  return getSessionUser();
}
