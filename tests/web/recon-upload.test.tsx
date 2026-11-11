import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { ReconBatchPage } from '../../web/src/pages/ReconBatchPage';
import { ReconPage } from '../../web/src/pages/ReconPage';
import { BATCH_ID, reconBatch, reconDiff } from './helpers/fixtures';
import { problem, startApi, type TestApi } from './helpers/server';

const SESSION = {
  user_id: 'aaaaaaaa-0000-4000-8000-000000000000',
  tenant_id: 'T-1',
  roles: ['operator'],
  default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
};

/** 上传演示批次：3 行全部匹配、其中 1 行单价差异（matched 是行级，不是 2）。 */
const UPLOADED = reconBatch({
  batch_id: BATCH_ID,
  status: '待人工复核',
  conclusive: false,
  suppliers: ['SUP-DEMO-01'],
  diff_count: 1,
  metrics: {
    supplier_lines: 3,
    matched: 3,
    matched_rate: '1.0000',
    ambiguous: 0,
    candidate_only: 0,
    unmatched: 0,
    diff_lines: 1,
    diff_rate: '0.333333',
    diff_amount: '125.0000',
    suspected_anomaly: false,
    auto_disposal_allowed: false,
  },
});

const PARSED = { statement_file: 'supplier_statement_2026-10.csv', supplier_lines: 3, our_lines: 3 };

let server: TestApi;
let mode: 'ok' | 'parse-error' | 'run-error';

beforeEach(async () => {
  mode = 'ok';
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === '/v1/auth/session') return { body: SESSION };
    if (request.method === 'GET' && request.path === '/v1/recon/batches') {
      return { body: { items: [], next_cursor: null } };
    }
    if (request.method === 'POST' && request.path === '/v1/recon/statements') {
      if (mode === 'parse-error') {
        return problem(422, 'E-PARSE-FAILED', '第 3 行：单位「KGX」不认识，整文件拒收');
      }
      return { status: 201, body: PARSED };
    }
    if (request.method === 'POST' && request.path === '/v1/recon/runs') {
      if (mode === 'run-error') return problem(500, 'E-INTERNAL', '引擎内部错误：batch 写入失败');
      return { status: 201, body: UPLOADED };
    }
    if (request.method === 'GET' && request.path === `/v1/recon/runs/${BATCH_ID}`) {
      return { body: UPLOADED };
    }
    if (request.method === 'GET' && request.path === `/v1/recon/runs/${BATCH_ID}/diffs`) {
      return { body: { items: [reconDiff({ type: 'unit_price' })], next_cursor: null } };
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });

  configureApi({
    baseUrl: server.baseUrl,
    token: 'token-upload',
    mock: false,
    roles: ['operator'],
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  await server.close();
  resetApiConfig();
});

function selectCsv(): void {
  const file = new File(['doc_no,line_no,unit_price\nPO-2026-0001,1,12.5\n'], 'supplier_statement_2026-10.csv', {
    type: 'text/csv',
  });
  fireEvent.change(screen.getByTestId('recon-upload-file'), { target: { files: [file] } });
}

/** 上传成功会 navigate 到批次详情，测试必须同时挂两条路由，否则跳转后整棵树被卸载。 */
function renderUploadFlow(): void {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/recon']}>
        <Routes>
          <Route path="/recon" element={<ReconPage />} />
          <Route path="/recon/:batchId" element={<ReconBatchPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('对账页上传：CSV → 开始对账 → 批次详情', () => {
  it('上传成功显示服务端返回的 matched=3 / diff_lines=1，并跳到批次详情', async () => {
    renderUploadFlow();

    selectCsv();
    fireEvent.click(screen.getByRole('button', { name: '开始对账' }));

    const caliber = await screen.findByTestId('matched-caliber');
    expect(caliber).toHaveTextContent('3 行匹配（行级），其中 1 行存在差异');
    // 口径判别：行级匹配不因 1 行差异变成 2。
    expect(caliber).not.toHaveTextContent('2 行匹配');
    expect(screen.queryByText(/2 条匹配/)).toBeNull();
    expect(screen.getByText('差异列表')).toBeInTheDocument();

    const upload = server.requests.find((item) => item.path === '/v1/recon/statements');
    expect(upload?.rawBody).toContain('name="file"');
    expect(upload?.rawBody).toContain('filename="supplier_statement_2026-10.csv"');
    expect(upload?.rawBody).toContain('name="dataset"');
    // our_lines 来自 fixtures（3 行），不是前端拼接的假数据。
    expect(upload?.rawBody).toContain('"supplier_code":"SUP-DEMO-01"');
    expect((upload?.rawBody ?? '').match(/"doc_no"/g)?.length).toBe(3);

    const run = server.requests.find((item) => item.path === '/v1/recon/runs');
    expect(run?.body).toMatchObject({
      suppliers: ['SUP-DEMO-01'],
      sources: { statement_files: ['supplier_statement_2026-10.csv'] },
      dataset: null,
      scope: { org_id: 'ORG-1', period: '2026-10' },
    });
  });

  it('422 解析失败：原样展示带行号的 detail，不跳转也不伪装成功', async () => {
    mode = 'parse-error';
    renderUploadFlow();

    selectCsv();
    fireEvent.click(screen.getByRole('button', { name: '开始对账' }));

    await waitFor(() => {
      expect(screen.getByText('第 3 行：单位「KGX」不认识，整文件拒收')).toBeInTheDocument();
    });
    expect(screen.queryByText('差异列表')).toBeNull();
    expect(screen.queryByText(/开始对账/)).toBeInTheDocument();
    expect(server.requests.some((item) => item.path === '/v1/recon/runs')).toBe(false);
  });

  it('引擎 500：显示错误而不是空态或成功', async () => {
    mode = 'run-error';
    renderUploadFlow();

    selectCsv();
    fireEvent.click(screen.getByRole('button', { name: '开始对账' }));

    await waitFor(() => {
      expect(screen.getByText('引擎内部错误：batch 写入失败')).toBeInTheDocument();
    });
    expect(screen.queryByText('差异列表')).toBeNull();
    expect(screen.queryByText('暂无批次')).toBeNull();
  });
});

