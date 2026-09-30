// 后端各端点响应体的前端类型镜像。字段名与 backend/app/*/schemas.py 保持一致，
// 仅保留前端用得到的部分，避免与后端契约漂移。

// ---------------------------------------------------------------------------
// 健康检查
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// 认证
// ---------------------------------------------------------------------------

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user_id: number;
  username: string;
  role: string;
}

// ---------------------------------------------------------------------------
// run
// ---------------------------------------------------------------------------

export type RunStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

// checkpoint 里只对 completed_stages 有强类型依赖，其余字段（case_details 等）
// 是运行期才有的结构，用索引签名放宽，避免后端加字段就崩前端。
export interface RunCheckpoint {
  completed_stages?: string[];
  [key: string]: unknown;
}

export interface RunResponse {
  id: string;
  config_fingerprint: string;
  idempotency_key: string | null;
  experiment_id: string | null;
  dataset_version_id: number;
  param_snapshot_id: number;
  model_version_id: number;
  eval_user_id: number;
  mode: string;
  case_limit: number | null;
  status: string;
  exclusive: boolean;
  current_stage: string | null;
  progress: number;
  retry_count: number;
  error_message: string | null;
  result_summary: Record<string, unknown> | null;
  checkpoint: RunCheckpoint;
  created_by: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

// ---------------------------------------------------------------------------
// 结果查询
// ---------------------------------------------------------------------------

export interface DimensionMetrics {
  dimension: string;
  metrics: Record<string, number>;
}

export interface RunResultsResponse {
  run_id: string;
  status: string;
  dimensions: DimensionMetrics[];
}

export interface CaseResultResponse {
  case_id: number;
  dimension: string;
  // 两个字段的键都是运行期动态的（不同维度列不同），故用 Record 放宽。
  metric_values: Record<string, unknown>;
  detail: Record<string, unknown>;
}

export interface RunCasesResponse {
  run_id: string;
  total: number;
  limit: number;
  offset: number;
  cases: CaseResultResponse[];
}

// ---------------------------------------------------------------------------
// 数据集 / 参数
// ---------------------------------------------------------------------------

export interface DatasetResponse {
  id: number;
  name: string;
  description: string | null;
  created_by: number | null;
  created_at: string;
  is_archived: boolean;
}

export interface ParamSnapshotResponse {
  id: number;
  name: string;
  params: Record<string, unknown>;
  freeze_config: Record<string, unknown>;
  params_hash: string;
  description: string | null;
  created_by: number | null;
  created_at: string;
}

export interface ModelVersionResponse {
  id: number;
  embedding_model_id: string;
  reranker_model_id: string;
  config_hash: string;
  config: Record<string, unknown>;
}
