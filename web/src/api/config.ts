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
  /** 当前会话可用的审批角色；由 `/v1/auth/session` 写入，不再从环境变量猜。 */
  roles: ApprovalRole[];
  viewerName: string;
  /**
   * 当前登录用户 ID（用于判断哪条审批意见属于自己；为空则一律不展示意见正文）。
   * 由 `/v1/auth/session` 写入，不从环境变量或本地存储读取。
   */
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

function defaults(): ApiConfig {
  return {
    baseUrl: ENV.VITE_API_BASE ?? '/erp/api',
    token: ENV.VITE_SESSION_TOKEN ?? '',
    scope: {
      org_id: ENV.VITE_ORG_ID ?? 'ORG-1',
      period: ENV.VITE_PERIOD ?? '2026-10',
      system_ids: envList(ENV.VITE_SYSTEM_IDS, ['u8', 'statement']),
    },
    // 身份与角色只认 `/v1/auth/session`：启动时为空，会话返回后由 applySessionIdentity 写入。
    roles: [],
    viewerName: ENV.VITE_VIEWER_NAME ?? '当前用户',
    viewerId: '',
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
      // 身份只认会话端点：旧版本存过的 roles / viewerId 一律不恢复，避免用过期身份猜权限。
      roles: base.roles,
      viewerId: base.viewerId,
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
    // 身份与 mock 开关不落本地存储：前者只认会话端点，后者只认构建期 VITE_USE_MOCK。
    const { mock: _mock, roles: _roles, viewerId: _viewerId, ...persisted } = current;
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(persisted));
  } catch {
    // 隐私模式下 localStorage 不可写；配置仍对当前会话生效
  }
  listeners.forEach((listener) => listener());
}

const APPROVAL_ROLES: ApprovalRole[] = ['reviewer', 'approver', 'cfo'];

/** 会话里的 roles 是宽松字符串；只保留前端认识的审批角色，其余原样由页面展示用不到。 */
export function toApprovalRoles(values: readonly string[]): ApprovalRole[] {
  return values.filter((item): item is ApprovalRole => (APPROVAL_ROLES as string[]).includes(item));
}

/**
 * 把 `/v1/auth/session` 的身份写入运行期配置（不落本地存储）。
 * 值未变化时不广播，避免每个调用方都在挂载后触发一次无效重渲染。
 */
export function applySessionIdentity(identity: { user_id: string; roles: readonly string[] }): void {
  const roles = toApprovalRoles(identity.roles);
  const sameRoles =
    roles.length === current.roles.length && roles.every((role, index) => role === current.roles[index]);
  if (sameRoles && current.viewerId === identity.user_id) return;
  current = { ...current, roles, viewerId: identity.user_id };
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
