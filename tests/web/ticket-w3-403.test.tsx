import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { TicketDetailPage } from '../../web/src/pages/TicketDetailPage';
import { auditChain, auditImage, ticketDetail, W3_TICKET_ID } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

let server: TestApi;

beforeEach(async () => {
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === `/v1/tickets/${W3_TICKET_ID}`) {
      return { body: ticketDetail() };
    }
    if (request.method === 'GET' && request.path === `/v1/audit/tickets/${W3_TICKET_ID}/images`) {
      return { body: { items: [auditImage()], next_cursor: null } };
    }
    if (request.method === 'GET' && request.path === `/v1/audit/tickets/${W3_TICKET_ID}/chain`) {
      return { body: auditChain() };
    }
    if (request.method === 'POST' && request.path === `/v1/tickets/${W3_TICKET_ID}/actions/approve`) {
      return problem(403, 'E-POLICY-W3FORBIDDEN', 'W3 写等级禁止自动执行，整单拒绝；连接器零写入');
    }
    return problem(404, 'E-DATA-MISSING', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-w3',
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

describe('工单详情：真实链路 + W3 403', () => {
  it('渲染 12 状态轨道、步骤前像/后像与审计链', async () => {
    renderPage(<TicketDetailPage />, { route: `/tickets/${W3_TICKET_ID}`, path: '/tickets/:ticketId' });

    expect(await screen.findByText('T-W3')).toBeInTheDocument();
    expect(screen.getByText('12 状态机')).toBeInTheDocument();
    expect(screen.getAllByText('前像').length).toBeGreaterThan(0);
    expect(screen.getAllByText('后像').length).toBeGreaterThan(0);
    expect(screen.getByText('完整')).toBeInTheDocument();
    expect(screen.getAllByText(/sha256:t/).length).toBeGreaterThan(0);
  });

  it('W3 审批被拒：界面显示 403 / E-POLICY-W3FORBIDDEN / 连接器零写入', async () => {
    renderPage(<TicketDetailPage />, { route: `/tickets/${W3_TICKET_ID}`, path: '/tickets/:ticketId' });

    await screen.findByText('T-W3');
    fireEvent.click(screen.getByRole('button', { name: '审批通过并执行' }));
    fireEvent.click(screen.getByRole('button', { name: '确认审批通过' }));

    await waitFor(() => {
      expect(screen.getByText(/403 · E-POLICY-W3FORBIDDEN/)).toBeInTheDocument();
    });
    expect(screen.getByText('连接器零写入')).toBeInTheDocument();
    expect(screen.getByText(/未向任何下游系统下发写入/)).toBeInTheDocument();

    const action = server.requests.find((item) => item.path.endsWith('/actions/approve'));
    expect(action?.headers['idempotency-key']).toBeTruthy();
    expect(action?.body).toMatchObject({ scope: { org_id: 'ORG-1', period: '2026-10' } });
  });
});
