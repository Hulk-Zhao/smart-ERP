/**
 * 会话身份：`GET /v1/auth/session`（C16）是前端唯一的身份与默认范围来源。
 *
 * 不再用 `VITE_SESSION_ROLES` / `VITE_VIEWER_ID` 在构建期猜角色与用户：
 * 服务端返回 `{user_id, tenant_id, roles, default_scope?}` 后写入运行期配置，页面再发依赖身份的查询。
 * 服务端演示会话默认关闭（`ERP_DEMO_SESSION_ENABLED=false`），此时端点返回 401，
 * 界面必须显式说明「当前未启用演示会话」，不能把 0 条待办渲染成「没有待办」。
 *
 * `default_scope.org_id === null` 表示令牌授权了多个组织，服务端不猜，前端必须进入「待用户选择」，
 * 不能拿环境变量里的 ORG-1 顶上去。
 */

import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from './client';
import { applySessionIdentity, applySessionScope, useApiConfig } from './config';
import { isApiError } from './errors';
import type { SessionOut } from './types';

export type SessionStatus = 'loading' | 'ready' | 'unavailable' | 'error';

/** 会话拿不到完整 scope 的原因：多组织 / 无账期 / 端点没给 default_scope / 手填后仍为空。 */
export type ScopeIssue = 'multi-org' | 'missing-period' | 'missing-scope' | 'incomplete';

export interface SessionState {
  status: SessionStatus;
  session: SessionOut | null;
  error: unknown;
  /** 401：服务端没有可用会话（演示会话未启用或令牌无效），依赖身份的查询一律不发。 */
  unavailable: boolean;
  /** org_id 与 period 都有值：G-05 要求三项齐全，缺一项就不发查询。 */
  scopeReady: boolean;
  /** 不能发查询的原因；为 null 表示 scope 可用。 */
  scopeIssue: ScopeIssue | null;
  /** 依赖身份的查询是否可以发：只在「确定没有身份」或「scope 不全」时拦住；网络/其它错误照常请求并各自报错。 */
  canQuery: boolean;
  refresh: () => void;
}

export function useSession(): SessionState {
  const config = useApiConfig();
  const query = useQuery({
    // 令牌进 key：换令牌就是换一次会话，旧令牌的 401/身份不会污染新结果。
    queryKey: ['auth', 'session', config.token],
    queryFn: () => api.getSession(),
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const session = query.data ?? null;
  useEffect(() => {
    if (session) applySessionIdentity({ user_id: session.user_id, roles: session.roles });
  }, [session]);

  // 只有 401 才代表「没有身份」；403/404/网络错误属于另一种故障，不能冒充「未启用会话」。
  const unavailable = isApiError(query.error) && query.error.status === 401;
  const status: SessionStatus = session
    ? 'ready'
    : query.isLoading
      ? 'loading'
      : unavailable
        ? 'unavailable'
        : 'error';

  // 会话落地（或明确失败）后把 default_scope 写进配置；手填的 scope 不会被覆盖。
  useEffect(() => {
    if (status === 'loading') return;
    applySessionScope(session ? (session.default_scope ?? null) : null);
  }, [session, status]);

  const scopeReady = config.scope.org_id.trim() !== '' && config.scope.period.trim() !== '';

  let scopeIssue: ScopeIssue | null = null;
  if (session && !scopeReady) {
    const scope = session.default_scope ?? null;
    if (!scope) scopeIssue = 'missing-scope';
    else if (!scope.org_id) scopeIssue = 'multi-org';
    else if (!scope.period) scopeIssue = 'missing-period';
    else scopeIssue = 'incomplete';
  }

  // 会话已返回但 scope 还没写进配置（同一轮渲染里 effect 尚未执行）时先别发请求，避免用占位 org 打后端。
  const canQuery =
    status === 'ready'
      ? config.scopeSource !== 'pending' && scopeReady
      : status === 'error' && scopeReady;

  return {
    status,
    session,
    error: query.error,
    unavailable,
    scopeReady,
    scopeIssue,
    canQuery,
    refresh: () => void query.refetch(),
  };
}
