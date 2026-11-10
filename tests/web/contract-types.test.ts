// @vitest-environment node
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { CONTRACT_PATH_COUNT, CONTRACT_SHA256, DIFF_TYPES, TICKET_STATES } from '../../web/src/api/types';

const WEB_ROOT = fileURLToPath(new URL('../../web', import.meta.url));
const REPO_ROOT = resolve(WEB_ROOT, '..');
const CONTRACT = resolve(REPO_ROOT, 'contracts/api/product_api.openapi.v1.json');
const GENERATED = resolve(WEB_ROOT, 'src/api/generated/contract.ts');

describe('C16 契约 → TypeScript 生成链路', () => {
  it('契约未变更时生成物零漂移（npm run gen:api -- --check 同款校验）', () => {
    const output = execFileSync(process.execPath, ['scripts/gen-api-types.mjs', '--check'], {
      cwd: WEB_ROOT,
      encoding: 'utf8',
    });
    expect(output).toContain('一致');
  });

  it('生成物 sha256 与契约原文一致', () => {
    const raw = readFileSync(CONTRACT, 'utf8').replace(/\r\n/g, '\n');
    const sha = createHash('sha256').update(raw).digest('hex');
    const generated = readFileSync(GENERATED, 'utf8');
    expect(generated).toContain(`CONTRACT_SHA256 = "${sha}"`);
    expect(CONTRACT_SHA256).toBe(sha);
  });

  it('契约全部路径进入生成文件，关键实体类型可被页面引用', () => {
    const spec = JSON.parse(readFileSync(CONTRACT, 'utf8')) as { paths: Record<string, unknown> };
    // 不写死路径数：契约是唯一真源，数量以同一次读取为准。
    const paths = Object.keys(spec.paths);
    expect(paths.length).toBeGreaterThanOrEqual(16);
    expect(CONTRACT_PATH_COUNT).toBe(paths.length);

    const generated = readFileSync(GENERATED, 'utf8');
    for (const path of paths) {
      // ApiOperations 的键形如 "GET /v1/..."，这里只校验路径本身进入生成物。
      expect(generated).toContain(path);
    }
    for (const name of [
      'TicketDetailOut',
      'ReconDiffDetailOut',
      'EvidenceOut',
      'AuditChainOut',
      'PageApprovalTaskOut',
      'ApiOperations',
    ]) {
      expect(generated).toContain(`export interface ${name}`);
    }
  });

  it('差异类型与 12 状态与后端契约枚举对齐', () => {
    expect(DIFF_TYPES).toHaveLength(8);
    expect(DIFF_TYPES.map((item) => item.code)).toContain('rounding_tolerance');
    expect(TICKET_STATES).toHaveLength(12);
  });
});
