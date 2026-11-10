import { isApiError, isApprovalConflict, isW3Forbidden, errorHint, errorTitle } from '../api/errors';
import { RoleBadge } from './Badges';

interface Props {
  error: unknown;
  onRetry?: () => void;
  compact?: boolean;
}

/** 统一错误呈现：403 W3 必须明确零写入，409 审批必须列出缺失角色。 */
export function ErrorPanel({ error, onRetry, compact = false }: Props) {
  const apiError = isApiError(error) ? error : null;
  const w3 = isW3Forbidden(error);
  const approval = isApprovalConflict(error);
  const body = (apiError?.body ?? {}) as Record<string, unknown>;
  const missing = Array.isArray(body.missing_roles) ? (body.missing_roles as string[]) : [];
  const required = Array.isArray(body.required_roles) ? (body.required_roles as string[]) : [];

  return (
    <div
      role="alert"
      data-testid="error-panel"
      className={`rounded-xl border px-3 py-3 ${w3 ? 'border-rose-300 bg-rose-50' : 'border-amber-200 bg-amber-50'} ${compact ? 'text-[12px]' : ''}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[13px] font-semibold text-slate-900">{errorTitle(error)}</span>
        {apiError ? <span className="font-mono text-[11px] text-slate-600">{apiError.code}</span> : null}
        {apiError?.retriable ? (
          <span className="badge bg-white text-slate-500 ring-slate-200">可重试</span>
        ) : null}
      </div>

      <p className="mt-1 break-words text-[12px] leading-5 text-slate-700">
        {apiError?.detail ?? '未识别的错误，请查看浏览器控制台。'}
      </p>

      {w3 ? (
        <div className="mt-2 rounded-lg border border-rose-200 bg-white/80 px-2.5 py-2 text-[12px] text-rose-800">
          <strong className="font-semibold">连接器零写入</strong>：W3 动作在网关层整单拒绝，未向任何下游系统下发写入，工单状态与版本均未改变。
        </div>
      ) : null}

      {approval ? (
        <div className="mt-2 rounded-lg border border-amber-200 bg-white/80 px-2.5 py-2 text-[12px] text-amber-900">
          <div className="font-semibold">审批链未满足</div>
          {missing.length > 0 ? (
            <div className="mt-1 flex flex-wrap items-center gap-1">
              <span>尚缺角色：</span>
              {missing.map((role) => (
                <RoleBadge key={role} role={role} />
              ))}
            </div>
          ) : (
            <div className="mt-1">请补齐缺失角色的审批意见后重试。</div>
          )}
          {required.length > 0 ? (
            <div className="mt-1 flex flex-wrap items-center gap-1 text-slate-600">
              <span>需要角色：</span>
              {required.map((role) => (
                <RoleBadge key={role} role={role} />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      <p className="mt-2 break-words text-[11px] text-slate-500">{errorHint(error)}</p>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        {apiError?.traceId ? <span className="font-mono">trace: {apiError.traceId}</span> : null}
        {apiError?.failCode ? <span className="font-mono">fail_code: {apiError.failCode}</span> : null}
        {onRetry ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
            重试
          </button>
        ) : null}
      </div>
    </div>
  );
}
