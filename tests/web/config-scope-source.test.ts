import { beforeEach, describe, expect, it, vi } from 'vitest';

const STORAGE_KEY = 'smart-erp.web.config.v1';

/** 每次都拿一份重新初始化的 config 模块，模拟「刷新页面时读 localStorage」。 */
async function freshConfig() {
  vi.resetModules();
  return import('../../web/src/api/config');
}

describe('scope 来源：旧存储里的 ORG-1 不算已确认范围', () => {
  beforeEach(() => {
    globalThis.localStorage?.clear();
  });

  it('旧版本只写了 scope、没有 scopeManual → pending，而不是 manual', async () => {
    globalThis.localStorage?.setItem(
      STORAGE_KEY,
      JSON.stringify({
        baseUrl: '/erp/api',
        token: 'old-token',
        scope: { org_id: 'ORG-1', period: '2026-10', system_ids: ['u8', 'statement'] },
      }),
    );

    const config = await freshConfig();
    const loaded = config.getApiConfig();

    // 令牌照常恢复；scope 不能因为「存过」就当用户确认过。
    expect(loaded.token).toBe('old-token');
    expect(loaded.scopeSource).toBe('pending');
  });

  it('带 scopeManual 标记才恢复为用户确认的 manual 范围', async () => {
    globalThis.localStorage?.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token: 't',
        scope: { org_id: 'ORG-9', period: '2026-08', system_ids: ['u8'] },
        scopeManual: true,
      }),
    );

    const config = await freshConfig();
    const loaded = config.getApiConfig();

    expect(loaded.scopeSource).toBe('manual');
    expect(loaded.scope).toEqual({ org_id: 'ORG-9', period: '2026-08', system_ids: ['u8'] });
  });

  it('会话带出的范围不落本地存储，下次启动继续跟随令牌', async () => {
    const config = await freshConfig();
    config.configureApi({
      baseUrl: '/erp/api',
      token: 'session-token',
      scope: { org_id: 'ORG-7', period: '2026-09', system_ids: ['u8'] },
      scopeSource: 'session',
    });

    const saved = JSON.parse(globalThis.localStorage?.getItem(STORAGE_KEY) ?? '{}');
    expect(saved.token).toBe('session-token');
    expect(saved.scope).toBeUndefined();
    expect(saved.scopeManual).toBeUndefined();
  });

  it('用户确认（manual）后才持久化 scope', async () => {
    const config = await freshConfig();
    config.configureApi({
      token: 't',
      scope: { org_id: 'ORG-3', period: '2026-07', system_ids: ['u8'] },
      scopeSource: 'manual',
    });

    const saved = JSON.parse(globalThis.localStorage?.getItem(STORAGE_KEY) ?? '{}');
    expect(saved.scope).toEqual({ org_id: 'ORG-3', period: '2026-07', system_ids: ['u8'] });
    expect(saved.scopeManual).toBe(true);
  });
});
