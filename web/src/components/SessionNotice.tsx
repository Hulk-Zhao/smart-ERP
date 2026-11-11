import { errorTitle, isApiError } from '../api/errors';
import { IconAlert, IconSettings } from './icons';

export interface SessionNoticeProps {
  /** 会话端点的原始错误，用于展示 401 的真实 code/trace，不另外编造原因。 */
  error: unknown;
  onRetry: () => void;
  onOpenSettings?: () => void;
  compact?: boolean;
}

/**
 * 演示会话不可用时的显式降级：
 * 服务端 `ERP_DEMO_SESSION_ENABLED` 默认关闭，`GET /v1/auth/session` 返回 401，
 * 此时「待你决策」不能渲染成 0 —— 0 是「确实没有待办」，这里是「根本没拿到身份」。
 */
export function SessionNotice({ error, onRetry, onOpenSettings, compact = false }: SessionNoticeProps) {
  const apiError = isApiError(error) ? error : null;
  return (
    <div
      role="status"
      data-testid="session-notice"
      className={`min-w-0 max-w-full overflow-hidden rounded-xl border border-amber-300 bg-amber-50 ${compact ? 'px-3 py-2.5' : 'px-4 py-3.5'}`}
    >
      <div className="flex items-start gap-2">
        <IconAlert width={14} height={14} className="mt-0.5 shrink-0 text-amber-600" />
        <div className="min-w-0">
          <p className="text-[13px] font-semibold text-amber-900">当前未启用演示会话</p>
          <p className="mt-1 break-words text-[12px] leading-5 text-amber-800">
            服务端未开启演示会话（<span className="font-mono">ERP_DEMO_SESSION_ENABLED</span> 关闭），
            或携带的 <span className="font-mono">X-Session-Token</span> 无效，因此拿不到
            <span className="font-mono"> user_id / tenant_id / roles</span>。
            这里的「待你决策」不是 0 条，而是<strong className="font-semibold">没有身份可查</strong>。
          </p>
          <p className="mt-1 break-words text-[11px] leading-5 text-amber-700">
            启用方式：部署期设 <span className="font-mono">ERP_DEMO_SESSION_ENABLED=true</span> 且
            <span className="font-mono"> ERP_APP_ENV=demo</span>，注入静态令牌后在「连接设置」填写同一令牌。
          </p>
          {apiError ? (
            <p className="mt-1 break-all font-mono text-[10.5px] text-amber-700/80">
              {errorTitle(error)}
              {apiError.traceId ? ` · trace ${apiError.traceId}` : ''}
            </p>
          ) : null}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
              重试会话
            </button>
            {onOpenSettings ? (
              <button type="button" className="btn btn-ghost btn-sm" onClick={onOpenSettings}>
                <IconSettings width={13} height={13} />
                连接设置
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
