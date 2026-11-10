// @vitest-environment node
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// npm 只有在 `--` 之后的参数才会传给脚本：`npm run gen:api --check` 会把 --check 吞掉，
// 脚本按无参数执行并重写生成物，看起来通过、实际是静默覆盖。这条断言把写法钉死在 scripts 上。
const WEB_ROOT = fileURLToPath(new URL('../../web', import.meta.url));
const packageJson = JSON.parse(readFileSync(`${WEB_ROOT}/package.json`, 'utf8')) as {
  scripts: Record<string, string>;
};
const readme = readFileSync(`${WEB_ROOT}/README.md`, 'utf8');

describe('web/package.json 的校验入口', () => {
  it('gen:api 只负责生成，校验必须带 `--` 转发 --check', () => {
    expect(packageJson.scripts['gen:api']).toBe('node scripts/gen-api-types.mjs');
    expect(packageJson.scripts.build).toContain('npm run gen:api -- --check');
    expect(packageJson.scripts['check:api']).toBe('npm run gen:api -- --check');
  });

  it('不存在漏写 `--` 的脚本（npm run gen:api --check）', () => {
    for (const [name, command] of Object.entries(packageJson.scripts)) {
      expect(command, `scripts.${name}`).not.toMatch(/gen:api\s+--check\b/);
    }
  });

  it('README 顶部写死同一条命令', () => {
    expect(readme).toContain('npm run gen:api -- --check');
  });
});
