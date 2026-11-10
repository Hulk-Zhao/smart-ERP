// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { api } from '../../web/src/api/client';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { ApiError } from '../../web/src/api/errors';
import { problem, startApi, type TestApi } from './helpers/server';

let server: TestApi;

beforeEach(async () => {
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === '/v1/tickets') {
      return {
        body: {
          items: [
            {
              ticket_id: 'd0000000-0000-4000-8000-000000000001',
              ticket_no: 'T-REAL',
              scene_code: 'ag1_reconcile',
              state: '待审批',
              version: 1,
              initiator_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
              business_key: 'BK-REAL',
              org_id: 'ORG-9',
              period: '2026-10',
              systems: ['u8'],
              write_level: 'W1',
              amount: '10.0000',
              touches_fund: false,
              created_at: '2026-11-10T09:00:00Z',
              updated_at: '2026-11-10T09:00:00Z',
            },
          ],
          next_cursor: null,
        },
      };
    }
    if (request.method === 'POST' && request.path === '/v1/tickets/d0000000-0000-4000-8000-000000000001/actions/approve') {
      return problem(403, 'E-POLICY-W3FORBIDDEN', 'W3 写等级禁止自动执行，连接器零写入');
    }
    if (request.method === 'POST' && request.path === '/v1/tickets/d0000000-0000-4000-8000-000000000001/actions/cancel') {
      return {
        body: {
          ticket_id: 'd0000000-0000-4000-8000-000000000001',
          action: 'cancel',
          state: '已终止',
          version: 2,
        },
      };
    }
    if (request.method === 'GET' && request.path === '/v1/audit/export') {
      return {
        text: 'image_id,hash\na1,sha256:x\na2,sha256:y\n',
        headers: {
          'content-disposition': 'attachment; filename="audit-real.csv"',
          'x-content-sha256': 'sha256:export',
        },
      };
    }
    return problem(404, 'E-DATA-MISSING', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'session-token-1',
    mock: false,
    scope: { org_id: 'ORG-9', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  await server.close();
  resetApiConfig();
});

describe('真实 HTTP client（C16）', () => {
  it('GET 自动带 scope 三参数与 X-Session-Token，返回契约分页结构', async () => {
    const page = await api.listTickets({ limit: 5, state: '待审批' });

    expect(page.items[0].ticket_no).toBe('T-REAL');
    expect(page.next_cursor).toBeNull();

    const request = server.requests.find((item) => item.path === '/v1/tickets');
    expect(request?.query.org_id).toEqual(['ORG-9']);
    expect(request?.query.period).toEqual(['2026-10']);
    expect(request?.query.system_ids).toEqual(['u8', 'statement']);
    expect(request?.query.limit).toEqual(['5']);
    expect(request?.headers['x-session-token']).toBe('session-token-1');
  });

  it('写接口自动补 Idempotency-Key 并把 scope 合入 body', async () => {
    const result = await api.ticketAction('d0000000-0000-4000-8000-000000000001', 'cancel', {
      comment: '客户取消',
    });

    expect(result.state).toBe('已终止');
    const request = server.requests.find((item) => item.path.endsWith('/actions/cancel'));
    expect(String(request?.headers['idempotency-key'])).toMatch(/^web-/);
    expect(request?.body).toMatchObject({
      comment: '客户取消',
      scope: { org_id: 'ORG-9', period: '2026-10', system_ids: ['u8', 'statement'] },
    });
  });

  it('problem+json 403 被解析为 ApiError（code/status/trace 保真）', async () => {
    let caught: unknown;
    try {
      await api.ticketAction('d0000000-0000-4000-8000-000000000001', 'approve', { role: 'reviewer' });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(ApiError);
    const error = caught as ApiError;
    expect(error.status).toBe(403);
    expect(error.code).toBe('E-POLICY-W3FORBIDDEN');
    expect(error.traceId).toContain('trace-E-POLICY-W3FORBIDDEN');
    expect(error.retriable).toBe(false);
  });

  it('CSV 导出保留文件名、行数与服务端 sha256', async () => {
    const result = await api.exportAuditCsv('d0000000-0000-4000-8000-000000000001');

    expect(result.filename).toBe('audit-real.csv');
    expect(result.rowCount).toBe(2);
    expect(result.sha256).toBe('sha256:export');
    const request = server.requests.find((item) => item.path === '/v1/audit/export');
    expect(request?.query.ticket_id).toEqual(['d0000000-0000-4000-8000-000000000001']);
  });
});
