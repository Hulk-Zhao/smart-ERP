import { fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { ApprovalTaskOut } from '../../web/src/api/types';
import { ApprovalsPage } from '../../web/src/pages/ApprovalsPage';
import { approvalTask, ticketDetail } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

const TASKS: ApprovalTaskOut[] = [
  approvalTask({ ticket_id: 'd0000000-0000-4000-8000-000000000101', ticket_no: 'T-101' }),
  approvalTask({ ticket_id: 'd0000000-0000-4000-8000-000000000102', ticket_no: 'T-102' }),
];

let server: TestApi;

beforeEach(async () => {
  server = await startApi((request) => {
    if (request.path === '/v1/auth/session') {
      return {
        body: {
          user_id: REVIEWER_ID,
          tenant_id: TENANT_ID,
          roles: ['reviewer', 'approver'],
          default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8'] },
        },
      };
    }
    if (request.path === '/v1/approvals/pending') return { body: { items: TASKS, next_cursor: null } };
    const detailMatch = /^\/v1\/tickets\/([^/]+)$/.exec(request.path);
    if (detailMatch) {
      const task = TASKS.find((item) => item.ticket_id === detailMatch[1]);
      return { body: ticketDetail({ ticket_id: task?.ticket_id, ticket_no: task?.ticket_no, write_level: 'W2' }) };
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
  });
  resetApiConfig();
  configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false, roles: ['reviewer', 'approver'] });
});

afterEach(async () => {
  resetApiConfig();
  await server.close();
});

describe('审批页筛选空态不保留上一张工单详情', () => {
  it('切到 W3（0 条）时右侧也清空，并说明是筛选结果为空', async () => {
    renderPage(<ApprovalsPage />, { route: '/approvals', path: '/approvals' });

    expect(await screen.findByText('T-101')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '通过' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'W3' }));

    expect(await screen.findByText(/当前筛选「W3」下没有审批/)).toBeInTheDocument();
    // 空态与详情不能同屏：上一张工单的审批按钮必须消失。
    expect(screen.queryByRole('button', { name: '通过' })).toBeNull();
    expect(screen.queryByRole('button', { name: '驳回' })).toBeNull();
  });
});
