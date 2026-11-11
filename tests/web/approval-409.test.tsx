import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { ApprovalsPage } from '../../web/src/pages/ApprovalsPage';
import { approvalTask, DUAL_TICKET_ID, OTHER_APPROVER_ID, ticketDetail } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const SECRET = 'SECRET-REVIEWER-OPINION-发起人不可见';

let server: TestApi;
let approveSucceeds = false;

beforeEach(async () => {
  approveSucceeds = false;
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === '/v1/approvals/pending') {
      return { body: { items: [approvalTask()], next_cursor: null } };
    }
    if (request.method === 'GET' && request.path === `/v1/tickets/${DUAL_TICKET_ID}`) {
      return {
        body: ticketDetail({
          ticket_id: DUAL_TICKET_ID,
          ticket_no: 'T-DUAL',
          business_key: 'BK-DUAL',
          write_level: 'W2',
          amount: '200000.0000',
          touches_fund: true,
          steps: [],
          audit_image_count: 0,
          approval_required_roles: ['reviewer', 'approver'],
          approval_missing_roles: ['approver'],
          execution_required_roles: ['reviewer', 'approver'],
          execution_missing_roles: ['approver'],
          approvals: [
            {
              role: 'reviewer',
              approver_id: OTHER_APPROVER_ID,
              decision: 'approved',
              comment: SECRET,
              decided_at: '2026-11-10T09:25:00Z',
            },
          ],
        }),
      };
    }
    if (request.method === 'POST' && request.path === `/v1/approvals/${DUAL_TICKET_ID}/approve`) {
      if (approveSucceeds) {
        return {
          body: {
            ticket_id: DUAL_TICKET_ID,
            role: 'approver',
            decision: 'approved',
            decided_at: '2026-11-10T09:30:00Z',
            satisfied: true,
            required_roles: ['reviewer', 'approver'],
            missing_roles: [],
            ticket_state: '待审批',
          },
        };
      }
      return problem(409, 'E-POLICY-APPROVAL', '审批未满足：还需 reviewer 审批', {
        approval_required_roles: ['reviewer', 'approver'],
        approval_missing_roles: ['reviewer'],
      });
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-dual',
    mock: false,
    roles: ['approver'],
    // 即使当前用户 ID 与 reviewer 的记录一致，角色不同也不得看到 reviewer 的意见正文
    viewerId: OTHER_APPROVER_ID,
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  await server.close();
  resetApiConfig();
});

describe('审批页：双人意见脱敏 + 409 审批未满足', () => {
  it('其他审批人的意见正文与审批人 ID 不出现在界面', async () => {
    renderPage(<ApprovalsPage />, { route: '/approvals', path: '/approvals' });

    expect(await screen.findByText('双人复核意见')).toBeInTheDocument();
    expect(screen.queryByText(SECRET)).toBeNull();
    expect(screen.getByText(/审批人 ID 与意见正文均不展示/)).toBeInTheDocument();
    expect(screen.getAllByText('已提交').length).toBeGreaterThan(0);
    expect(screen.getAllByText('待提交').length).toBeGreaterThan(0);
  });

  it('审批未满足：界面呈现 409 / E-POLICY-APPROVAL / 缺失角色', async () => {
    renderPage(<ApprovalsPage />, { route: '/approvals', path: '/approvals' });

    await screen.findByText('双人复核意见');
    fireEvent.click(screen.getByRole('button', { name: '通过' }));

    await waitFor(() => {
      expect(screen.getByText(/409 · E-POLICY-APPROVAL/)).toBeInTheDocument();
    });
    expect(screen.getByText('审批尚缺角色：')).toBeInTheDocument();
    expect(screen.getAllByText('复核人').length).toBeGreaterThan(0);
    expect(screen.getByText(/审批未满足：还需 reviewer 审批/)).toBeInTheDocument();

    const request = server.requests.find((item) => item.path.endsWith('/approve'));
    expect(request?.body).toMatchObject({
      role: 'approver',
      scope: { org_id: 'ORG-1', period: '2026-10' },
    });
  });

  it('通过后给出「查看执行步骤 →」，可直接跳到工单详情看写入步骤', async () => {
    approveSucceeds = true;
    renderPage(<ApprovalsPage />, { route: '/approvals', path: '/approvals' });

    await screen.findByText('双人复核意见');
    fireEvent.click(screen.getByRole('button', { name: '通过' }));

    const link = await screen.findByRole('link', { name: '查看执行步骤 →' });
    expect(link).toHaveAttribute('href', `/tickets/${DUAL_TICKET_ID}`);
  });
});
