/**
 * 运行期配置：API 地址、会话令牌、G-05 scope、当前审批角色。
 *
 * 默认走真实后端；只有显式设置 `VITE_USE_MOCK=true` 才启用离线 mock（默认关）。
 */

import { useSyncExternalStore } from 'react';
import type { ApprovalDecisionIn, ScopeIn } from './generated/contract';

export type ApprovalRole = ApprovalDecisionIn['role'];

export interface ApiConfig {
  baseUrl: string;
  token: string;
  scope: ScopeIn;
  roles: ApprovalRole[];
  viewerName: string;
  /** 当前登录用户 ID（用于判断哪条审批意见属于自己；为空则一律不展示意见正文） */
  viewerId: string;
  /** 仅显式开启时使用 src/api/mock.ts；默认 false */
  mock: boolean;
}

const STORAGE_KEY = 'smart-erp.web.config.v1';
const ENV = import.meta.env;

function envList(value: string | undefined, fallback: string[]): string[] {
  if (!value) return fallback;
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function envRoles(value: string | undefined): ApprovalRole[] {
  const allowed: ApprovalRole[] = ['reviewer', 'approver', 'cfo'];
  const roles = envList(value, ['reviewer']).filter((item): item is ApprovalRole =>
    (allowed as string[]).includes(item),
  );
  return roles.length > 0 ? roles : ['reviewer'];
}

function defaults(): ApiConfig {
  return {
    baseUrl: ENV.VITE_API_BASE ?? '/erp/api',
    token: ENV.VITE_SESSION_TOKEN ?? '',
    scope: {
      org_id: ENV.VITE_ORG_ID ?? 'ORG-1',
      period: ENV.VITE_PERIOD ?? '2026-10',
      system_ids: envList(ENV.VITE_SYSTEM_IDS, ['u8', 'statement']),
    },
    roles: envRoles(ENV.VITE_SESSION_ROLES),
    viewerName: ENV.VITE_VIEWER_NAME ?? '当前用户',
    viewerId: ENV.VITE_VIEWER_ID ?? '',
    mock: ENV.VITE_USE_MOCK === 'true',
  };
}

function load(): ApiConfig {
  const base = defaults();
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    if (!raw) return base;
    const saved = JSON.parse(raw) as Partial<ApiConfig>;
    return {
      ...base,
      ...saved,
      // 环境变量显式开启 mock 时不允许被本地存储覆盖为关闭
      mock: base.mock || saved.mock === true,
      scope: { ...base.scope, ...(saved.scope ?? {}) },
      roles: saved.roles?.length ? saved.roles : base.roles,
    };
  } catch {
    return base;
  }
}

let current = load();
const listeners = new Set<() => void>();

export function getApiConfig(): ApiConfig {
  return current;
}

export function configureApi(patch: Partial<ApiConfig>): void {
  current = {
    ...current,
    ...patch,
    scope: { ...current.scope, ...(patch.scope ?? {}) },
  };
  try {
    const { mock: _mock, ...persisted } = current;
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(persisted));
  } catch {
    // 隐私模式下 localStorage 不可写；配置仍对当前会话生效
  }
  listeners.forEach((listener) => listener());
}

export function resetApiConfig(): void {
  current = defaults();
  listeners.forEach((listener) => listener());
}

export function subscribeApiConfig(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useApiConfig(): ApiConfig {
  return useSyncExternalStore(subscribeApiConfig, getApiConfig, getApiConfig);
}

export function scopeQuery(scope: ScopeIn): Array<[string, string]> {
  return [
    ['org_id', scope.org_id],
    ['period', scope.period],
    ...scope.system_ids.map((system): [string, string] => ['system_ids', system]),
  ];
}
