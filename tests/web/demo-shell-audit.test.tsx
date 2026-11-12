import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { TicketDetailPage } from '../../web/src/pages/TicketDetailPage';
import { auditChain, auditImage, ticketDetail, W3_TICKET_ID } from './helpers/fixtures';
import { renderPage, renderShell } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

let server: TestApi;

function sessionReply() {
  return {
    body: {
      user_id: REVIEWER_ID,
      tenant_id: TENANT_ID,
      roles: ['reviewer'],
      default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8'] },
    },
  };
}

beforeEach(async () => {
  server = await startApi((request) => {
    if (request.path === '/v1/auth/session') return sessionReply();
    if (request.path === '/v1/approvals/pending') return { body: { items: [], next_cursor: null } };
    if (request.path === `/v1/tickets/${W3_TICKET_ID}`) {
      return { body: ticketDetail({ write_level: 'W2', audit_image_count: 1 }) };
    }
    if (request.path === `/v1/audit/tickets/${W3_TICKET_ID}/images`) {
      return { body: { items: [auditImage()], next_cursor: null } };
    }
    if (request.path === `/v1/audit/tickets/${W3_TICKET_ID}/chain`) {
      return { body: auditChain() };
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

describe('顶栏通知按钮不是死控件', () => {
  it('点击后出现说明面板（明确「通知中心未启用」），再点收起', async () => {
    renderShell(<div data-testid="page-body" />, { route: '/' });

    const button = await screen.findByTestId('notify-button');
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByTestId('notification-panel')).toBeNull();

    fireEvent.click(button);
    const panel = await screen.findByTestId('notification-panel');
    expect(panel.textContent).toContain('通知中心未启用');
    expect(button.getAttribute('aria-expanded')).toBe('true');

    fireEvent.click(button);
    expect(screen.queryByTestId('notification-panel')).toBeNull();
  });
});

describe('审计链与审计影像可下钻', () => {
  it('链卡给出影像明细入口，影像逐条可展开查看完整 hash 与 field_diff', async () => {
    renderPage(<TicketDetailPage />, { route: `/tickets/${W3_TICKET_ID}`, path: '/tickets/:ticketId' });

    const link = await screen.findByTestId('audit-chain-detail-link');
    // 影像列表是异步加载：等计数从 0 变为 1，避免时序抖动导致假红。
    await waitFor(() => expect(link.textContent).toContain('查看影像明细（1 条）'));

    const row = await screen.findByTestId('audit-image-row');
    expect(screen.queryByTestId('audit-image-detail')).toBeNull();

    fireEvent.click(within(row).getByRole('button', { name: '查看' }));
    const detail = await screen.findByTestId('audit-image-detail');
    expect(detail.textContent).toContain('sha256:test-image-1');
    expect(detail.textContent).toContain('prev_hash：GENESIS');
    // field_diff 逐条展示，不只给一串 JSON。
    expect(detail.textContent).toContain('数量');
    expect(detail.textContent).toContain('金额');

    fireEvent.click(within(row).getByRole('button', { name: '收起' }));
    expect(screen.queryByTestId('audit-image-detail')).toBeNull();
  });
});
