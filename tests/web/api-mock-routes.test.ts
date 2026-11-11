import { describe, expect, it } from 'vitest';
import type { ApiConfig } from '../../web/src/api/config';
import {
  ASSISTANT_SUGGEST_PATH,
  auditPath,
  batchPath,
  meteringPath,
  PENDING_APPROVALS_PATH,
  RECON_BATCHES_PATH,
  SESSION_PATH,
  TICKETS_PATH,
  ticketPath,
} from '../../web/src/api/client';
import type { SessionOut, SuggestOut } from '../../web/src/api/types';
import { mockSend } from '../../web/src/api/mock';

/**
 * 防漂移守护：mock 用分段字符串匹配路径，client 用模板拼路径，两者是两份定义。
 * 这里只允许通过 client 的路径构造器生成路径——client 改路径时本用例会立刻红，
 * 不会再出现「详情页证据区一直 E-DATA-MISSING，但单测全绿」。
 */
const config: ApiConfig = {
  baseUrl: '/erp/api',
  token: '',
  scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  scopeSource: 'session',
  roles: ['reviewer'],
  viewerName: '测试用户',
  viewerId: '',
  mock: true,
};

const ticketId = 'd0000000-0000-4000-8000-000000000002';
const batchId = 'e0000000-0000-4000-8000-000000000001';

const routes: Array<{
  name: string;
  method?: string;
  path: string;
  query?: Record<string, unknown>;
  body?: unknown;
}> = [
  { name: '会话身份', path: SESSION_PATH },
  {
    name: '助手建议',
    method: 'POST',
    path: ASSISTANT_SUGGEST_PATH,
    body: { ticket_id: ticketId, kind: 'next_action' },
  },
  { name: '工单列表', path: TICKETS_PATH, query: { limit: 5 } },
  { name: '工单详情', path: ticketPath(ticketId) },
  { name: '审批待办', path: PENDING_APPROVALS_PATH },
  { name: '批次列表', path: RECON_BATCHES_PATH, query: { limit: 10 } },
  { name: '批次详情', path: batchPath(batchId) },
  { name: '批次差异列表', path: batchPath(batchId, '/diffs') },
  { name: '差异详情', path: batchPath(batchId, '/diffs/d0') },
  { name: '计量表', path: meteringPath(batchId, '/statement') },
  { name: '审计影像', path: auditPath(ticketId, '/images') },
  { name: '审计链', path: auditPath(ticketId, '/chain') },
  { name: '审计导出', path: '/v1/audit/export', query: { ticket_id: ticketId } },
];

describe('mock 路由与 client 路径同源', () => {
  it.each(routes)('$name $method $path 命中 mock 而非 404', async ({ path, query, method, body }) => {
    const response = await mockSend(method ?? 'GET', path, query ?? {}, body, config);
    expect(response.status).toBe(200);
  });
});

describe('mock 助手建议覆盖 llm / rule 两种来源', () => {
  const ask = (id: string) =>
    mockSend('POST', ASSISTANT_SUGGEST_PATH, {}, { ticket_id: id, kind: 'next_action' }, config);

  it('普通工单返回 llm + 模型名', async () => {
    const response = await ask(ticketId);
    const body = response.body as SuggestOut;
    expect(body.source).toBe('llm');
    expect(body.model).toBe('mock-assistant-v0');
    expect(body.fallback_reason ?? null).toBeNull();
  });

  it('W3 工单返回 rule + fallback_reason，供本地看降级路径', async () => {
    const response = await ask('d0000000-0000-4000-8000-000000000001');
    const body = response.body as SuggestOut;
    expect(body.source).toBe('rule');
    expect(body.fallback_reason).toBe('llm_not_configured');
    expect(body.reasons.length).toBeGreaterThan(0);
  });
});

describe('mock 批次列表与真实契约的边界一致', () => {
  it('无投影旧批次返回 null，不是 0', async () => {
    const response = await mockSend('GET', RECON_BATCHES_PATH, { limit: 10 }, undefined, config);
    const body = response.body as { items: Array<Record<string, unknown>> };
    const legacy = body.items.find((item) => item.status === 'completed');
    expect(legacy).toBeTruthy();
    expect(legacy?.line_count).toBeNull();
    expect(legacy?.diff_count).toBeNull();
    expect(legacy?.diff_amount).toBeNull();

    const projected = body.items.find((item) => item.status === 'reconciled');
    expect(typeof projected?.line_count).toBe('number');
  });
});

describe('mock 会话与真实契约的 default_scope 一致', () => {
  it('三条分支里的「齐全」：org_id / period / system_ids 都由会话带出', async () => {
    const response = await mockSend('GET', SESSION_PATH, {}, undefined, config);
    expect(response.status).toBe(200);
    const body = response.body as SessionOut;
    expect(body.default_scope).toEqual({
      org_id: 'ORG-1',
      period: '2026-10',
      system_ids: ['u8', 'statement'],
    });
  });
});
