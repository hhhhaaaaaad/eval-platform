// 基于 fetch 的薄封装。
// 为什么自己封装：统一拼接后端前缀、统一把非 2xx 转成可识别的 ApiError，
// 避免每个页面重复处理 status 判断与响应体读取。

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

function buildHeaders(hasBody: boolean): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (hasBody) {
    headers['Content-Type'] = 'application/json';
  }

  // TODO(P0-1): 鉴权接入点。
  // 后端加入 JWT/Token 后在此注入 Authorization 头，例如：
  //   const token = getToken();
  //   if (token) headers['Authorization'] = `Bearer ${token}`;

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

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);

  if (!response.ok) {
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

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'GET', headers: buildHeaders(false) });
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: buildHeaders(true),
    body: JSON.stringify(body),
  });
}
