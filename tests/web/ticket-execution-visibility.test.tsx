import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { ActionOut, TicketStepOut } from '../../web/src/api/types';
import { TicketDetailPage } from '../../web/src/pages/TicketDetailPage';
import { auditChain, auditImage, ticketDetail, W3_TICKET_ID } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

/** 审批回执里的写入步骤：页面必须能看出每一步写到哪个系统/对象、幂等键是什么。 */
const EXECUTED_STEPS: TicketStepOut[] = [
  {
    step_no: 1,
    step_id: 'f0000000-0000-4000-8000-000000000021',
    action_key: 'u8.po.create_draft',
    system: 'u8',
    object_type: 'purchase_order',
    object_id: 'PO-2026-0001',
    state: 'succeeded',
    level: 'W1',
    idem_key: 'idem-exec-aaa',
    executed_at: '2026-11-11T10:00:00Z',
  },
  {
    step_no: 2,
    step_id: 'f0000000-0000-4000-8000-000000000022',
    action_key: 'u8.po.submit',
    system: 'u8',
    object_type: 'purchase_order',
    object_id: 'PO-2026-0001',
    state: 'succeeded',
    level: 'W1',
    idem_key: 'idem-exec-bbb',
    executed_at: '2026-11-11T10:00:01Z',
  },
];

const APPROVE_OK: ActionOut = {
  ticket_id: W3_TICKET_ID,
  action: 'approve',
  state: '执行中',
  version: 2,
  required_roles: [],
  missing_roles: [],
  executed_steps: EXECUTED_STEPS,
  manual_tasks: [],
};

let server: TestApi;
let approveReply: () => ReturnType<typeof problem> | { body: ActionOut };

beforeEach(async () => {
  approveReply = () => ({ body: APPROVE_OK });
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === '/v1/auth/session') {
      return {
        body: {
          user_id: 'dddddddd-0000-4000-8000-000000000000',
          tenant_id: 'T-1',
          roles: ['reviewer', 'approver', 'cfo'],
          default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
        },
      };
    }
    if (request.method === 'GET' && request.path === `/v1/tickets/${W3_TICKET_ID}`) {
      // W1 + 待审批：可审批，正常执行写路径。
      return { body: ticketDetail({ state: '待审批', write_level: 'W1' }) };
    }
    if (request.method === 'GET' && request.path === `/v1/audit/tickets/${W3_TICKET_ID}/images`) {
      return { body: { items: [auditImage()], next_cursor: null } };
    }
    if (request.method === 'GET' && request.path === `/v1/audit/tickets/${W3_TICKET_ID}/chain`) {
      return { body: auditChain() };
    }
    if (request.method === 'POST' && request.path === `/v1/tickets/${W3_TICKET_ID}/actions/approve`) {
      return approveReply();
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });

  resetApiConfig();
  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-exec',
    mock: false,
    roles: ['reviewer'],
    viewerId: '',
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  await server.close();
  resetApiConfig();
});

async function approve(): Promise<void> {
  await screen.findByText('T-W3');
  fireEvent.click(screen.getByRole('button', { name: '审批通过并执行' }));
  fireEvent.click(screen.getByRole('button', { name: '确认审批通过' }));
}

describe('工单详情：执行回执可见', () => {
  it('把 executed_steps 渲染成步骤列表，含动作、写入对象与幂等键', async () => {
    renderPage(<TicketDetailPage />, { route: `/tickets/${W3_TICKET_ID}`, path: '/tickets/:ticketId' });
    await approve();

    const list = await screen.findByTestId('executed-steps');
    const steps = within(list).getAllByTestId('executed-step');
    expect(steps).toHaveLength(2);

    // 当前身份必须跟令牌会话走，而不是 env 里的固定名字。
    expect(await screen.findByText(/dddddddd… · reviewer \/ approver \/ cfo/)).toBeInTheDocument();

    const first = steps[0];
    expect(within(first).getByText('u8.po.create_draft')).toBeInTheDocument();
    expect(within(first).getByText(/写入 u8 · purchase_order:PO-2026-0001/)).toBeInTheDocument();
    expect(within(first).getByText('idem-exec-aaa')).toBeInTheDocument();
    expect(within(first).getByText('幂等键')).toBeInTheDocument();
    // 第二步的幂等键必须与第一步不同，避免所有步骤共用同一个键值造成误读。
    expect(within(steps[1]).getByText('idem-exec-bbb')).toBeInTheDocument();
  });

  it('类别闸 403：原样展示 detail 判据，不折叠成「操作失败」', async () => {
    approveReply = () =>
      problem(403, 'E-POLICY-CATEGORY', '角色 [reviewer] 不能复核 finance 类场景');
    renderPage(<TicketDetailPage />, { route: `/tickets/${W3_TICKET_ID}`, path: '/tickets/:ticketId' });
    await approve();

    await waitFor(() => {
      expect(screen.getByText('角色 [reviewer] 不能复核 finance 类场景')).toBeInTheDocument();
    });
    const panel = screen.getByTestId('error-panel');
    expect(panel.getAttribute('data-kind')).toBe('forbidden');
    expect(panel.textContent).toContain('角色 [reviewer] 不能复核 finance 类场景');
    expect(panel.textContent).toContain('业务类别闸拒绝');
    // 类别闸不是缺角色：不能给出「请管理员开通角色/scope」这种会误导现场的解释。
    expect(panel.textContent).not.toContain('缺少所需角色或 scope');
    expect(panel.textContent).not.toContain('操作失败');
  });
});
