// 后端 GET /api/v1/health 的响应体
export interface HealthResponse {
  status: string;
  service: string;
  environment: string;
}

// 就绪检查中的单项结果（Postgres / Redis）
export interface ReadyCheck {
  ok: boolean;
}

// 后端 GET /api/v1/ready 的响应体。
// 注意：就绪失败时后端返回 HTTP 503，此时由 client 抛出 ApiError，不会走到本类型。
export interface ReadyResponse {
  status: 'ready' | 'not_ready';
  checks: {
    postgres: ReadyCheck;
    redis: ReadyCheck;
  };
}
