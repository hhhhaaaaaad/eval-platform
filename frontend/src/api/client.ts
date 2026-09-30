// 基于 fetch 的薄封装。
// 为什么自己封装：统一拼接后端前缀、统一把非 2xx 转成可识别的 ApiError，
// 避免每个页面重复处理 status 判断与响应体读取。

import { clearSession, getToken } from './session';

const API_BASE = '/api/v1';

// 非 2xx 响应统一抛出的错误类型，携带 HTTP 状态码与响应体摘要，便于页面展示与后续判定（如 503 未就绪）。
export class ApiError extends Error {
  readonly status: number;
  readonly bodyText: string;

  constructor(status: number, bodyText: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.bodyText = bodyText;
  }
}

// 单个请求的附加选项。
export interface RequestOptions {
  // 登录接口自身的密码错误也是 401，若也触发「清 token + 跳登录」会把
  // 错误提示冲掉，故登录请求显式跳过该兜底，改由调用方就地展示错误。
  skipUnauthorizedRedirect?: boolean;
}

function buildHeaders(hasBody: boolean): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (hasBody) {
    headers['Content-Type'] = 'application/json';
  }

  // P0-1 鉴权接入点：有 token 时注入 Authorization 头。
  // 未登录（无 token）时静默不带，让后端返回 401 由下方统一兜底。
  const token = getToken();
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  return headers;
}

// 读取响应体并构造 ApiError；响应体不可读时降级为占位文本，保证错误路径自身不抛异常。
async function toApiError(response: Response): Promise<ApiError> {
  let bodyText = '';
  try {
    bodyText = await response.text();
  } catch {
    bodyText = '<响应体不可读>';
  }
  const summary = bodyText.length > 300 ? `${bodyText.slice(0, 300)}…` : bodyText;
  return new ApiError(
    response.status,
    bodyText,
    `请求 ${response.url} 失败（HTTP ${response.status}）：${summary}`,
  );
}

// 401 全局兜底：token 已失效或非法时，清空会话并回登录页。
// 用整页跳转而非 SPA 内导航，是为了让所有页面组件随路由守卫一起重新评估，
// 避免「已清 token 但当前页仍在渲染」的半死状态。
function redirectToLogin(): void {
  window.location.assign('/login');
}

async function request<T>(path: string, init: RequestInit, options?: RequestOptions): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);

  if (!response.ok) {
    if (response.status === 401 && !options?.skipUnauthorizedRedirect) {
      clearSession();
      redirectToLogin();
    }
    throw await toApiError(response);
  }

  // 204 或空响应体没有可解析内容，返回 undefined（调用方按需处理）。
  if (response.status === 204) {
    return undefined as unknown as T;
  }

  const text = await response.text();
  if (text.length === 0) {
    return undefined as unknown as T;
  }

  return JSON.parse(text) as T;
}

export function apiGet<T>(path: string, options?: RequestOptions): Promise<T> {
  return request<T>(path, { method: 'GET', headers: buildHeaders(false) }, options);
}

export function apiPost<T>(
  path: string,
  body: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>(
    path,
    {
      method: 'POST',
      headers: buildHeaders(true),
      body: JSON.stringify(body),
    },
    options,
  );
}
