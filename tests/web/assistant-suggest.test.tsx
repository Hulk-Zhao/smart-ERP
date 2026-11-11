import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { AssistantPanel } from '../../web/src/components/AssistantPanel';
import { api } from '../../web/src/api/client';
import { configureApi, resetApiConfig } from '../../web/src/api/config';
import type { SessionOut, SuggestOut } from '../../web/src/api/types';
import { DUAL_TICKET_ID, W3_TICKET_ID } from './helpers/fixtures';
import { renderPage } from './helpers/render';
import { problem, startApi, type CapturedRequest, type TestApi } from './helpers/server';

const SESSION: SessionOut = {
  user_id: 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
  tenant_id: '11111111-1111-1111-1111-111111111111',
  roles: ['reviewer', 'approver'],
  default_scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
};

function llmBody(): SuggestOut {
  return {
    suggestion: '先核对驳回意见，再决定重提或终止。',
    reasons: ['工单当前状态：已驳回', '写等级：W2'],
    source: 'llm',
    model: 'gpt-test',
    generated_at: '2026-11-11T06:00:00Z',
    fallback_reason: null,
  };
}

function ruleBody(): SuggestOut {
  return {
    suggestion: 'W3 动作禁止自动执行，转人工或线下处理。',
    reasons: ['工单当前状态：待审批', '写等级：W3'],
    source: 'rule',
    model: 'rule/next_action-v1',
    generated_at: '2026-11-11T06:01:00Z',
    fallback_reason: 'llm_timeout',
  };
}

function lastRequest(server: TestApi, path: string): CapturedRequest | undefined {
  return [...server.requests].reverse().find((request) => request.path === path);
}

function renderPanel(ticketId: string, path = '/tickets/:ticketId') {
  return renderPage(<AssistantPanel onClose={() => {}} />, {
    route: `/tickets/${ticketId}`,
    path,
  });
}

const servers: TestApi[] = [];

async function startAssistantApi(
  suggest: (request: CapturedRequest) => ReturnType<typeof problem> | Promise<{ body: SuggestOut }>,
): Promise<TestApi> {
  const server = await startApi(async (request) => {
    if (request.path === '/v1/auth/session') return { body: SESSION };
    if (request.path === '/v1/approvals/pending') {
      return { body: { items: [], next_cursor: null } };
    }
    if (request.method === 'POST' && request.path === '/v1/assistant/suggest') {
      return suggest(request);
    }
    return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
  });
  servers.push(server);
  resetApiConfig();
  configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false, scopeSource: 'pending' });
  return server;
}

afterEach(async () => {
  vi.restoreAllMocks();
  resetApiConfig();
  globalThis.localStorage?.clear();
  await Promise.all(servers.splice(0).map((server) => server.close()));
});

describe('助手面板接真实 /v1/assistant/suggest', () => {
  it('llm：显示模型名与生成时间，请求体不带 scope', async () => {
    const server = await startAssistantApi(async () => {
      await new Promise((resolve) => setTimeout(resolve, 80));
      return { body: llmBody() };
    });

    renderPanel(DUAL_TICKET_ID);

    await waitFor(() => expect(lastRequest(server, '/v1/assistant/suggest')).toBeTruthy());
    // 请求已发出、响应未回：必须有独立加载态。
    expect(screen.getAllByTestId('assistant-loading').length).toBeGreaterThan(0);

    await waitFor(() => expect(screen.queryAllByTestId('assistant-suggest').length).toBeGreaterThan(0));
    const card = screen.getAllByTestId('assistant-suggest')[0];
    expect(card.getAttribute('data-source')).toBe('llm');
    expect(screen.getAllByTestId('assistant-source')[0].textContent).toContain('模型建议');
    expect(screen.getAllByTestId('assistant-model')[0].textContent).toBe('gpt-test');
    expect(screen.getAllByTestId('assistant-generated-at')[0].textContent).not.toBe('—');
    expect(screen.queryAllByTestId('assistant-fallback-reason')).toHaveLength(0);

    const request = lastRequest(server, '/v1/assistant/suggest');
    // 后端 SuggestIn 是 extra="forbid"：body 只能是这两个字段。
    expect(request?.body).toEqual({ ticket_id: DUAL_TICKET_ID, kind: 'next_action' });
    expect(request?.body).not.toHaveProperty('scope');
    expect(request?.headers['x-session-token']).toBe('demo-token');
    expect(request?.headers['idempotency-key']).toBeTruthy();
  });

  it('rule：明确标注「规则建议（模型未参与）」+ 降级原因，不显示模型名', async () => {
    await startAssistantApi(() => ({ body: ruleBody() }));

    renderPanel(W3_TICKET_ID);

    await waitFor(() => expect(screen.queryAllByTestId('assistant-suggest').length).toBeGreaterThan(0));
    const card = screen.getAllByTestId('assistant-suggest')[0];
    expect(card.getAttribute('data-source')).toBe('rule');

    const source = screen.getAllByTestId('assistant-source')[0];
    expect(source.textContent).toContain('规则建议');
    expect(source.textContent).toContain('模型未参与');

    const reason = screen.getAllByTestId('assistant-fallback-reason')[0];
    expect(reason.textContent).toContain('模型调用超时');
    // 降级时不能出现模型名，避免让用户误以为是模型给的。
    expect(screen.queryAllByTestId('assistant-model')).toHaveLength(0);
    expect(document.body.textContent).not.toContain('rule/next_action-v1');
  });

  it('429 限流：单独文案，不合并成加载失败', async () => {
    await startAssistantApi(() =>
      problem(429, 'E-CONN-RATE_LIMIT', '每租户每分钟最多 10 次助手建议'),
    );

    renderPanel(DUAL_TICKET_ID);

    await waitFor(() => expect(screen.queryAllByTestId('assistant-error').length).toBeGreaterThan(0));
    const error = screen.getAllByTestId('assistant-error')[0];
    expect(error.getAttribute('data-kind')).toBe('rate-limit');
    expect(error.textContent).toContain('每分钟');
    expect(error.textContent).not.toContain('加载失败');
    expect(screen.getAllByTestId('assistant-retry').length).toBeGreaterThan(0);
  });

  it('超时：TimeoutError 与普通失败分开报', async () => {
    await startAssistantApi(() => ({ body: llmBody() }));
    const original = globalThis.fetch;
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      if (String(input).includes('/v1/assistant/suggest')) {
        return Promise.reject(new DOMException('助手响应超时', 'TimeoutError'));
      }
      return original(input as RequestInfo, init);
    });

    renderPanel(DUAL_TICKET_ID);

    await waitFor(() => expect(screen.queryAllByTestId('assistant-error').length).toBeGreaterThan(0));
    const error = screen.getAllByTestId('assistant-error')[0];
    expect(error.getAttribute('data-kind')).toBe('timeout');
    expect(error.textContent).toContain('超时');
    expect(error.textContent).not.toContain('加载失败');
  });

  it('非工单页：不调用建议接口，也不展示规则建议', async () => {
    const server = await startAssistantApi(() => ({ body: llmBody() }));

    renderPage(<AssistantPanel onClose={() => {}} />, { route: '/approvals', path: '/approvals' });

    await waitFor(() => expect(screen.queryAllByTestId('assistant-no-ticket').length).toBeGreaterThan(0));
    expect(server.requests.some((request) => request.path === '/v1/assistant/suggest')).toBe(false);
  });
});

describe('client 超时', () => {
  it('超过前端等待上限时抛 TimeoutError', async () => {
    const server = await startApi(async (request) => {
      if (request.path === '/v1/auth/session') return { body: SESSION };
      if (request.path === '/v1/assistant/suggest') {
        await new Promise((resolve) => setTimeout(resolve, 300));
        return { body: llmBody() };
      }
      return problem(404, 'E-RESOURCE-NOTFOUND', `unexpected ${request.path}`);
    });
    servers.push(server);
    configureApi({ baseUrl: server.baseUrl, token: 'demo-token', mock: false, scopeSource: 'session' });

    await expect(
      api.suggestNextAction(DUAL_TICKET_ID, { timeoutMs: 30 }),
    ).rejects.toMatchObject({ name: 'TimeoutError' });
  });
});
