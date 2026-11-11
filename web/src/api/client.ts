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
  PageReconDiffOut,
  PageTicketOut,
  MeteringStatementOut,
  ReconBatchOut,
  ReconDiffDetailOut,
  ReconRunIn,
  TicketDetailOut,
} from './types';

type Operation<K extends keyof ApiOperations> = ApiOperations[K];
type QueryOf<K extends keyof ApiOperations> = Operation<K>['query'] extends never
  ? Record<string, never>
  : Operation<K>['query'];
type ScopeKeys = 'org_id' | 'period' | 'system_ids';

export type ListTicketsParams = Omit<QueryOf<'GET /v1/tickets'>, ScopeKeys>;
export type ListApprovalsParams = Omit<QueryOf<'GET /v1/approvals/pending'>, ScopeKeys>;
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
}

function newIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) return `web-${cryptoApi.randomUUID()}`;
  return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildQuery(config: ApiConfig, query: Record<string, unknown> | undefined): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of scopeQuery(config.scope)) {
    params.append(key, value);
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
  const query = buildQuery(config, options.query);
  const isWrite = method !== 'GET';
  const payload = isWrite ? { ...(options.body as object), scope: config.scope } : undefined;

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
    if ((error as Error)?.name === 'AbortError') throw error;
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

export const api = {
  listTickets(params: ListTicketsParams = {}): Promise<PageTicketOut> {
    return request<'GET /v1/tickets'>('GET', '/v1/tickets', { query: params as Record<string, unknown> });
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
    return request<'GET /v1/approvals/pending'>('GET', '/v1/approvals/pending', {
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
