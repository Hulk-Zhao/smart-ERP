import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { TicketOut } from '../../web/src/api/types';
import { TicketsPage } from '../../web/src/pages/TicketsPage';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

function tickets(): TicketOut[] {
  return Array.from({ length: 18 }, (_, index) => ({
    ticket_id: `c0000000-0000-4000-8000-0000000000${String(index).padStart(2, '0')}`,
    ticket_no: `TK-LIST-${String(index + 1).padStart(2, '0')}`,
    business_key: `BK-LIST-${index + 1}`,
    state: '已完成',
    write_level: 'W1',
    scene_code: 'ag1_reconcile',
    amount: '100.0000',
    touches_fund: false,
    initiator_id: REVIEWER_ID,
    org_id: 'ORG-1',
    period: '2026-10',
    systems: ['u8'],
    version: 1,
    created_at: '2026-11-10T09:00:00Z',
    updated_at: '2026-11-10T09:30:00Z',
  }));
}

let server: TestApi;
let paged = false;

beforeEach(async () => {
  paged = false;
  server = await startApi((request) => {
    if (request.path === '/v1/auth/session') {
      return {
        body: {
          user_id: REVIEWER_ID,
          tenant_id: TENANT_ID,
          roles: ['reviewer'],
          default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8'] },
        },
      };
    }
    if (request.path === '/v1/tickets') {
      if (!paged) return { body: { items: tickets(), next_cursor: null } };
      return request.query.cursor?.[0] === 'cursor-2'
        ? { body: { items: [], next_cursor: null } }
        : { body: { items: tickets(), next_cursor: 'cursor-2' } };
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

describe('工单列表分页：没有 next_cursor 时下一页必须禁用', () => {
  it('18/18 一页满且无 next_cursor：按钮禁用、写明已到末页，点击不产生请求', async () => {
    renderPage(<TicketsPage />, { route: '/tickets', path: '/tickets' });

    await screen.findAllByText('TK-LIST-01');
    const next = screen.getByTestId('tickets-next-page');
    expect(next).toBeDisabled();
    expect(screen.getByTestId('tickets-page-footer').textContent).toContain('已到末页');

    fireEvent.click(next);
    await waitFor(() => {
      expect(server.requests.filter((request) => request.path === '/v1/tickets')).toHaveLength(1);
    });
  });

  it('有 next_cursor 时点击下一页会带上 cursor 再请求一次', async () => {
    paged = true;
    renderPage(<TicketsPage />, { route: '/tickets', path: '/tickets' });

    await screen.findAllByText('TK-LIST-01');
    const next = screen.getByTestId('tickets-next-page');
    expect(next).toBeEnabled();
    expect(screen.getByTestId('tickets-page-footer').textContent).toContain('还有下一页');

    fireEvent.click(next);
    await waitFor(() => {
      const listRequests = server.requests.filter((request) => request.path === '/v1/tickets');
      expect(listRequests).toHaveLength(2);
      expect(listRequests[1].query.cursor).toEqual(['cursor-2']);
    });
  });
});
