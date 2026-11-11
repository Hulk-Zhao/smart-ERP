/** problem+json 错误体与前端统一处置口径（对齐 C16 契约文档的错误表）。 */

export type FailCode = 'F1' | 'F2' | 'F3' | 'F4' | 'F5' | 'F6' | 'F7' | 'F8';

export interface ProblemBody {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code?: string;
  trace_id?: string;
  retriable?: boolean;
  fail_code?: FailCode;
  /** 角色差集：与工单详情同名字段同义；仅 E-POLICY-APPROVAL 的 409 携带。 */
  approval_required_roles?: string[] | null;
  approval_missing_roles?: string[] | null;
  execution_required_roles?: string[] | null;
  execution_missing_roles?: string[] | null;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly title: string;
  readonly detail: string;
  readonly traceId?: string;
  readonly retriable: boolean;
  readonly failCode?: FailCode;
  readonly body: ProblemBody;

  constructor(body: ProblemBody, fallbackStatus = 0) {
    const detail = body.detail || body.title || '请求失败';
    super(detail);
    this.name = 'ApiError';
    this.body = body;
    this.status = body.status ?? fallbackStatus;
    this.code = body.code ?? (this.status ? `HTTP-${this.status}` : 'NETWORK');
    this.title = body.title ?? '请求失败';
    this.detail = detail;
    this.traceId = body.trace_id;
    this.retriable = body.retriable ?? this.status >= 500;
    this.failCode = body.fail_code;
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/** W3 / 需要更高权限：前端必须明确「连接器零写入」。 */
export function isW3Forbidden(error: unknown): boolean {
  return isApiError(error) && error.status === 403 && error.code === 'E-POLICY-W3FORBIDDEN';
}

/** 审批未满足 / 审批意见缺失 / 重复审批。 */
export function isApprovalConflict(error: unknown): boolean {
  return isApiError(error) && error.status === 409 && error.code === 'E-POLICY-APPROVAL';
}

export function isScopeDenied(error: unknown): boolean {
  return isApiError(error) && (error.status === 401 || error.status === 403);
}

/** 错误大类：统一决定界面文案与下一步动作，页面不再各自判断 status/code。 */
export type ErrorKind = 'forbidden' | 'notfound' | 'conflict' | 'config' | 'network' | 'generic';

/**
 * 配置缺失不是故障，界面必须明确「去找管理员配置」，不能让用户反复重试。
 * 契约已定版：E-DATA-MISSING 的 http_statuses 为 [422, 503]，503 即计量单价未配置。
 */
export function isConfigMissing(error: unknown): boolean {
  return isApiError(error) && error.code === 'E-DATA-MISSING' && error.status === 503;
}

/** 网络中断 / 超时：status 0 或 fetch 层抛出的 AbortError/TimeoutError。 */
export function isNetworkFailure(error: unknown): boolean {
  if (isApiError(error)) return error.status === 0 || error.code === 'NETWORK';
  const name = (error as Error | undefined)?.name;
  return name === 'AbortError' || name === 'TimeoutError' || name === 'NetworkError';
}

/** 前端等待超时（client 侧等待上限主动抛 TimeoutError）。 */
export function isTimeoutFailure(error: unknown): boolean {
  return (error as Error | undefined)?.name === 'TimeoutError';
}

/** 限流：助手建议每租户每分钟有上限，429 必须与普通失败区分。 */
export function isRateLimited(error: unknown): boolean {
  return isApiError(error) && (error.status === 429 || error.code === 'E-CONN-RATE_LIMIT');
}

/** 资源不存在：契约已定版为 E-RESOURCE-NOTFOUND（404），不再按 status 猜。 */
export function isNotFoundFailure(error: unknown): boolean {
  return isApiError(error) && error.code === 'E-RESOURCE-NOTFOUND';
}

export function errorKind(error: unknown): ErrorKind {
  if (isNetworkFailure(error)) return 'network';
  if (isConfigMissing(error)) return 'config';
  if (isNotFoundFailure(error)) return 'notfound';
  if (!isApiError(error)) return 'generic';
  if (error.status === 401 || error.status === 403) return 'forbidden';
  if (error.status === 409) return 'conflict';
  return 'generic';
}

export const ERROR_HINTS: Record<string, string> = {
  'E-IDEM-CONFLICT': '写接口必须携带 Idempotency-Key；同一键不能复用于不同请求体。',
  'E-AUTHZ-001': '会话缺失/失效或超出授权范围，请重新登录或收窄 scope。',
  'E-AUTHZ-002': '字段范围越界，检查该动作允许写入的字段白名单。',
  'E-POLICY-W3FORBIDDEN': 'W3 或需要更高权限，整单拒绝；连接器零写入。',
  'E-POLICY-LEVEL': '动作未注册或写等级不足，未产生外部写入。',
  'E-POLICY-APPROVAL': '审批未满足：补齐缺失角色或审批意见后再提交。',
  'E-STATE-ILLEGAL': '当前状态不允许该迁移，请刷新工单后重试。',
  'E-VERIFY-MISMATCH': '写入校验或审计链校验不一致，需人工核查。',
  'E-DATA-MISSING': '缺少必要数据（含工单/批次不存在），不会猜测填充。',
  'E-PARSE-FAILED': '入参、游标或金额解析失败，请检查输入。',
  'E-CONN-UNAVAILABLE': '下游连接器不可用，稍后重试或查看连接器健康度。',
};

export function errorHint(error: unknown): string {
  if (!isApiError(error)) return '未识别的错误，请查看浏览器控制台。';
  return ERROR_HINTS[error.code] ?? '请根据错误码与 trace_id 排查。';
}

export function errorTitle(error: unknown): string {
  if (!isApiError(error)) return '请求失败';
  if (error.status === 0) return '网络不可达';
  return `${error.status} · ${error.code}`;
}
