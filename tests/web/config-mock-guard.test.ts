import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * mock 只能由构建期 ``VITE_USE_MOCK=true`` 打开。
 * localStorage 里旧版本/手工留下的 ``mock:true`` 不能把演示页面切到 mock.ts 的假数据上。
 */
describe('mock 开关只认构建期变量', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetModules();
  });

  it('localStorage 残留 mock:true 不生效', async () => {
    localStorage.setItem(
      'smart-erp.web.config.v1',
      JSON.stringify({ mock: true, baseUrl: '/erp/api', token: 'stale-token', viewerName: '旧配置' }),
    );

    const { getApiConfig } = await import('../../web/src/api/config');
    expect(getApiConfig().mock).toBe(false);
    // 其他配置仍按旧值恢复，只有 mock 被忽略。
    expect(getApiConfig().token).toBe('stale-token');
  });
});
