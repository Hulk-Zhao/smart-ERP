/**
 * 真实 `/v1` API client（C16 契约）。
 *
 * - 返回类型全部来自 `ApiOperations[...]['response']`（由契约生成），页面不再手写响应类型。
 * - GET 自动带 G-05 scope 三参数；POST 自动把 scope 合入请求体；写接口自动补 Idempotency-Key。
 * - 错误统一转成 `ApiError`（problem+json），页面按 code/status 呈现。
 * - `config.mock === true` 时才走 `mock.ts`，默认关闭。
 */

import { getApiConfig, scopeQuery, type ApiConfig } from './config';
import { ApiError } from './errors';
import { mockSend } from './mock';
import type {
  ActionIn,
  ActionOut,
  ApiOperations,
  ApprovalDecisionIn,
  ApprovalDecisionOut,
  AuditChainOut,
  PageApprovalTaskOut,
  PageAuditImageOut,
  PageReconBatchSummaryOut,
  PageReconDiffOut,
  PageTicketOut,
  MeteringStatementOut,
  ReconBatchOut,
  ReconDatasetIn,
  ReconDiffDetailOut,
  ReconRunIn,
  ReconStatementOut,
  SessionOut,
  SuggestIn,
  SuggestOut,
  TicketDetailOut,
} from './types';

type Operation<K extends keyof ApiOperations> = ApiOperations[K];
type QueryOf<K extends keyof ApiOperations> = Operation<K>['query'] extends never
  ? Record<string, never>
  : Operation<K>['query'];
type ScopeKeys = 'org_id' | 'period' | 'system_ids';

export type ListTicketsParams = Omit<QueryOf<'GET /v1/tickets'>, ScopeKeys>;
export type ListApprovalsParams = Omit<QueryOf<'GET /v1/approvals/pending'>, ScopeKeys>;
export type ListBatchesParams = Omit<QueryOf<'GET /v1/recon/batches'>, ScopeKeys>;
export type ListDiffsParams = Omit<QueryOf<'GET /v1/recon/runs/{batch_id}/diffs'>, ScopeKeys>;
export type ListImagesParams = Omit<QueryOf<'GET /v1/audit/tickets/{ticket_id}/images'>, ScopeKeys>;
export type TicketAction = 'approve' | 'reject' | 'cancel' | 'compensate';
export type ApprovalDecision = 'approve' | 'reject';
export type TicketActionPayload = Omit<ActionIn, 'scope'>;
export type ApprovalPayload = Omit<ApprovalDecisionIn, 'scope'>;
export type ReconRunPayload = Omit<ReconRunIn, 'scope'>;

interface RawResult {
  status: number;
  text: string;
  headers: Headers;
}

interface SendOptions {
  query?: Record<string, unknown>;
  body?: unknown;
  signal?: AbortSignal;
  idempotencyKey?: string;
  /** 会话端点不参与 G-05 业务 scope，不附加 org/period/system_ids。 */
  omitScope?: boolean;
}

function newIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) return `web-${cryptoApi.randomUUID()}`;
  return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildQuery(
  config: ApiConfig,
  query: Record<string, unknown> | undefined,
  omitScope = false,
): URLSearchParams {
  const params = new URLSearchParams();
  if (!omitScope) {
    for (const [key, value] of scopeQuery(config.scope)) {
      params.append(key, value);
    }
  }
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === '') continue;
    if (Array.isArray(value)) {
      value.forEach((item) => params.append(key, String(item)));
    } else {
      params.append(key, String(value));
    }
  }
  return params;
}

function joinUrl(base: string, path: string, query: URLSearchParams): string {
  const normalizedBase = base.replace(/\/+$/, '');
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const search = query.toString();
  return `${normalizedBase}${normalizedPath}${search ? `?${search}` : ''}`;
}

function toApiError(status: number, text: string): ApiError {
  try {
    const parsed = JSON.parse(text) as Record<string, unknown>;
    return new ApiError(parsed, status);
  } catch {
    return new ApiError({ status, detail: text.slice(0, 300) || `HTTP ${status}` }, status);
  }
}

async function sendJson(method: string, path: string, options: SendOptions): Promise<RawResult> {
  const config = getApiConfig();
  const query = buildQuery(config, options.query, options.omitScope);
  const isWrite = method !== 'GET';
  // 会话与助手建议不是 G-05 业务写接口，请求体不带 scope（后端 extra="forbid"）。
  const payload = isWrite
    ? options.omitScope
      ? options.body
      : { ...(options.body as object), scope: config.scope }
    : undefined;

  if (config.mock) {
    const response = await mockSend(method, path, Object.fromEntries(query) as Record<string, unknown>, payload, config);
    const headers = new Headers(response.headers ?? {});
    headers.set('content-type', headers.get('content-type') ?? 'application/json');
    const text = response.text ?? JSON.stringify(response.body ?? null);
    return { status: response.status, text, headers };
  }

  const headers = new Headers({ Accept: 'application/json, application/problem+json' });
  if (config.token) headers.set('X-Session-Token', config.token);
  if (isWrite) {
    headers.set('Content-Type', 'application/json');
    headers.set('Idempotency-Key', options.idempotencyKey ?? newIdempotencyKey());
  }

  let response: Response;
  try {
    response = await fetch(joinUrl(config.baseUrl, path, query), {
      method,
      headers,
      body: isWrite ? JSON.stringify(payload) : undefined,
      signal: options.signal,
    });
  } catch (error) {
    const name = (error as Error)?.name;
    if (name === 'AbortError' || name === 'TimeoutError') throw error;
    throw new ApiError(
      {
        status: 0,
        code: 'NETWORK',
        title: '无法连接 API',
        detail: `请求 ${path} 失败：${(error as Error)?.message ?? '网络错误'}。请检查 baseUrl、后端进程与跨域配置。`,
        retriable: true,
      },
      0,
    );
  }

  return { status: response.status, text: await response.text(), headers: response.headers };
}

/** jsdom 的 File 没有 text()，用 FileReader 兜底；浏览器走原生 text()。 */
async function readFileText(file: File): Promise<string> {
  if (typeof file.text === 'function') return file.text();
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('读取对账单文件失败'));
    reader.readAsText(file);
  });
}

/**
 * 对账单 CSV 的 multipart 上传。手写 body 而不是 FormData：jsdom 的 FormData
 * 与 Node fetch 跨 realm 不互认，会退化成 "[object FormData]"；CSV 按 UTF-8
 * 文本拼装既能跑真实链路，也不依赖浏览器实现细节。
 */
async function sendStatementMultipart(file: File, dataset: ReconDatasetIn): Promise<RawResult> {
  const config = getApiConfig();

  if (config.mock) {
    const response = await mockSend(
      'POST',
      RECON_STATEMENTS_PATH,
      {},
      { file_name: file.name, dataset },
      config,
    );
    return {
      status: response.status,
      text: response.text ?? JSON.stringify(response.body ?? null),
      headers: new Headers(response.headers ?? {}),
    };
  }

  const boundary = `----erp-recon-${Date.now().toString(16)}`;
  const safeName = file.name.replace(/"/g, '');
  const csv = await readFileText(file);
  const payload = [
    `--${boundary}`,
    `Content-Disposition: form-data; name="file"; filename="${safeName}"`,
    'Content-Type: text/csv; charset=utf-8',
    '',
    csv,
    `--${boundary}`,
    'Content-Disposition: form-data; name="dataset"',
    '',
    JSON.stringify(dataset),
    `--${boundary}--`,
    '',
  ].join('\r\n');

  const headers = new Headers({
    Accept: 'application/json, application/problem+json',
    'Content-Type': `multipart/form-data; boundary=${boundary}`,
  });
  if (config.token) headers.set('X-Session-Token', config.token);

  let response: Response;
  try {
    response = await fetch(joinUrl(config.baseUrl, RECON_STATEMENTS_PATH, new URLSearchParams()), {
      method: 'POST',
      headers,
      body: payload,
    });
  } catch (error) {
    throw new ApiError(
      {
        status: 0,
        code: 'NETWORK',
        title: '无法连接 API',
        detail: `上传对账单失败：${(error as Error)?.message ?? '网络错误'}。请检查 baseUrl、后端进程与跨域配置。`,
        retriable: true,
      },
      0,
    );
  }
  return { status: response.status, text: await response.text(), headers: response.headers };
}

function parseJson<T>(result: RawResult): T {
  if (result.status >= 400) throw toApiError(result.status, result.text);
  if (!result.text) return undefined as T;
  try {
    return JSON.parse(result.text) as T;
  } catch {
    throw new ApiError(
      { status: result.status, code: 'E-PARSE-FAILED', detail: '响应不是合法 JSON' },
      result.status,
    );
  }
}

async function request<K extends keyof ApiOperations>(
  method: string,
  path: string,
  options: SendOptions = {},
): Promise<Operation<K>['response']> {
  const result = await sendJson(method, path, options);
  return parseJson<Operation<K>['response']>(result);
}

export function ticketPath(ticketId: string, suffix = ''): string {
  return `/v1/tickets/${encodeURIComponent(ticketId)}${suffix}`;
}

/**
 * 无路径参数的读接口路径集中在这里，作为 client 侧唯一来源。
 * mock 仍是另一份分段匹配实现，靠 `tests/web/api-mock-routes.test.ts` 的守卫用例
 * 保证「client 改路径而 mock 不认」时测试立刻变红。
 */
export const SESSION_PATH = '/v1/auth/session';
export const ASSISTANT_SUGGEST_PATH = '/v1/assistant/suggest';
export const RECON_BATCHES_PATH = '/v1/recon/batches';
export const RECON_STATEMENTS_PATH = '/v1/recon/statements';
export const TICKETS_PATH = '/v1/tickets';
export const PENDING_APPROVALS_PATH = '/v1/approvals/pending';

export function batchPath(batchId: string, suffix = ''): string {
  return `/v1/recon/runs/${encodeURIComponent(batchId)}${suffix}`;
}

export function meteringPath(batchId: string, suffix = ''): string {
  return `/v1/metering/batches/${encodeURIComponent(batchId)}${suffix}`;
}

export function auditPath(ticketId: string, suffix = ''): string {
  return `/v1/audit/tickets/${encodeURIComponent(ticketId)}${suffix}`;
}

export interface AuditExport {
  csv: string;
  filename: string;
  sha256?: string;
  rowCount: number;
}

export interface MeteringExport {
  csv: string;
  filename: string;
  /** 服务端 X-Content-SHA256，供用户用 sha256sum 自行核验下载文件。 */
  sha256?: string;
  rowCount: number;
}

export interface SuggestOptions {
  /** 前端等待上限；后端 LLM 硬超时 8s，这里留出网络余量。 */
  timeoutMs?: number;
}

export const api = {
  /** 当前会话身份：前端唯一的身份来源，替代 VITE_SESSION_ROLES / VITE_VIEWER_ID。 */
  getSession(): Promise<SessionOut> {
    return request<'GET /v1/auth/session'>('GET', SESSION_PATH, { omitScope: true });
  },

  /**
   * 助手下一步建议（`POST /v1/assistant/suggest`）。
   *
   * 降级时接口仍返回 200 且 `source="rule"`，调用方必须展示 `fallback_reason`，
   * 不能靠 HTTP 状态码判断真假，也不能在 rule 时展示模型名。
   * 请求体不带 G-05 scope：该端点入参只接受 `{ticket_id, kind}`。
   */
  async suggestNextAction(ticketId: string, options: SuggestOptions = {}): Promise<SuggestOut> {
    const { timeoutMs = 12_000 } = options;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // 用 Promise.race 做前端等待上限，而不是把 AbortSignal 传给 fetch：
    // 不同运行环境的 AbortSignal 实现可能不互通（如 jsdom 与 undici），
    // 浏览器端多等一个已发出的请求也无害。
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new DOMException('助手响应超时', 'TimeoutError')), timeoutMs);
    });
    try {
      return await Promise.race([
        request<'POST /v1/assistant/suggest'>('POST', ASSISTANT_SUGGEST_PATH, {
          body: { ticket_id: ticketId, kind: 'next_action' } satisfies SuggestIn,
          omitScope: true,
        }),
        timeout,
      ]);
    } finally {
      clearTimeout(timer);
    }
  },

  listTickets(params: ListTicketsParams = {}): Promise<PageTicketOut> {
    return request<'GET /v1/tickets'>('GET', TICKETS_PATH, { query: params as Record<string, unknown> });
  },

  getTicket(ticketId: string): Promise<TicketDetailOut> {
    return request<'GET /v1/tickets/{ticket_id}'>('GET', ticketPath(ticketId));
  },

  ticketAction(ticketId: string, action: TicketAction, payload: TicketActionPayload): Promise<ActionOut> {
    // 路径与类型键都由 action 推出，避免把四种动作都套在 approve 的类型上。
    const key = `POST /v1/tickets/{ticket_id}/actions/${action}` as const;
    return request<typeof key>('POST', ticketPath(ticketId, `/actions/${action}`), {
      body: payload,
    });
  },

  listPendingApprovals(params: ListApprovalsParams = {}): Promise<PageApprovalTaskOut> {
    return request<'GET /v1/approvals/pending'>('GET', PENDING_APPROVALS_PATH, {
      query: params as Record<string, unknown>,
    });
  },

  decideApproval(
    ticketId: string,
    decision: ApprovalDecision,
    payload: ApprovalPayload,
  ): Promise<ApprovalDecisionOut> {
    const key = `POST /v1/approvals/{ticket_id}/${decision}` as const;
    return request<typeof key>(
      'POST',
      `/v1/approvals/${encodeURIComponent(ticketId)}/${decision}`,
      { body: payload },
    );
  },

  createReconRun(payload: ReconRunPayload): Promise<ReconBatchOut> {
    return request<'POST /v1/recon/runs'>('POST', '/v1/recon/runs', { body: payload });
  },

  /**
   * 上传供应商对账单 CSV，登记成对账数据集。`dataset` 是我方数据（来自 fixtures，
   * 不由上传文件反填）；422 的 detail 会带真实文件行号，调用方原样展示。
   */
  async uploadReconStatement(file: File, dataset: ReconDatasetIn): Promise<ReconStatementOut> {
    return parseJson<ReconStatementOut>(await sendStatementMultipart(file, dataset));
  },

  /**
   * 批次列表（键集分页）。`line_count/diff_count/diff_amount` 对没有投影的旧批次为
   * `null`，调用方必须原样渲染成「—」，不能折算成 0；越权是 403 且没有 `items`，
   * 会在 parseJson 里抛 ApiError，不会被当成空列表。
   */
  listReconBatches(params: ListBatchesParams = {}): Promise<PageReconBatchSummaryOut> {
    return request<'GET /v1/recon/batches'>('GET', RECON_BATCHES_PATH, {
      query: params as Record<string, unknown>,
    });
  },

  getReconRun(batchId: string): Promise<ReconBatchOut> {
    return request<'GET /v1/recon/runs/{batch_id}'>('GET', batchPath(batchId));
  },

  listReconDiffs(batchId: string, params: ListDiffsParams = {}): Promise<PageReconDiffOut> {
    return request<'GET /v1/recon/runs/{batch_id}/diffs'>('GET', batchPath(batchId, '/diffs'), {
      query: params as Record<string, unknown>,
    });
  },

  getReconDiff(batchId: string, diffId: string): Promise<ReconDiffDetailOut> {
    return request<'GET /v1/recon/runs/{batch_id}/diffs/{diff_id}'>(
      'GET',
      batchPath(batchId, `/diffs/${encodeURIComponent(diffId)}`),
    );
  },

  getMeteringStatement(batchId: string): Promise<MeteringStatementOut> {
    return request<'GET /v1/metering/batches/{batch_id}/statement'>(
      'GET',
      meteringPath(batchId, '/statement'),
    );
  },

  async exportMeteringCsv(batchId: string): Promise<MeteringExport> {
    const result = await sendJson('GET', meteringPath(batchId, '/statement'), {
      query: { format: 'csv' },
    });
    if (result.status >= 400) throw toApiError(result.status, result.text);
    const disposition = result.headers.get('content-disposition') ?? '';
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    const filename = match?.[1] ?? `metering-${batchId}.csv`;
    const csv = result.text;
    const rowCount = Math.max(0, csv.replace(/^\ufeff/, '').trim().split('\n').length - 1);
    return {
      csv,
      filename,
      sha256: result.headers.get('x-content-sha256') ?? undefined,
      rowCount,
    };
  },

  listAuditImages(ticketId: string, params: ListImagesParams = {}): Promise<PageAuditImageOut> {
    return request<'GET /v1/audit/tickets/{ticket_id}/images'>(
      'GET',
      auditPath(ticketId, '/images'),
      { query: params as Record<string, unknown> },
    );
  },

  getAuditChain(ticketId: string): Promise<AuditChainOut> {
    return request<'GET /v1/audit/tickets/{ticket_id}/chain'>(
      'GET',
      auditPath(ticketId, '/chain'),
    );
  },

  async exportAuditCsv(ticketId: string, system?: string): Promise<AuditExport> {
    const result = await sendJson('GET', '/v1/audit/export', { query: { ticket_id: ticketId, system } });
    if (result.status >= 400) throw toApiError(result.status, result.text);
    const disposition = result.headers.get('content-disposition') ?? '';
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    const filename = match?.[1] ?? `audit-${ticketId}.csv`;
    const csv = result.text;
    const rowCount = Math.max(0, csv.trim().split('\n').length - 1);
    return {
      csv,
      filename,
      sha256: result.headers.get('x-content-sha256') ?? undefined,
      rowCount,
    };
  },
};

/** 触发浏览器下载；CSV 由 fetch 取得，避免 <a downloads> 无法携带会话令牌。 */
export function downloadText(filename: string, text: string, mime = 'text/csv;charset=utf-8'): string {
  // 服务端 CSV 可能自带 UTF-8 BOM；重复添加会让本地文件摘要与服务端 X-Content-SHA256 不一致。
  const body = text.startsWith('\ufeff') ? text : `\ufeff${text}`;
  const blob = new Blob([body], { type: mime });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  // 返回实际写入文件的文本（含 BOM 决策），供调用方本地重算摘要。
  return body;
}
