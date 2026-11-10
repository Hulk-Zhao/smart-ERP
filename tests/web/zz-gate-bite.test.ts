import { describe, expect, it } from 'vitest';

// 临时用例：证明 CI 前端 job 的「单元测试」步骤会因断言失败而变红，验证后删除。
describe('CI 门禁自检（临时）', () => {
  it('必然失败的断言', () => {
    expect(1).toBe(2);
  });
});
