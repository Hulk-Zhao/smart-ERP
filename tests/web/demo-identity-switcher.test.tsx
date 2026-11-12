import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { configureApi, getApiConfig, resetApiConfig } from '../../web/src/api/config';
import { renderShell } from './helpers/render';
import { problem, startApi, type TestApi } from './helpers/server';

const TOKEN_A = 'demo-operator-token-0001';
const TOKEN_D = 'demo-cfo-token-000000001';
const USER_A = 'aaaaaaaa-0000-4000-8000-000000000001';
const USER_D = 'dddddddd-0000-4000-8000-000000000004';

const SESSION = {
  user_id: USER_A,
  tenant_id: 'T-1',
  roles: ['operator'],
  default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
};

const IDENTITIES = {
  items: [
    { user_id: USER_A, roles: ['operator'], label: 'aaaaaaaa · operator', token: TOKEN_A },
    { user_id: USER_D, roles: ['approver', 'cfo', 'reviewer'], label: 'dddddddd · approver/cfo/reviewer', token: TOKEN_D },
  ],
};

let server: TestApi;
let demoEndpoint: 'ok' | 'disabled';

beforeEach(async () => {
  demoEndpoint = 'ok';
  resetApiConfig();
  server = await startApi((request) => {
    if (request.method === 'GET' && request.path === '/v1/auth/session') return { body: SESSION };
    if (request.method === 'GET' && request.path === '/v1/auth/demo-identities') {
      return demoEndpoint === 'ok'
        ? { body: IDENTITIES }
        : problem(404, 'HTTP-404', '演示身份切换未开启');
    }
    if (request.method === 'GET' && request.path === '/v1/approvals/pending') {
      return { body: { items: [], next_cursor: null } };
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.method} ${request.path}`);
  });
  configureApi({
    baseUrl: server.baseUrl,
    token: TOKEN_A,
    mock: false,
    roles: ['operator'],
    scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
  });
});

afterEach(async () => {
  await server.close();
  resetApiConfig();
});

describe('演示身份切换器', () => {
  it('列出身份并标注演示用途；点击只换令牌并重取会话', async () => {
    renderShell(<div data-testid="page">page</div>, { route: '/' });

    fireEvent.click(await screen.findByTestId('demo-identity-toggle'));
    expect(await screen.findByText(/演示身份切换，仅用于演示环境/)).toBeInTheDocument();

    const options = await screen.findAllByTestId('demo-identity-option');
    expect(options).toHaveLength(2);
    expect(options[1]).toHaveTextContent('dddddddd · approver/cfo/reviewer');

    fireEvent.click(options[1]);

    await waitFor(() => expect(getApiConfig().token).toBe(TOKEN_D));
    // 会话按新令牌重取：不是前端伪造角色，而是让后端重新判身份。
    await waitFor(() => {
      const sessions = server.requests.filter((item) => item.path === '/v1/auth/session');
      expect(sessions.at(-1)?.headers['x-session-token']).toBe(TOKEN_D);
    });
  });

  it('非 demo 环境端点 404：如实显示未开启，不伪造身份列表', async () => {
    demoEndpoint = 'disabled';
    renderShell(<div data-testid="page">page</div>, { route: '/' });

    fireEvent.click(await screen.findByTestId('demo-identity-toggle'));

    expect(await screen.findByTestId('demo-identity-unavailable')).toHaveTextContent('未开启演示身份接口');
    expect(screen.queryAllByTestId('demo-identity-option')).toHaveLength(0);
    expect(getApiConfig().token).toBe(TOKEN_A);
  });
});
