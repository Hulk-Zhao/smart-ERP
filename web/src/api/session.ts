/**
 * 会话身份：`GET /v1/auth/session`（C16）是前端唯一的身份来源。
 *
 * 不再用 `VITE_SESSION_ROLES` / `VITE_VIEWER_ID` 在构建期猜角色与用户：
 * 服务端返回 `{user_id, tenant_id, roles}` 后写入运行期配置，页面再发依赖身份的查询。
 * 服务端演示会话默认关闭（`ERP_DEMO_SESSION_ENABLED=false`），此时端点返回 401，
 * 界面必须显式说明「当前未启用演示会话」，不能把 0 条待办渲染成「没有待办」。
 */

import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from './client';
import { applySessionIdentity } from './config';
import { isApiError } from './errors';
import type { SessionOut } from './types';

export type SessionStatus = 'loading' | 'ready' | 'unavailable' | 'error';

export interface SessionState {
  status: SessionStatus;
  session: SessionOut | null;
  error: unknown;
  /** 401：服务端没有可用会话（演示会话未启用或令牌无效），依赖身份的查询一律不发。 */
  unavailable: boolean;
  /** 依赖身份的查询是否可以发：只在「确定没有身份」时拦住；网络/其它错误照常请求并各自报错。 */
  canQuery: boolean;
  refresh: () => void;
}

export function useSession(): SessionState {
  const query = useQuery({
    queryKey: ['auth', 'session'],
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

  return {
    status,
    session,
    error: query.error,
    unavailable,
    canQuery: status !== 'loading' && !unavailable,
    refresh: () => void query.refetch(),
  };
}
