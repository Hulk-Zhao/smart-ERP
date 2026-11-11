import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { ReconBatchSummaryOut } from '../../web/src/api/types';
import { ReconPage } from '../../web/src/pages/ReconPage';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

const PROJECTED: ReconBatchSummaryOut = {
  batch_id: 'e0000000-0000-4000-8000-000000000001',
  status: 'reconciled',
  line_count: 192,
  diff_count: 10,
  diff_amount: '113242.8300',
  created_at: '2026-11-10T10:00:00Z',
};

/** 迁移前旧批次：三个汇总字段为 null，必须渲染成「—」。 */
const LEGACY: ReconBatchSummaryOut = {
  batch_id: 'e0000000-0000-4000-8000-000000000002',
  status: 'completed',
  line_count: null,
  diff_count: null,
  diff_amount: null,
  created_at: '2026-09-30T16:20:00Z',
};

type BatchesReply = 'projected' | 'paged' | 'legacy' | 'empty' | 'forbidden' | 'unauthorized';

let server: TestApi;
let reply: BatchesReply = 'projected';

function sessionReply() {
  if (reply === 'unauthorized') return problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）');
  return {
    body: {
      user_id: REVIEWER_ID,
      tenant_id: TENANT_ID,
      roles: ['reviewer'],
      default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
    },
  };
}

function batchesReply(query: Record<string, string[]> = {}) {
  switch (reply) {
    case 'projected':
      return { body: { items: [PROJECTED, LEGACY], next_cursor: null } };
    case 'paged':
      // 真键集分页：第一页带 cursor，第二页必须按该 cursor 取。
      return query.cursor?.[0] === 'cursor-2'
        ? { body: { items: [LEGACY], next_cursor: null } }
        : { body: { items: [PROJECTED], next_cursor: 'cursor-2' } };
    case 'legacy':
      return { body: { items: [LEGACY], next_cursor: null } };
    case 'empty':
      return { body: { items: [], next_cursor: null } };
    case 'forbidden':
      // 越权：403 + problem 体，响应里没有 items，不能当空数组。
      return problem(403, 'E-AUTHZ-001', '缺少所需角色或 scope');
    case 'unauthorized':
      return problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）');
  }
}

beforeEach(async () => {
  reply = 'projected';
  server = await startApi((request) => {
    if (request.path === '/v1/auth/session') return sessionReply();
    if (request.path === '/v1/recon/batches') return batchesReply(request.query);
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
  });
  resetApiConfig();
  configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false });
});

afterEach(async () => {
  resetApiConfig();
  await server.close();
});

function batchRows(): HTMLElement[] {
  return screen.getAllByTestId('batch-row');
}

describe('最近批次来自 /v1/recon/batches', () => {
  it('渲染真实列表，null 汇总渲染成「—」而不是 0', async () => {
    renderPage(<ReconPage />, { route: '/recon', path: '/recon' });

    await waitFor(() => expect(batchRows()).toHaveLength(2));

    const [projectedRow, legacyRow] = batchRows();
    expect(within(projectedRow).getByText('192')).toBeTruthy();
    expect(within(projectedRow).getByText('113,242.83 CNY')).toBeTruthy();
    expect(within(projectedRow).queryAllByTestId('batch-metric-missing')).toHaveLength(0);

    const missing = within(legacyRow).getAllByTestId('batch-metric-missing');
    expect(missing).toHaveLength(3);
    missing.forEach((cell) => expect(cell.textContent).toBe('—'));
    // 旧批次的行里不该出现被折算的 0。
    expect(within(legacyRow).queryByText('0')).toBeNull();
    expect(within(legacyRow).getByText('未投影（非 0）')).toBeTruthy();
  });

  it('空列表走空态，文案说明是服务端确实没有批次', async () => {
    reply = 'empty';
    renderPage(<ReconPage />, { route: '/recon', path: '/recon' });

    const empty = await screen.findByTestId('empty-state');
    expect(empty.getAttribute('data-variant')).toBe('not-run');
    expect(empty.textContent).toContain('服务端还没有本租户的对账批次');
    expect(screen.queryByTestId('batch-row')).toBeNull();
  });

  it('403 渲染无权限错误面板，且不伪装成空态', async () => {
    reply = 'forbidden';
    renderPage(<ReconPage />, { route: '/recon', path: '/recon' });

    const panel = await screen.findByTestId('error-panel');
    expect(panel.getAttribute('data-kind')).toBe('forbidden');
    expect(panel.textContent).toContain('缺少所需角色或 scope');
    expect(screen.queryByTestId('empty-state')).toBeNull();
    expect(screen.queryByTestId('batch-row')).toBeNull();
  });

  it('会话 401 时显示演示会话未启用，不请求批次列表', async () => {
    reply = 'unauthorized';
    renderPage(<ReconPage />, { route: '/recon', path: '/recon' });

    const notices = await screen.findAllByTestId('session-notice');
    expect(notices[0].textContent).toContain('当前未启用演示会话');
    expect(server.requests.some((request) => request.path === '/v1/recon/batches')).toBe(false);
  });

  it('列表带上业务 scope，键集分页参数透传', async () => {
    renderPage(<ReconPage />, { route: '/recon', path: '/recon' });
    await waitFor(() => expect(batchRows()).toHaveLength(2));

    const batchRequest = server.requests.find((request) => request.path === '/v1/recon/batches');
    expect(batchRequest?.query.limit).toEqual(['10']);
    expect(batchRequest?.query.org_id).toEqual(['ORG-1']);
    expect(batchRequest?.query.period).toEqual(['2026-10']);
  });

  it('加载更多按 next_cursor 取下一页并追加，而不是重复第一页', async () => {
    reply = 'paged';
    renderPage(<ReconPage />, { route: '/recon', path: '/recon' });

    await waitFor(() => expect(batchRows()).toHaveLength(1));
    fireEvent.click(screen.getByRole('button', { name: '加载更多' }));

    await waitFor(() => expect(batchRows()).toHaveLength(2));
    // 第二页必须用第一页返回的 cursor；旧实现若忽略 cursor 会再次拿到同一页。
    const paged = server.requests.filter((request) => request.path === '/v1/recon/batches');
    expect(paged).toHaveLength(2);
    expect(paged[1].query.cursor).toEqual(['cursor-2']);
    expect(within(batchRows()[1]).getAllByTestId('batch-metric-missing')).toHaveLength(3);
  });
});
