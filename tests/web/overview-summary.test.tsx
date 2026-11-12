import { screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { ReconBatchSummaryOut, TicketOut } from '../../web/src/api/types';
import { OverviewPage } from '../../web/src/pages/OverviewPage';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

const STATES: TicketOut['state'][] = ['计划中', '待确认', '待审批', '执行中', '校验中', '已完成'];

function tickets(): TicketOut[] {
  return STATES.map((state, index) => ({
    ticket_id: `c0000000-0000-4000-8000-00000000001${index}`,
    ticket_no: `TK-OV-${index + 1}`,
    business_key: `BK-OV-${index + 1}`,
    state,
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

const BATCH: ReconBatchSummaryOut = {
  batch_id: '316d16a4-c0b1-595f-ad5e-4b8a664d2997',
  status: 'reconciled',
  line_count: 104,
  diff_count: 80,
  diff_amount: '7108301.1000',
  created_at: '2026-11-10T10:00:00Z',
};

let server: TestApi;
let batches: ReconBatchSummaryOut[] = [BATCH];

beforeEach(async () => {
  batches = [BATCH];
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
    if (request.path === '/v1/tickets') return { body: { items: tickets(), next_cursor: null } };
    if (request.path === '/v1/approvals/pending') return { body: { items: [], next_cursor: null } };
    if (request.path === '/v1/recon/batches') return { body: { items: batches, next_cursor: null } };
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
  });
  resetApiConfig();
  configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false });
});

afterEach(async () => {
  resetApiConfig();
  await server.close();
});

describe('工作台对账摘要接真实批次', () => {
  it('scope 内已有批次时显示最近批次与真实差异金额，不再显示「从本期对账开始」空态', async () => {
    renderPage(<OverviewPage />, { route: '/', path: '/' });

    const summary = await screen.findByTestId('overview-recon-summary', {}, { timeout: 5000 });
    expect(within(summary).getByText('316d16a4…')).toBeTruthy();
    expect(within(summary).getByText('7,108,301.10 CNY')).toBeTruthy();
    expect(within(summary).getByText('104')).toBeTruthy();
    expect(within(summary).getByText('80')).toBeTruthy();
    expect(screen.queryByText('从本期对账开始')).toBeNull();
    expect(summary.getAttribute('href')).toBe(`/recon/${BATCH.batch_id}`);
  });

  it('确实没有批次时才显示空态引导', async () => {
    batches = [];
    renderPage(<OverviewPage />, { route: '/', path: '/' });

    expect(await screen.findByText('该 scope 还没有对账批次', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByTestId('overview-recon-summary')).toBeNull();
  });
});

describe('工作台状态分布与「当前页 N 条」自洽', () => {
  it('列出全部非零状态，不只截前 5 个', async () => {
    renderPage(<OverviewPage />, { route: '/', path: '/' });

    await screen.findAllByText('TK-OV-1');
    await waitFor(() => expect(screen.getAllByTestId('distribution-row')).toHaveLength(6));
    const labels = screen
      .getAllByTestId('distribution-row')
      .map((row) => row.textContent ?? '');
    for (const state of STATES) {
      expect(labels.some((label) => label.includes(state))).toBe(true);
    }
  });
});
