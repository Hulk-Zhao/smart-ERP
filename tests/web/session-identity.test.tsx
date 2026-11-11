import { screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { OverviewPage } from '../../web/src/pages/OverviewPage';
import { approvalTask, ticketOut } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

function kpi(label: string): HTMLElement {
  const node = document.querySelector(`[data-kpi="${label}"]`);
  if (!(node instanceof HTMLElement)) throw new Error(`KPI ${label} 未渲染`);
  return node;
}

describe('会话身份驱动工作台数字（mock）', () => {
  beforeEach(() => {
    resetApiConfig();
    configureApi({ mock: true, roles: [], viewerId: '' });
  });

  afterEach(() => {
    resetApiConfig();
  });

  it('会话返回后待办与金额不再为 0', async () => {
    renderPage(<OverviewPage />, { route: '/', path: '/' });

    // mock 会话 roles = reviewer / approver，工作台据此请求 /v1/approvals/pending。
    await waitFor(() => expect(within(kpi('待你决策')).getByText('8')).toBeTruthy());
    expect(kpi('涉及资金').textContent).toContain('511,115.50');
    expect(kpi('涉及资金').textContent).not.toContain('未启用演示会话');
  });
});

describe('演示会话关闭时的降级（401）', () => {
  let server: TestApi;

  beforeEach(async () => {
    server = await startApi((request) => {
      if (request.path === '/v1/auth/session') {
        return problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）');
      }
      return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
    });
    resetApiConfig();
    configureApi({ baseUrl: server.baseUrl, token: 'stale-token', mock: false });
  });

  afterEach(async () => {
    resetApiConfig();
    await server.close();
  });

  it('显式提示「当前未启用演示会话」，且不发审批/工单查询', async () => {
    renderPage(<OverviewPage />, { route: '/', path: '/' });

    const notices = await screen.findAllByTestId('session-notice');
    // 等 401 真正落地（错误码渲染进提示），而不是停在「获取中」的骨架态。
    await waitFor(() => expect(notices[0].textContent).toContain('E-AUTHZ-001'));
    expect(notices[0].textContent).toContain('当前未启用演示会话');
    expect(within(kpi('待你决策')).getByText('—')).toBeTruthy();
    expect(within(kpi('涉及资金')).getByText('—')).toBeTruthy();

    // 身份没拿到之前，不能把 401 当成「没有待办」去查。
    const paths = server.requests.map((request) => request.path);
    expect(paths).toContain('/v1/auth/session');
    expect(paths).not.toContain('/v1/approvals/pending');
    expect(paths).not.toContain('/v1/tickets');
  });
});

describe('会话返回后才请求审批待办', () => {
  let server: TestApi;

  beforeEach(async () => {
    server = await startApi((request) => {
      if (request.path === '/v1/auth/session') {
        return { body: { user_id: REVIEWER_ID, tenant_id: TENANT_ID, roles: ['reviewer', 'approver'] } };
      }
      if (request.path === '/v1/approvals/pending') {
        return { body: { items: [approvalTask()], next_cursor: null } };
      }
      if (request.path === '/v1/tickets') {
        return {
          body: {
            items: [ticketOut({ ticket_id: 'd0000000-0000-4000-8000-000000000009', ticket_no: 'T-9', state: '待审批' })],
            next_cursor: null,
          },
        };
      }
      return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
    });
    resetApiConfig();
    configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false });
  });

  afterEach(async () => {
    resetApiConfig();
    await server.close();
  });

  it('先取会话再取待办，KPI 用真实响应聚合', async () => {
    renderPage(<OverviewPage />, { route: '/', path: '/' });

    await waitFor(() => expect(within(kpi('待你决策')).getByText('1')).toBeTruthy());
    expect(kpi('涉及资金').textContent).toContain('200,000.00');

    const sessionIndex = server.requests.findIndex((request) => request.path === '/v1/auth/session');
    const pendingIndex = server.requests.findIndex((request) => request.path === '/v1/approvals/pending');
    expect(sessionIndex).toBeGreaterThanOrEqual(0);
    expect(pendingIndex).toBeGreaterThan(sessionIndex);

    const pending = server.requests[pendingIndex];
    expect(pending.headers['x-session-token']).toBe('demo-token');
    // 会话端点不参与业务 scope，不额外附加 org/period/system_ids。
    expect(server.requests[sessionIndex].query.org_id).toBeUndefined();
  });
});
