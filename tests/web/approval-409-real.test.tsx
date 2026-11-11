import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { TicketDetailPage } from '../../web/src/pages/TicketDetailPage';
import { auditChain, auditImage, DUAL_TICKET_ID, ticketDetail } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

/**
 * 真后端 409 的形状（不是 mock 模块编出来的）。
 *
 * 来源：`tests/api/test_demo_write_path.py::test_approval_roles_and_execution_roles_are_separately_named`
 * 实测同一张 B 级 W2 工单——approver 已签、reviewer 未签时，执行动作返回：
 * code/title/detail 与下面一致，角色差集四字段由 `ApprovalNotSatisfied` 落进 problem body。
 * 该 Python 用例负责证明「后端真的这么发」，这里负责证明「面板真的这么读」。
 */
const REAL_409 = {
  type: 'about:blank',
  title: '审批未满足',
  status: 409,
  detail: "审批未满足（G-04）：缺少角色 ['reviewer']",
  code: 'E-POLICY-APPROVAL',
  trace_id: 'trace-from-real-backend',
  retriable: false,
  fail_code: null,
  approval_required_roles: ['approver'],
  approval_missing_roles: [],
  execution_required_roles: ['approver', 'reviewer'],
  execution_missing_roles: ['reviewer'],
};

let server: TestApi;
let reply: Record<string, unknown>;

beforeEach(() => {
  resetApiConfig();
  reply = REAL_409;
});

afterEach(async () => {
  if (server) await server.close();
  resetApiConfig();
});

async function mountAndFail() {
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === `/v1/tickets/${DUAL_TICKET_ID}`) {
      return {
        body: ticketDetail({
          ticket_id: DUAL_TICKET_ID,
          ticket_no: 'T-REAL-409',
          business_key: 'BK-REAL-409',
          state: '待审批',
          write_level: 'W2',
          amount: '5000.0000',
          approval_required_roles: ['approver'],
          approval_missing_roles: [],
          execution_required_roles: ['approver', 'reviewer'],
          execution_missing_roles: ['reviewer'],
        }),
      };
    }
    if (request.method === 'GET' && request.path === `/v1/audit/tickets/${DUAL_TICKET_ID}/images`) {
      return { body: { items: [auditImage()], next_cursor: null } };
    }
    if (request.method === 'GET' && request.path === `/v1/audit/tickets/${DUAL_TICKET_ID}/chain`) {
      return { body: auditChain() };
    }
    if (request.method === 'POST' && request.path === `/v1/tickets/${DUAL_TICKET_ID}/actions/approve`) {
      return problem(409, 'E-POLICY-APPROVAL', String(reply.detail), reply);
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-real-409',
    mock: false,
    roles: ['approver'],
    viewerId: 'cccccccc-cccc-cccc-cccc-cccccccccccc',
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });

  renderPage(<TicketDetailPage />, {
    route: `/tickets/${DUAL_TICKET_ID}`,
    path: '/tickets/:ticketId',
  });
  await screen.findByText('T-REAL-409');
  fireEvent.click(screen.getByRole('button', { name: '审批通过并执行' }));
  fireEvent.click(screen.getByRole('button', { name: '确认审批通过' }));
  await waitFor(() => {
    expect(screen.getByText(/409 · E-POLICY-APPROVAL/)).toBeInTheDocument();
  });
}

describe('工单详情：真后端 409 的角色差集驱动面板', () => {
  it('带 execution_* 字段时：亮出「执行尚缺角色」并说明审批一票 ≠ 执行一票', async () => {
    await mountAndFail();

    expect(screen.getByText('执行尚缺角色：')).toBeInTheDocument();
    expect(screen.getAllByText('复核人').length).toBeGreaterThan(0);
    expect(screen.getByText(/审批链已满足；执行另需 B 级连接器强制双签/)).toBeInTheDocument();
    expect(screen.queryByText('审批尚缺角色：')).toBeNull();
  });

  it('旧同名键（missing_roles / required_roles）不再驱动面板：砍掉旧写法必须红', async () => {
    reply = {
      ...REAL_409,
      approval_required_roles: null,
      approval_missing_roles: null,
      execution_required_roles: null,
      execution_missing_roles: null,
      missing_roles: ['reviewer'],
      required_roles: ['reviewer', 'approver'],
    };
    await mountAndFail();

    expect(screen.queryByText('执行尚缺角色：')).toBeNull();
    expect(screen.queryByText('审批尚缺角色：')).toBeNull();
    expect(screen.queryByText('尚缺角色：')).toBeNull();
  });

  it('完全不带角色字段的 409：只显示错误码与 detail，不臆造缺票', async () => {
    reply = {
      ...REAL_409,
      approval_required_roles: null,
      approval_missing_roles: null,
      execution_required_roles: null,
      execution_missing_roles: null,
    };
    await mountAndFail();

    expect(screen.queryByText('执行尚缺角色：')).toBeNull();
    expect(screen.queryByText('审批尚缺角色：')).toBeNull();
    expect(screen.getByText(/缺少角色 \['reviewer'\]/)).toBeInTheDocument();
  });
});
