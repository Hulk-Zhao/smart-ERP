import { createHash } from 'node:crypto';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { MeteringLineOut } from '../../web/src/api/types';
import { MeteringPage } from '../../web/src/pages/MeteringPage';
import { ReconBatchPage } from '../../web/src/pages/ReconBatchPage';
import { BATCH_ID, meteringStatement, reconBatch } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const STATEMENT_PATH = `/v1/metering/batches/${BATCH_ID}/statement`;
const CSV_TEXT = '\ufeffline_no,side,status\n1,supplier,已对账\n2,supplier,差异挂起\n';
const CSV_TRUE_SHA = createHash('sha256').update(Buffer.from(CSV_TEXT, 'utf8')).digest('hex');
const CSV_WRONG_SHA = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

let server: TestApi;
let downloadClicks = 0;
let mode: 'ok' | 'not-found' = 'ok';
let statementBody = meteringStatement();
let csvSha = CSV_WRONG_SHA;

/** 计数从不写字面量：一律从同一份 fixture 推导，mock 数据改了用例仍然只表达语义。 */
function bucketCount(bucket: MeteringLineOut['billing_bucket']): number {
  return (statementBody.lines ?? []).filter((line) => line.billing_bucket === bucket).length;
}

function detailLineCount(): number {
  return statementBody.lines?.length ?? 0;
}

/** 服务端算式含 . ( ) 等字符，断言前转义成字面量。 */
function literal(text: string): RegExp {
  return new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
}

function stubSubtle(): void {
  vi.stubGlobal('crypto', {
    subtle: {
      digest: async (_algorithm: string, data: Uint8Array) => {
        const hash = createHash('sha256').update(Buffer.from(data)).digest();
        return hash.buffer.slice(hash.byteOffset, hash.byteOffset + hash.byteLength);
      },
    },
  });
}

beforeAll(() => {
  // jsdom 没有 object URL 与真实下载；只验证页面拿到服务端摘要并触发一次下载。
  Object.defineProperty(URL, 'createObjectURL', { value: vi.fn(() => 'blob:metering'), configurable: true });
  Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {
    downloadClicks += 1;
  });
});

afterAll(() => {
  vi.restoreAllMocks();
});

beforeEach(async () => {
  downloadClicks = 0;
  mode = 'ok';
  statementBody = meteringStatement();
  csvSha = CSV_WRONG_SHA;
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === STATEMENT_PATH) {
      if (request.query.format?.[0] === 'csv') {
        return {
          text: CSV_TEXT,
          headers: {
            'content-type': 'text/csv; charset=utf-8',
            'content-disposition': 'attachment; filename="metering-E2E.csv"',
            'x-content-sha256': csvSha,
          },
        };
      }
      if (mode === 'not-found') return problem(404, 'E-RESOURCE-NOTFOUND', 'batch not found');
      return { body: statementBody };
    }
    if (request.method === 'GET' && request.path === `/v1/recon/runs/${BATCH_ID}`) {
      return { body: reconBatch() };
    }
    if (request.method === 'GET' && request.path === `/v1/recon/runs/${BATCH_ID}/diffs`) {
      return { body: { items: [], next_cursor: null } };
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-metering',
    mock: false,
    roles: ['reviewer'],
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  vi.unstubAllGlobals();
  await server.close();
  resetApiConfig();
});

describe('计量表：四类行 + data_gaps 显式可见 + CSV 摘要', () => {
  it('渲染四类行、单价来源、金额、算式与上界说明；null 用「—」并给出原因', async () => {
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    expect(await screen.findByText(/计量表 · 批次/)).toBeInTheDocument();

    // 四类行：口径数字与可下钻桶名分开，术语按接口字段语义一一对应
    const caliber = screen.getByTestId('metering-caliber');
    expect(caliber).toHaveTextContent('供应商对账单行数（应处理）');
    expect(caliber).toHaveTextContent('有效对账行数');
    expect(caliber).toHaveTextContent('计费行数（billable 桶）');
    // 两个数当前相等，页面必须解释为什么：本期无扣减；扣减上线后会分开
    expect(screen.getByTestId('metering-valid-vs-billable')).toHaveTextContent(
      '本期无扣减，故两者相等；发生扣减后计费行数将小于有效对账行数。',
    );
    expect(screen.getByTestId('metering-bucket-billable')).toHaveTextContent('计费行（billable 桶）');
    expect(screen.getByTestId('metering-bucket-auto_released')).toHaveTextContent('自动放行但扣减');
    expect(screen.getByTestId('metering-bucket-dispute_hold')).toHaveTextContent('争议挂起');
    expect(screen.getByTestId('metering-bucket-other_unbillable')).toHaveTextContent('其他不计费');

    // 金额与单价来源
    expect(screen.getByText('本期金额')).toBeInTheDocument();
    expect(screen.getByText(/来源：ERP_METERING_UNIT_PRICE/)).toBeInTheDocument();

    // null → 「—」且带原因（封顶余额卡片 + 两类计数行）
    expect(screen.getAllByText(/合同封顶金额未进入数据模型，无法计算封顶余额/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/错放行确认结果当前无数据源/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/客户原因分类当前无数据源/).length).toBeGreaterThan(0);

    // data_gaps 必须显式可见、不折叠
    const gaps = screen.getByTestId('metering-data-gaps');
    expect(within(gaps).getByText('这几项暂无数据源')).toBeVisible();
    expect(within(gaps).getByText(/无法计算封顶余额/)).toBeVisible();

    // 上界说明：计费行数（billable 桶）是上界，且明确与有效对账行数分开
    const bound = screen.getByTestId('metering-upper-bound');
    expect(within(bound).getByText(/计费行数（billable 桶）是上界，不是精确值/)).toBeVisible();
    expect(within(bound).getByText(/已确认错放行（confirmed_misrelease_lines）/)).toBeVisible();
    expect(within(bound).getByText(/客户原因（customer_reason_lines）/)).toBeVisible();
    expect(within(bound).getByTestId('metering-billable-lines')).toHaveTextContent(String(statementBody.billable_lines));
    expect(within(bound).getByText(/只数 billable 桶，不等于「有效对账行数」/)).toBeVisible();
    expect(within(bound).getByTestId('metering-valid-lines')).toHaveTextContent(String(statementBody.auto_released_lines));

    // calculation 算式：页面原样展示服务端文本，断言从 fixture 取，不抄数字
    const amountCalc = (statementBody.calculation ?? []).find((step) => step.includes('CNY'));
    expect(amountCalc).toBeTruthy();
    expect(screen.getByText(literal(amountCalc as string))).toBeInTheDocument();

    // 契约文案缺口：API 修好 calculation 文案后，一并删除 MeteringPage 的 metering-calculation-gap、
    // mock.ts 中镜像的两行文案，以及下面这条断言
    expect(screen.getByTestId('metering-calculation-gap')).toBeVisible();
  });

  it('四类行卡片数字与下钻列表行数逐一相等（D8 验收）', async () => {
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    await screen.findByText(/计量表 · 批次/);

    // 分母来自接口字段 expected_lines，不写字面量；先证明明细覆盖完整对账单，分母才成立
    const expectedTotal = statementBody.expected_lines;
    expect(detailLineCount()).toBe(expectedTotal);
    // 字段语义与桶计数对齐：billable 桶 = billable_lines；billable + auto_released 桶 = auto_released_lines
    expect(bucketCount('billable')).toBe(statementBody.billable_lines);
    expect(bucketCount('billable') + bucketCount('auto_released')).toBe(statementBody.auto_released_lines);

    const buckets = [
      ['billable', '计费行（billable 桶）'],
      ['auto_released', '自动放行但扣减'],
      ['dispute_hold', '争议挂起'],
      ['other_unbillable', '其他不计费'],
    ] as const;

    for (const [key, label] of buckets) {
      // 卡片数字 == 同一过滤条件下的计数（两者都从 fixture 推导）
      const count = bucketCount(key);
      expect(screen.getByTestId(`metering-bucket-${key}-value`)).toHaveTextContent(String(count));
      // 点进去列表的同名数字（差一行就会失败）
      fireEvent.click(screen.getByTestId(`metering-bucket-${key}`));
      expect(
        screen.getByText(new RegExp(`已筛选：${label} · ${count} 行 / 共 ${expectedTotal} 行`)),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByText('清除筛选'));
    }

    // 清除后回到全部明细
    expect(screen.getByText('#1')).toBeInTheDocument();
    expect(screen.getByText(`#${expectedTotal}`)).toBeInTheDocument();
  });

  it('上界说明只点名实际缺失的扣减项', async () => {
    statementBody = meteringStatement({ confirmed_misrelease_lines: 3 });
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    const bound = await screen.findByTestId('metering-upper-bound');
    expect(within(bound).getByText(/客户原因（customer_reason_lines）当前无数据源/)).toBeVisible();
    expect(within(bound).queryByText(/已确认错放行（confirmed_misrelease_lines）/)).toBeNull();
    expect(within(bound).queryByText(/两项扣减项/)).toBeNull();
  });

  it('下载 CSV 后显示服务端 X-Content-SHA256，且请求带 format=csv', async () => {
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    await screen.findByText(/计量表 · 批次/);
    fireEvent.click(screen.getByRole('button', { name: '下载 CSV' }));

    const sha = await screen.findByTestId('metering-sha256');
    await waitFor(() => expect(sha).toHaveTextContent(CSV_WRONG_SHA));
    expect(screen.getByText(/metering-E2E\.csv/)).toBeInTheDocument();
    expect(downloadClicks).toBe(1);

    const csvRequest = server.requests.find((request) => request.query.format?.[0] === 'csv');
    expect(csvRequest).toBeDefined();
    expect(csvRequest?.path).toBe(STATEMENT_PATH);
    expect(csvRequest?.headers['x-session-token']).toBe('token-metering');
  });

  it('本地重算 SHA-256 与服务端摘要一致', async () => {
    csvSha = CSV_TRUE_SHA;
    stubSubtle();
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    await screen.findByText(/计量表 · 批次/);
    fireEvent.click(screen.getByRole('button', { name: '下载 CSV' }));

    await waitFor(() =>
      expect(screen.getByTestId('metering-sha-verdict')).toHaveTextContent('一致：文件内容未被改动'),
    );
    expect(screen.getByTestId('metering-local-sha256')).toHaveTextContent(CSV_TRUE_SHA);
  });

  it('本地重算与服务端摘要不一致时明确报错', async () => {
    csvSha = CSV_WRONG_SHA;
    stubSubtle();
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    await screen.findByText(/计量表 · 批次/);
    fireEvent.click(screen.getByRole('button', { name: '下载 CSV' }));

    await waitFor(() =>
      expect(screen.getByTestId('metering-sha-verdict')).toHaveTextContent('不一致：文件可能被改动'),
    );
  });

  it('环境不支持 Web Crypto 时降级为仅展示服务端摘要', async () => {
    vi.stubGlobal('crypto', {});
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    await screen.findByText(/计量表 · 批次/);
    fireEvent.click(screen.getByRole('button', { name: '下载 CSV' }));

    await waitFor(() =>
      expect(screen.getByTestId('metering-sha-verdict')).toHaveTextContent('当前环境不支持 Web Crypto'),
    );
  });

  it('404 时给出「该批次尚未生成计量表」的空态', async () => {
    mode = 'not-found';
    renderPage(<MeteringPage />, {
      route: `/recon/${BATCH_ID}/metering`,
      path: '/recon/:batchId/metering',
    });

    expect(await screen.findByText('该批次尚未生成计量表')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '← 返回批次' })).toHaveAttribute('href', `/recon/${BATCH_ID}`);
    expect(screen.getByRole('button', { name: '重新检查' })).toBeInTheDocument();
  });

  it('批次详情页提供进入计量表的入口', async () => {
    renderPage(<ReconBatchPage />, {
      route: `/recon/${BATCH_ID}`,
      path: '/recon/:batchId',
    });

    const link = await screen.findByRole('link', { name: '计量表 →' });
    expect(link).toHaveAttribute('href', `/recon/${BATCH_ID}/metering`);
  });
});
