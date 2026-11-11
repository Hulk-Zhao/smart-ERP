import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import { OverviewPage } from '../../web/src/pages/OverviewPage';
import { TicketsPage } from '../../web/src/pages/TicketsPage';
import { approvalTask } from './helpers/fixtures';
import { renderShell } from './helpers/render';
import { problem, startApi, type Reply, type TestApi } from './helpers/server';

const TENANT_ID = '11111111-1111-1111-1111-111111111111';
const REVIEWER_ID = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb';

function kpi(label: string): HTMLElement {
  const node = document.querySelector(`[data-kpi="${label}"]`);
  if (!(node instanceof HTMLElement)) throw new Error(`KPI ${label} 未渲染`);
  return node;
}

/** 会话端点：没有 X-Session-Token 一律 401；带令牌时按令牌返回对应分支。 */
async function bootApi(sessionForToken: (token: string) => Reply): Promise<TestApi> {
  const server = await startApi((request) => {
    const token = String(request.headers['x-session-token'] ?? '');
    if (!token) {
      return problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）');
    }
    if (request.path === '/v1/auth/session') return sessionForToken(token);
    if (request.path === '/v1/approvals/pending') {
      return { body: { items: [approvalTask()], next_cursor: null } };
    }
    if (request.path === '/v1/tickets') return { body: { items: [], next_cursor: null } };
    if (request.path === '/v1/recon/batches') return { body: { items: [], next_cursor: null } };
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
  });
  resetApiConfig();
  configureApi({ baseUrl: server.baseUrl, token: '', mock: false });
  return server;
}

async function openSettings(): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole('button', { name: '连接设置' }));
  return screen.findByTestId('connection-settings');
}

/** 用户粘令牌：失焦即探测会话，默认范围随令牌返回。 */
async function pasteToken(value: string): Promise<void> {
  const input = screen.getByTestId('session-token');
  fireEvent.change(input, { target: { value } });
  fireEvent.blur(input);
}

async function waitScopeState(state: string): Promise<HTMLElement> {
  const node = await screen.findByTestId('scope-source');
  await waitFor(() => expect(node).toHaveAttribute('data-state', state));
  return node;
}

function sessionBody(defaultScope: unknown): Reply {
  return {
    body: {
      user_id: REVIEWER_ID,
      tenant_id: TENANT_ID,
      roles: ['reviewer'],
      ...(defaultScope === undefined ? {} : { default_scope: defaultScope }),
    },
  };
}

function paths(server: TestApi): string[] {
  return server.requests.map((request) => request.path);
}

function lastRequest(server: TestApi, path: string) {
  return server.requests.filter((request) => request.path === path).at(-1);
}

describe('default_scope 齐全：只粘一次令牌', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await bootApi(() =>
      sessionBody({ org_id: 'ORG-7', period: '2026-09', system_ids: ['u8'] }),
    );
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('三项自动带出且可见可改，保存后工作台出现「待你决策」', async () => {
    renderShell(<OverviewPage />);
    await openSettings();

    // 没令牌时是 401：明确说「当前无法获取默认范围」，而不是假装 ORG-1 就是对的。
    const before = await waitScopeState('unavailable');
    expect(before.textContent).toContain('当前无法获取默认范围');
    expect(paths(server)).not.toContain('/v1/tickets');

    await pasteToken('demo-token');

    const ready = await waitScopeState('ready');
    expect(ready.textContent).toContain('已从会话带出');
    await waitFor(() => expect(screen.getByTestId('scope-org')).toHaveValue('ORG-7'));
    expect(screen.getByTestId('scope-period')).toHaveValue('2026-09');
    expect(screen.getByTestId('scope-systems')).toHaveValue('u8');
    // 可见、可改：自动带出不是只读展示。
    expect(screen.getByTestId('scope-org')).not.toBeDisabled();
    expect(screen.getByTestId('scope-period')).not.toBeDisabled();

    // 面板还开着、用户还没保存：页面业务查询必须全部暂缓，只允许会话请求。
    expect(paths(server)).not.toContain('/v1/approvals/pending');
    expect(paths(server)).not.toContain('/v1/tickets');
    expect(paths(server)).not.toContain('/v1/recon/batches');

    // 交互到此为止：粘令牌 + 保存，共 2 次。
    fireEvent.click(screen.getByTestId('save-settings'));
    await waitFor(() => expect(screen.queryByTestId('connection-settings')).toBeNull());
    await waitFor(() => expect(within(kpi('待你决策')).getByText('1')).toBeTruthy());

    const pending = lastRequest(server, '/v1/approvals/pending');
    expect(pending?.query.org_id).toEqual(['ORG-7']);
    expect(pending?.query.period).toEqual(['2026-09']);
    expect(pending?.headers['x-session-token']).toBe('demo-token');
  });
});

describe('default_scope.org_id 为 null：进入选择态', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await bootApi(() =>
      sessionBody({ org_id: null, period: '2026-10', system_ids: ['u8', 'statement'] }),
    );
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('org_id 留空待选、保存被拦，且不用占位 org 打业务接口', async () => {
    renderShell(<OverviewPage />);
    await openSettings();

    await pasteToken('multi-org-token');
    const state = await waitScopeState('multi-org');
    expect(state.textContent).toContain('授权了多个组织');

    await waitFor(() => expect(screen.getByTestId('scope-org')).toHaveValue(''));
    expect(screen.getByTestId('scope-org')).toHaveAttribute('placeholder', '请选择 / 填写要使用的 org_id');
    // 服务端能给的另外两项照常带出，只有 org 留给用户。
    expect(screen.getByTestId('scope-period')).toHaveValue('2026-10');
    expect(screen.getByTestId('save-settings')).toBeDisabled();
    expect(screen.getByTestId('save-blocked')).toBeTruthy();
    // 顶栏也显示「未选择组织」，不显示 ORG-1。
    expect(screen.getByTestId('scope-chip').textContent).toContain('未选择组织');

    // 选择态挡在页面之前：没有用 ORG-1 去请求工单/待办。
    expect(paths(server)).not.toContain('/v1/tickets');
    expect(paths(server)).not.toContain('/v1/approvals/pending');

    fireEvent.change(screen.getByTestId('scope-org'), { target: { value: 'ORG-2' } });
    expect(screen.getByTestId('save-settings')).not.toBeDisabled();
    fireEvent.click(screen.getByTestId('save-settings'));

    await waitFor(() => expect(within(kpi('待你决策')).getByText('1')).toBeTruthy());
    expect(lastRequest(server, '/v1/approvals/pending')?.query.org_id).toEqual(['ORG-2']);
  });
});

describe('default_scope 为 null / 端点未启用：明确提示并可手填', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await bootApi(() => sessionBody(undefined));
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('提示「当前无法获取默认范围」，三项保留可改，手填保存后照常加载', async () => {
    renderShell(<OverviewPage />);
    await openSettings();

    await pasteToken('no-scope-token');
    const state = await waitScopeState('missing');
    expect(state.textContent).toContain('当前无法获取默认范围');
    expect(state.textContent).toContain('default_scope');

    // 不留三个空格子：保留现有值（环境变量/上次填写），并允许直接改。
    expect(screen.getByTestId('scope-org')).toHaveValue('ORG-1');
    expect(screen.getByTestId('scope-period')).toHaveValue('2026-10');
    expect(screen.getByTestId('scope-systems')).toHaveValue('u8, statement');
    expect(screen.getByTestId('scope-org')).not.toBeDisabled();
    expect(screen.getByTestId('save-settings')).not.toBeDisabled();

    // 关键：ORG-1 只是未确认的旧默认值，未经用户保存不得当作可用范围发业务请求。
    expect(screen.getByTestId('scope-gate')).toHaveAttribute('data-state', 'missing-scope');
    expect(paths(server)).not.toContain('/v1/tickets');
    expect(paths(server)).not.toContain('/v1/approvals/pending');
    expect(paths(server)).not.toContain('/v1/recon/batches');

    // 保存即确认：这一步之后 ORG-1 才是用户认可的范围。
    fireEvent.click(screen.getByTestId('save-settings'));
    await waitFor(() => expect(within(kpi('待你决策')).getByText('1')).toBeTruthy());
    expect(lastRequest(server, '/v1/approvals/pending')?.query.org_id).toEqual(['ORG-1']);
  });
});

describe('default_scope.period 为 null：不猜账期', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await bootApi(() =>
      sessionBody({ org_id: 'ORG-7', period: null, system_ids: ['u8'] }),
    );
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('period 留空并说明原因，org_id 照常带出', async () => {
    renderShell(<OverviewPage />);
    await openSettings();

    await pasteToken('no-period-token');
    const state = await waitScopeState('missing-period');
    expect(state.textContent).toContain('period 为 null');
    await waitFor(() => expect(screen.getByTestId('scope-org')).toHaveValue('ORG-7'));
    expect(screen.getByTestId('scope-period')).toHaveValue('');
    expect(screen.getByTestId('save-settings')).toBeDisabled();
    expect(paths(server)).not.toContain('/v1/tickets');
  });
});

describe('冷刷新 /tickets：第一条查询就带会话范围', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await startApi((request) => {
      const token = String(request.headers['x-session-token'] ?? '');
      if (request.path === '/v1/auth/session') {
        return token === 'demo-token'
          ? sessionBody({ org_id: 'ORG-7', period: '2026-09', system_ids: ['u8'] })
          : problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）');
      }
      if (request.path === '/v1/tickets') {
        // 真实后端按 G-05 校验范围：用占位 ORG-1 打过来就是 403。
        if (request.query.org_id?.[0] !== 'ORG-7') {
          return problem(403, 'E-AUTHZ-003', `org ${request.query.org_id?.[0] ?? '空'} 不在授权范围`);
        }
        return { body: { items: [], next_cursor: null } };
      }
      return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
    });
    resetApiConfig();
    // 模拟「上次已保存令牌」后冷刷新：令牌在，范围还没由会话确认。
    configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false, scopeSource: 'pending' });
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('不发默认 ORG-1 的 403，只发会话带出的 ORG-7', async () => {
    renderShell(<TicketsPage />, { route: '/tickets' });

    await waitFor(() => expect(paths(server)).toContain('/v1/tickets'));

    const ticketsRequests = server.requests.filter((request) => request.path === '/v1/tickets');
    expect(ticketsRequests.length).toBeGreaterThan(0);
    for (const request of ticketsRequests) {
      expect(request.query.org_id).toEqual(['ORG-7']);
      expect(request.query.period).toEqual(['2026-09']);
      expect(request.headers['x-session-token']).toBe('demo-token');
    }
  });
});

describe('粘贴令牌后直接保存：不依赖 blur 时序', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await bootApi((token) =>
      token === 'demo-token'
        ? sessionBody({ org_id: 'ORG-7', period: '2026-09', system_ids: ['u8'] })
        : problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）'),
    );
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('只 change 不 blur 直接点保存，仍按会话范围加载', async () => {
    renderShell(<OverviewPage />);
    await openSettings();
    await waitScopeState('unavailable');

    fireEvent.change(screen.getByTestId('session-token'), { target: { value: 'demo-token' } });
    // 关键：不失焦、不等 400ms 去抖，直接保存；正确性由保存路径自己取会话保证。
    fireEvent.click(screen.getByTestId('save-settings'));

    await waitFor(() => expect(screen.queryByTestId('connection-settings')).toBeNull());
    await waitFor(() => expect(within(kpi('待你决策')).getByText('1')).toBeTruthy());

    const pending = lastRequest(server, '/v1/approvals/pending');
    expect(pending?.query.org_id).toEqual(['ORG-7']);
    expect(pending?.query.period).toEqual(['2026-09']);
    expect(pending?.headers['x-session-token']).toBe('demo-token');
    // 全程没有用旧的 ORG-1 发过任何待办请求。
    const pendingRequests = server.requests.filter((request) => request.path === '/v1/approvals/pending');
    expect(pendingRequests.every((request) => request.query.org_id?.[0] === 'ORG-7')).toBe(true);
  });
});

describe('多组织令牌 + 立刻保存：先取会话再决定，不替用户猜', () => {
  let server: TestApi;

  beforeEach(async () => {
    globalThis.localStorage?.clear();
    server = await bootApi((token) =>
      token === 'multi-org-token'
        ? sessionBody({ org_id: null, period: '2026-10', system_ids: ['u8', 'statement'] })
        : problem(401, 'E-AUTHZ-001', '缺少会话令牌（请求头 X-Session-Token）'),
    );
  });

  afterEach(async () => {
    resetApiConfig();
    globalThis.localStorage?.clear();
    await server.close();
  });

  it('保存时发现 org_id 为 null：面板保持打开进入选择态，补齐后才加载', async () => {
    renderShell(<OverviewPage />);
    await openSettings();
    await waitScopeState('unavailable');

    fireEvent.change(screen.getByTestId('session-token'), { target: { value: 'multi-org-token' } });
    fireEvent.click(screen.getByTestId('save-settings'));

    const state = await waitScopeState('multi-org');
    expect(state.textContent).toContain('授权了多个组织');
    await waitFor(() => expect(screen.getByTestId('scope-org')).toHaveValue(''));
    expect(screen.getByTestId('connection-settings')).toBeTruthy();
    expect(screen.getByTestId('save-settings')).toBeDisabled();
    expect(paths(server)).not.toContain('/v1/approvals/pending');

    fireEvent.change(screen.getByTestId('scope-org'), { target: { value: 'ORG-2' } });
    fireEvent.click(screen.getByTestId('save-settings'));
    await waitFor(() => expect(within(kpi('待你决策')).getByText('1')).toBeTruthy());
    expect(lastRequest(server, '/v1/approvals/pending')?.query.org_id).toEqual(['ORG-2']);
  });
});
