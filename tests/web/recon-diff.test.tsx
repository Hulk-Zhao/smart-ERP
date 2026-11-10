import { fireEvent, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { ReconDiffPage } from '../../web/src/pages/ReconDiffPage';
import { BATCH_ID, DIFF_ID, reconDiffDetail } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

let server: TestApi;

beforeEach(async () => {
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === `/v1/recon/runs/${BATCH_ID}/diffs/${DIFF_ID}`) {
      // 含 rounding_tolerance 的批次：后端修复后正常返回 200
      return { body: reconDiffDetail() };
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-diff',
    mock: false,
    roles: ['reviewer'],
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  await server.close();
  resetApiConfig();
});

describe('差异明细：字段级 diff + 证据下钻（含 rounding_tolerance）', () => {
  it('渲染我方/对方/差异/原因码，舍入容差行可正常展示', async () => {
    renderPage(<ReconDiffPage />, {
      route: `/recon/${BATCH_ID}/diffs/${DIFF_ID}`,
      path: '/recon/:batchId/diffs/:diffId',
    });

    expect(await screen.findByText('字段级差异')).toBeInTheDocument();
    expect(screen.getAllByText('金额舍入容差').length).toBeGreaterThan(0);
    expect(screen.getAllByText('rounding_tolerance').length).toBeGreaterThan(0);
    expect(screen.getAllByText('我方值').length).toBeGreaterThan(0);
    expect(screen.getAllByText('对方值').length).toBeGreaterThan(0);
    expect(screen.getAllByText('原因码').length).toBeGreaterThan(0);
    expect(screen.getByText('数量（A-001）')).toBeInTheDocument();
    expect(screen.getByText('单据引用（A-001）')).toBeInTheDocument();
    expect(screen.getByText('u8:PO-202610-001#1')).toBeInTheDocument();
    // 对方单号同时出现在差异行与处置草稿引用中，两处都必须可读。
    expect(screen.getAllByText('statement:ST-202610-001#1').length).toBeGreaterThan(0);
  });

  it('点击证据可下钻到原始单据坐标（页码 + bbox）', async () => {
    renderPage(<ReconDiffPage />, {
      route: `/recon/${BATCH_ID}/diffs/${DIFF_ID}`,
      path: '/recon/:batchId/diffs/:diffId',
    });

    await screen.findByText('证据下钻');
    const chip = screen.getByRole('button', { name: /u8 · PO-202610-001 · amount/ });
    fireEvent.click(chip);

    const panel = screen.getByTestId('evidence-detail');
    expect(within(panel).getByText('2')).toBeInTheDocument();
    expect(within(panel).getByText('10, 20, 30, 40')).toBeInTheDocument();
    expect(within(panel).getByText(/原始坐标 JSON/)).toBeInTheDocument();
  });
});
