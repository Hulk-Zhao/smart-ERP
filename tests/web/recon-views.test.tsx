import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { MeteringPage } from '../../web/src/pages/MeteringPage';
import { ReconBatchPage } from '../../web/src/pages/ReconBatchPage';
import { ReconDiffPage } from '../../web/src/pages/ReconDiffPage';
import {
  BATCH_ID,
  DIFF_ID,
  meteringStatement,
  reconBatch,
  reconDiff,
  reconDiffDetail,
} from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

let server: TestApi;

beforeEach(async () => {
  server = await startApi((request) => {
    if (request.path === '/v1/auth/session') {
      return {
        body: {
          user_id: REVIEWER_ID,
          tenant_id: TENANT_ID,
          roles: ['reviewer'],
          default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
        },
      };
    }
    if (request.path === `/v1/recon/runs/${BATCH_ID}`) {
      const batch = reconBatch({ suppliers: ['S-STL-0076', 'S-STL-0077'] });
      return {
        body: {
          ...batch,
          metrics: batch.metrics ? { ...batch.metrics, diff_amount: '7108301.1000', matched: 3, diff_lines: 1 } : null,
        },
      };
    }
    if (request.path === `/v1/recon/runs/${BATCH_ID}/diffs`) {
      return {
        body: {
          items: [
            reconDiff({ diff_id: 'd8', type: 'candidate', confidence: '0.85' }),
            reconDiff({ diff_id: 'd9', type: 'unparseable', confidence: '0.62' }),
          ],
          next_cursor: null,
        },
      };
    }
    if (request.path === `/v1/recon/runs/${BATCH_ID}/diffs/${DIFF_ID}`) {
      const detail = reconDiffDetail({
        type: 'unit_price',
        amount: { amount: '1760.0800', currency: 'CNY', tax_included: true },
        draft: {
          ...reconDiffDetail().draft!,
          // 现场旧数据里草稿状态就是「待确认」：标题不能渲染成「待确认 · 待确认」。
          status: '待确认',
          diff_type: 'unit_price',
        },
        related_lines: [
            {
              item: 'A-001',
              qty_mine: '80',
              qty_supplier: '80',
              amount: { amount: '52048.0800', currency: 'CNY', tax_included: true },
              match_type: 'single',
              confidence: '0.80',
              status: 'diff',
              our_ref: 'u8:PO-1#1',
              supplier_ref: 'statement:ST-1#1',
              needs_manual: true,
              note: '单价不一致',
            },
          ],
          evidence: [
            { system: 'u8', doc_no: 'PO-1', field: 'amount', coord: { page: 1, bbox: [1, 2, 3, 4] } },
            { system: 'statement', doc_no: 'ST-1', field: 'amount', coord: { page: 1, bbox: [5, 6, 7, 8] } },
          ],
      });
      return { body: detail };
    }
    if (request.path === `/v1/metering/batches/${BATCH_ID}/statement`) {
      const base = meteringStatement();
      const lines = (base.lines ?? []).map((line, index) =>
        index === 0
          ? { ...line, confidence: '0.85' }
          : index === 59
            ? { ...line, excluded_reason: 'duplicate_billing', confidence: '0.62' }
            : line,
      );
      return { body: { ...base, lines } };
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

describe('批次详情页头与指标', () => {
  it('标题是「对账批次」，不与供应商串和批次 ID 粘连；差异金额不裁切且带币种', async () => {
    renderPage(<ReconBatchPage />, { route: `/recon/${BATCH_ID}`, path: '/recon/:batchId' });

    const title = await screen.findByTestId('batch-title');
    expect(title.textContent).toContain('对账批次');
    expect(title.textContent).not.toContain('S-STL-0076');
    expect(title.textContent).not.toContain(BATCH_ID);

    const amountCell = await screen.findByTestId('metric-diff-amount', {}, { timeout: 5000 });
    expect(amountCell.textContent).toBe('7,108,301.10 CNY');
    // truncate 会把金额裁成 7,108,301.，因此这一格必须允许换行。
    expect(amountCell.className).not.toContain('truncate');
  });

  it('英文枚举有中文标签，置信度注明来源，三个行级口径有说明', async () => {
    renderPage(<ReconBatchPage />, { route: `/recon/${BATCH_ID}`, path: '/recon/:batchId' });

    // 桌面表格与移动列表各渲染一份，这里只要求至少出现中文标签而不是裸枚举。
    expect((await screen.findAllByText('候选待人工（candidate）', {}, { timeout: 5000 })).length).toBeGreaterThan(0);
    expect(screen.getAllByText('不可解析（unparseable）').length).toBeGreaterThan(0);
    expect(screen.queryByText('candidate')).toBeNull();
    const confidenceHeader = screen.getAllByText('置信度（规则）')[0];
    expect(confidenceHeader.getAttribute('title')).toContain('不是模型输出');
    expect(screen.getByTestId('metric-caliber').textContent).toContain('互斥');
  });
});

describe('差异详情：差异金额与关联行金额分开', () => {
  it('头部差异金额取 diff.amount，行金额单独标注，不塞进「差异」列', async () => {
    renderPage(<ReconDiffPage />, {
      route: `/recon/${BATCH_ID}/diffs/${DIFF_ID}`,
      path: '/recon/:batchId/diffs/:diffId',
    });

    const header = await screen.findByTestId('diff-amount', {}, { timeout: 5000 });
    expect(header.textContent).toBe('1,760.08 CNY');

    const related = screen.getByTestId('related-line-amounts');
    expect(within(related).getByTestId('related-line-amount').textContent).toBe('52,048.08 CNY');
    expect(screen.getAllByText('52,048.08 CNY')).toHaveLength(1);

    // 旧的金额行（把行金额当差异展示）必须消失。
    expect(screen.queryByText('金额（A-001）')).toBeNull();
    expect(screen.queryByText('差异金额 1,760.08 CNY')).toBeNull();
  });

  it('原因码列改称差异类型，草稿状态不重复，证据条数可见', async () => {
    renderPage(<ReconDiffPage />, {
      route: `/recon/${BATCH_ID}/diffs/${DIFF_ID}`,
      path: '/recon/:batchId/diffs/:diffId',
    });

    await screen.findByTestId('diff-amount', {}, { timeout: 5000 });
    // 表头（字段 diff）与草稿卡片都有「差异类型」，这里只要求旧的「原因码」不再出现。
    expect(screen.getAllByText('差异类型').length).toBeGreaterThan(0);
    expect(screen.queryByText('原因码')).toBeNull();

    expect(screen.queryByText('待确认 · 待确认')).toBeNull();
    expect(screen.getByText('待确认')).toBeInTheDocument();

    expect(screen.getByTestId('evidence-count').textContent).toContain('共 2 条');
  });
});

describe('计量账本：英文原因码与置信度格式', () => {
  it('不计费原因码带中文标签，置信度统一为百分比', async () => {
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    expect(await screen.findByText('重复计费（duplicate_billing）', {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getAllByText('85.00%').length).toBeGreaterThan(0);
    expect(screen.queryByText('0.85')).toBeNull();
  });
});
