import { describe, expect, it } from 'vitest';
import type { ApiConfig } from '../../web/src/api/config';
import { auditPath, batchPath, meteringPath, ticketPath } from '../../web/src/api/client';
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
  roles: ['reviewer'],
  viewerName: '测试用户',
  viewerId: '',
  mock: true,
};

const ticketId = 'd0000000-0000-4000-8000-000000000002';
const batchId = 'e0000000-0000-4000-8000-000000000001';

const routes: Array<{ name: string; path: string; query?: Record<string, unknown> }> = [
  { name: '工单详情', path: ticketPath(ticketId) },
  { name: '审批待办', path: '/v1/approvals/pending' },
  { name: '批次详情', path: batchPath(batchId) },
  { name: '批次差异列表', path: batchPath(batchId, '/diffs') },
  { name: '差异详情', path: batchPath(batchId, '/diffs/d0') },
  { name: '计量表', path: meteringPath(batchId, '/statement') },
  { name: '审计影像', path: auditPath(ticketId, '/images') },
  { name: '审计链', path: auditPath(ticketId, '/chain') },
  { name: '审计导出', path: '/v1/audit/export', query: { ticket_id: ticketId } },
];

describe('mock 路由与 client 路径同源', () => {
  it.each(routes)('$name $path 命中 mock 而非 404', async ({ path, query }) => {
    const response = await mockSend('GET', path, query ?? {}, undefined, config);
    expect(response.status).toBe(200);
  });
});
