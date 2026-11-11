import type { ReactElement } from 'react';
import { fireEvent, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { ApprovalsPage } from '../../web/src/pages/ApprovalsPage';
import { MeteringPage } from '../../web/src/pages/MeteringPage';
import { OverviewPage } from '../../web/src/pages/OverviewPage';
import { ReconBatchPage } from '../../web/src/pages/ReconBatchPage';
import { ReconDiffPage } from '../../web/src/pages/ReconDiffPage';
import { ReconPage } from '../../web/src/pages/ReconPage';
import { TicketDetailPage } from '../../web/src/pages/TicketDetailPage';
import { TicketsPage } from '../../web/src/pages/TicketsPage';
import {
  auditChain,
  BATCH_ID,
  DIFF_ID,
  meteringStatement,
  OTHER_APPROVER_ID,
  reconBatch,
  reconDiffDetail,
  ticketDetail,
  W3_TICKET_ID,
} from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type CapturedRequest, type Reply, type TestApi } from './helpers/server';

type Mode = 'forbidden' | 'notfound' | 'conflict' | 'config' | 'network' | 'empty';
type ErrorMode = Exclude<Mode, 'empty' | 'network'>;

const MODES: Mode[] = ['forbidden', 'notfound', 'conflict', 'config', 'network', 'empty'];

interface PageCase {
  name: string;
  route: string;
  path: string;
  render: () => ReactElement;
  /** 对账页只有提交后才发请求。 */
  interaction?: 'recon-submit';
  emptyVariant: 'no-data' | 'not-run' | 'no-diff';
  emptyText: string;
  /** 空态格也必须给出可点击的下一步（筛选/刷新/返回/发起对账）。 */
  emptyAction: { role: 'button' | 'link'; name: string };
}

const PAGES: PageCase[] = [
  {
    name: '工作台',
    route: '/',
    path: '/',
    render: () => <OverviewPage />,
    emptyVariant: 'no-data',
    emptyText: '本期确实无数据',
    emptyAction: { role: 'button', name: '刷新' },
  },
  {
    name: '工单列表',
    route: '/tickets',
    path: '/tickets',
    render: () => <TicketsPage />,
    emptyVariant: 'no-data',
    emptyText: '本期确实无数据',
    emptyAction: { role: 'button', name: '全部' },
  },
  {
    name: '工单详情',
    route: `/tickets/${W3_TICKET_ID}`,
    path: '/tickets/:ticketId',
    render: () => <TicketDetailPage />,
    emptyVariant: 'no-data',
    emptyText: '本期确实无数据',
    emptyAction: { role: 'button', name: '刷新' },
  },
  {
    name: '审批待办',
    route: '/approvals',
    path: '/approvals',
    render: () => <ApprovalsPage />,
    emptyVariant: 'no-data',
    emptyText: '本期确实无数据',
    emptyAction: { role: 'button', name: '刷新' },
  },
  {
    name: '对账',
    route: '/recon',
    path: '/recon',
    render: () => <ReconPage />,
    interaction: 'recon-submit',
    emptyVariant: 'not-run',
    emptyText: '还没跑过对账',
    emptyAction: { role: 'button', name: '发起对账' },
  },
  {
    name: '批次详情',
    route: `/recon/${BATCH_ID}`,
    path: '/recon/:batchId',
    render: () => <ReconBatchPage />,
    emptyVariant: 'no-diff',
    emptyText: '当前筛选下没有差异行',
    emptyAction: { role: 'button', name: '全部类型' },
  },
  {
    name: '差异明细',
    route: `/recon/${BATCH_ID}/diffs/${DIFF_ID}`,
    path: '/recon/:batchId/diffs/:diffId',
    render: () => <ReconDiffPage />,
    emptyVariant: 'no-data',
    emptyText: '本期确实无数据',
    emptyAction: { role: 'link', name: '← 返回批次差异列表' },
  },
  {
    name: '计量表',
    route: `/recon/${BATCH_ID}/metering`,
    path: '/recon/:batchId/metering',
    render: () => <MeteringPage />,
    emptyVariant: 'no-data',
    emptyText: '该批次没有计量行',
    emptyAction: { role: 'link', name: '← 返回批次' },
  },
];

let activeServer: TestApi | null = null;

function errorReply(mode: ErrorMode): Reply {
  if (mode === 'forbidden') {
    return problem(403, 'E-AUTHZ-001', '当前身份缺少所需角色或 scope', {
      required_roles: ['reviewer'],
      missing_roles: ['reviewer'],
    });
  }
  if (mode === 'notfound') return problem(404, 'E-RESOURCE-NOTFOUND', 'resource not found');
  if (mode === 'conflict') return problem(409, 'E-STATE-ILLEGAL', '当前状态不允许该迁移');
  return problem(503, 'E-DATA-MISSING', '单价未配置：未设置 ERP_METERING_UNIT_PRICE');
}

function emptyReply(pageName: string, request: CapturedRequest): Reply {
  const { path } = request;
  if (path === '/v1/auth/session') {
    return {
      body: {
        user_id: OTHER_APPROVER_ID,
        tenant_id: '11111111-1111-1111-1111-111111111111',
        roles: ['reviewer'],
        default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
      },
    };
  }
  if (path === '/v1/tickets' || path === '/v1/approvals/pending') {
    return { body: { items: [], next_cursor: null } };
  }
  if (path === '/v1/recon/batches') {
    return { body: { items: [], next_cursor: null } };
  }
  if (pageName === '工单详情') {
    if (path === `/v1/tickets/${W3_TICKET_ID}`) {
      return {
        body: ticketDetail({
          steps: [],
          approvals: [],
          approval_required_roles: [],
          approval_missing_roles: [],
          execution_required_roles: [],
          execution_missing_roles: [],
          audit_image_count: 0,
        }),
      };
    }
    if (path.endsWith('/images')) return { body: { items: [], next_cursor: null } };
    if (path.endsWith('/chain')) return { body: auditChain() };
  }
  if (pageName === '批次详情') {
    if (path === `/v1/recon/runs/${BATCH_ID}`) return { body: reconBatch({ diff_count: 0 }) };
    if (path.endsWith('/diffs')) return { body: { items: [], next_cursor: null } };
  }
  if (pageName === '差异明细' && path === `/v1/recon/runs/${BATCH_ID}/diffs/${DIFF_ID}`) {
    return { body: reconDiffDetail({ evidence: [], related_lines: [], draft: null, unattributed: null }) };
  }
  if (pageName === '计量表' && path.endsWith('/statement')) {
    return { body: meteringStatement({ lines: [], line_count: 0 }) };
  }
  return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${path}`);
}

function configure(baseUrl: string): void {
  configureApi({
    baseUrl,
    token: 'token-error-states',
    mock: false,
    roles: ['reviewer'],
    viewerId: '',
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
}

async function firstPanel(kind: string): Promise<HTMLElement> {
  const panels = await screen.findAllByTestId('error-panel');
  const panel = panels[0];
  expect(panel).toHaveAttribute('data-kind', kind);
  // 390px：统一错误态必须可收缩换行，不允许固定宽度。
  expect(panel.className).toContain('min-w-0');
  expect(panel.className).toContain('max-w-full');
  return panel;
}

afterEach(async () => {
  if (activeServer) {
    await activeServer.close();
    activeServer = null;
  }
  resetApiConfig();
  globalThis.localStorage?.clear();
});

describe.each(PAGES)('$name：6 类错误态与空态', (page) => {
  it.each(MODES)('%s 有统一文案与可点击动作', async (mode) => {
    resetApiConfig();
    globalThis.localStorage?.clear();

    if (mode === 'network') {
      configure('http://127.0.0.1:1');
    } else {
      activeServer = await startApi((request) =>
        mode === 'empty' ? emptyReply(page.name, request) : errorReply(mode),
      );
      configure(activeServer.baseUrl);
    }

    renderPage(page.render(), { route: page.route, path: page.path });

    if (page.interaction && mode !== 'empty') {
      fireEvent.change(await screen.findByPlaceholderText('S-001, S-002'), { target: { value: 'S-001' } });
      fireEvent.click(screen.getByRole('button', { name: '发起对账' }));
    }

    if (mode === 'forbidden') {
      const panel = await firstPanel('forbidden');
      expect(within(panel).getByText('缺少所需角色或 scope')).toBeInTheDocument();
      expect(within(panel).getByRole('button', { name: '打开连接设置' })).toBeInTheDocument();
      return;
    }

    if (mode === 'notfound') {
      const panel = await firstPanel('notfound');
      expect(within(panel).getAllByText(/不存在|尚未生成计量表|已被清理/).length).toBeGreaterThan(0);
      expect(within(panel).getByRole('button', { name: '重新检查' })).toBeInTheDocument();
      return;
    }

    if (mode === 'conflict') {
      const panel = await firstPanel('conflict');
      expect(within(panel).getByText('当前状态不允许该操作')).toBeInTheDocument();
      expect(within(panel).getByRole('button', { name: '刷新' })).toBeInTheDocument();
      return;
    }

    if (mode === 'config') {
      const panel = await firstPanel('config');
      expect(within(panel).getAllByText(/ERP_METERING_UNIT_PRICE/).length).toBeGreaterThan(0);
      // 配置问题不是故障：不能给「重试」，避免用户反复点击。
      expect(within(panel).queryByRole('button', { name: /重试|重新检查|刷新/ })).toBeNull();
      return;
    }

    if (mode === 'network') {
      const panel = await firstPanel('network');
      expect(within(panel).getByText('网络中断或请求超时')).toBeInTheDocument();
      expect(within(panel).getByRole('button', { name: '重试' })).toBeInTheDocument();
      return;
    }

    const empties = await screen.findAllByTestId('empty-state');
    const expected = empties.filter((element) => element.getAttribute('data-variant') === page.emptyVariant);
    expect(expected.length).toBeGreaterThan(0);
    for (const element of expected) {
      expect(element.className).toContain('min-w-0');
      expect(element.className).toContain('max-w-full');
    }
    expect(screen.getAllByText(new RegExp(page.emptyText)).length).toBeGreaterThan(0);
    // 空态也有可点击的下一步：筛选、刷新、返回或发起对账。
    expect(screen.getByRole(page.emptyAction.role, { name: page.emptyAction.name })).toBeInTheDocument();
  });
});
