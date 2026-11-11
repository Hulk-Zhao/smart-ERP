/**
 * 运行期配置：API 地址、会话令牌、G-05 scope、当前审批角色。
 *
 * 默认走真实后端；只有显式设置 `VITE_USE_MOCK=true` 才启用离线 mock（默认关）。
 */

import { useSyncExternalStore } from 'react';
import type { ApprovalDecisionIn, ScopeIn, SessionScopeOut } from './generated/contract';

export type ApprovalRole = ApprovalDecisionIn['role'];

/**
 * scope 的来源，决定能否被会话默认范围覆盖、以及要不要落本地存储：
 * - `pending`：会话还没返回，此刻的 scope 只是环境变量/占位值，不能据此发查询；
 * - `session`：由 `/v1/auth/session` 的 `default_scope` 带出，可被新会话继续覆盖；
 * - `manual`：用户手填，优先级最高，不会被会话覆盖，且会持久化；
 * - `unresolved`：会话返回了但没有完整范围（多组织 / 无账期），等待用户补齐。
 */
export type ScopeSource = 'pending' | 'session' | 'manual' | 'unresolved';

export interface ApiConfig {
  baseUrl: string;
  token: string;
  scope: ScopeIn;
  scopeSource: ScopeSource;
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
    // 会话返回前不把环境变量里的 org/period 当作既定事实：先标记 pending，等 default_scope 落地。
    scopeSource: 'pending',
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
    const saved = JSON.parse(raw) as Partial<ApiConfig> & { scopeManual?: boolean };
    // 只有用户显式改过并保存的 scope 才恢复（scopeManual）；旧版本存下的默认值一律视为待会话带出。
    const manual = saved.scopeManual === true && !!saved.scope;
    return {
      ...base,
      ...saved,
      // 环境变量显式开启 mock 时不允许被本地存储覆盖为关闭
      mock: base.mock || saved.mock === true,
      scope: manual ? { ...base.scope, ...saved.scope } : base.scope,
      scopeSource: manual ? 'manual' : 'pending',
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
    // scope 只在用户手填（manual）时保存；会话带出的范围每次启动重新取，避免把旧 scope 当成本地事实。
    const persisted: Record<string, unknown> = {
      baseUrl: current.baseUrl,
      token: current.token,
      viewerName: current.viewerName,
    };
    if (current.scopeSource === 'manual') {
      persisted.scope = current.scope;
      persisted.scopeManual = true;
    }
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

/**
 * 把 `/v1/auth/session` 的 `default_scope` 写入运行期 scope。
 *
 * 规则（服务端返回 null 的含义必须原样保留，不能猜）：
 * - `org_id === null`：令牌授权多个组织，清空 org_id 进入「待选择」，绝不回退到 ORG-1；
 * - `period === null`：授权期内没有合法账期，清空 period 让用户补；
 * - 整个 `default_scope` 为 null：保留现有值供手填，只把来源标成 unresolved；
 * - 用户手填（manual）的 scope 优先级最高，会话不再覆盖。
 */
export function applySessionScope(scope: SessionScopeOut | null | undefined): void {
  if (current.scopeSource === 'manual') return;

  if (!scope) {
    // 端点没给默认范围：保留现值供手填，但来源必须从 pending/session 降级为 unresolved。
    if (current.scopeSource !== 'unresolved') {
      current = { ...current, scopeSource: 'unresolved' };
      listeners.forEach((listener) => listener());
    }
    return;
  }

  const orgId = scope.org_id ?? '';
  const period = scope.period ?? '';
  const systemIds = scope.system_ids.length > 0 ? [...scope.system_ids] : current.scope.system_ids;
  const nextSource: ScopeSource = orgId && period ? 'session' : 'unresolved';

  const unchanged =
    current.scopeSource === nextSource &&
    current.scope.org_id === orgId &&
    current.scope.period === period &&
    current.scope.system_ids.join(',') === systemIds.join(',');
  if (unchanged) return;

  current = { ...current, scope: { org_id: orgId, period, system_ids: systemIds }, scopeSource: nextSource };
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
