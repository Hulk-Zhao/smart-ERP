import { Link } from 'react-router-dom';
import {
  errorHint,
  errorKind,
  errorTitle,
  isApiError,
  isApprovalConflict,
  isCategoryGateRejection,
  isW3Forbidden,
  type ErrorKind,
} from '../api/errors';
import { RoleBadge } from './Badges';

export type NotFoundKind = 'batch' | 'metering' | 'ticket' | 'diff' | 'resource';

export interface ErrorPanelProps {
  error: unknown;
  onRetry?: () => void;
  compact?: boolean;
  /** 404 的语义：不同资源给不同文案与返回动作。 */
  notFoundKind?: NotFoundKind;
  /** 404 的固定返回入口（无则只显示「重新检查」）。 */
  backTo?: { to: string; label: string };
  /** 403 的下一步：打开连接设置（角色/scope/令牌）。 */
  onOpenSettings?: () => void;
}

const NOT_FOUND_COPY: Record<NotFoundKind, { title: string; description: string }> = {
  batch: {
    title: '批次不存在或已过期',
    description: '该批次可能已被清理，或不属于当前 scope。请重新发起对账，或从对账页打开最近批次。',
  },
  metering: {
    title: '该批次尚未生成计量表',
    description: '计量表在对账完成或重跑后生成。可返回批次页查看当前状态，或重新检查。',
  },
  ticket: {
    title: '工单不存在或不在当前 scope',
    description: '工单可能已被删除、不属于当前 scope，或链接有误。请返回工单列表，确认 org/period/systems 后重试。',
  },
  diff: {
    title: '差异不存在或已被清理',
    description: '该差异可能已被清理或不属于当前批次。请返回批次差异列表重新选择。',
  },
  resource: {
    title: '资源不存在',
    description: '请求的资源不存在或已被清理，请返回上一页确认。',
  },
};

const CONFLICT_COPY: Record<string, { title: string; description: string }> = {
  'E-POLICY-APPROVAL': {
    title: '审批链未满足',
    description: '补齐缺失角色的审批意见后再提交；当前状态与版本未改变。',
  },
  'E-STATE-ILLEGAL': {
    title: '当前状态不允许该操作',
    description: '工单状态可能已被他人推进。请先刷新确认当前状态，再让对应角色完成审批/执行。',
  },
  'E-CONFLICT-VERSION': {
    title: '数据已被他人更新',
    description: '请重新取数后再操作，不要用旧版本重复提交。',
  },
  'E-IDEM-CONFLICT': {
    title: '重复提交或同键不同参',
    description: '先刷新确认上一次是否已成功，不要原样重试。',
  },
  'E-EXTERNAL-EFFECTIVE': {
    title: '外部已生效，不可回滚',
    description: '请生成处置任务并保留证据，不要重复发起原动作。',
  },
  'E-VERIFY-MISMATCH': {
    title: '写入值或审计链校验不一致',
    description: '已进入人工核查，不要反复重试；请把 trace_id 交给运维/审计。',
  },
};

const KIND_TONE: Record<ErrorKind, string> = {
  forbidden: 'border-rose-300 bg-rose-50',
  notfound: 'border-slate-300 bg-slate-50',
  conflict: 'border-amber-300 bg-amber-50',
  config: 'border-rose-300 bg-rose-50',
  network: 'border-amber-300 bg-amber-50',
  generic: 'border-amber-200 bg-amber-50',
};

const KIND_LABEL: Record<ErrorKind, string> = {
  forbidden: '无权限',
  notfound: '不存在',
  conflict: '状态冲突',
  config: '配置缺失',
  network: '网络异常',
  generic: '请求失败',
};

/**
 * 统一错误呈现：所有页面共用这一套分类与文案。
 *
 * 403 越权、404 不存在（区分批次/计量表/工单/差异）、409 状态冲突、
 * 503 E-DATA-MISSING 配置缺失、网络中断/超时、其它错误各自有明确下一步动作。
 * 390px 下靠 min-w-0 + break-words 换行，不横向溢出。
 */
export function ErrorPanel({
  error,
  onRetry,
  compact = false,
  notFoundKind,
  backTo,
  onOpenSettings,
}: ErrorPanelProps) {
  const kind = errorKind(error);
  const resolvedNotFoundKind = notFoundKind ?? 'resource';
  const apiError = isApiError(error) ? error : null;
  const w3 = isW3Forbidden(error);
  const categoryGate = isCategoryGateRejection(error);
  const approval = isApprovalConflict(error);
  const body = (apiError?.body ?? {}) as Record<string, unknown>;
  const rolesOf = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
  const approvalMissing = rolesOf(body.approval_missing_roles);
  const approvalRequired = rolesOf(body.approval_required_roles);
  const executionMissing = rolesOf(body.execution_missing_roles);
  const executionRequired = rolesOf(body.execution_required_roles);
  const notFound = NOT_FOUND_COPY[resolvedNotFoundKind];
  const conflict = CONFLICT_COPY[apiError?.code ?? ''] ?? CONFLICT_COPY['E-STATE-ILLEGAL'];
  const retryLabel = kind === 'notfound' ? '重新检查' : kind === 'conflict' ? '刷新' : '重试';

  return (
    <div
      role="alert"
      data-testid="error-panel"
      data-kind={kind}
      className={`min-w-0 max-w-full overflow-hidden rounded-xl border px-3 py-3 ${KIND_TONE[kind]} ${compact ? 'text-[12px]' : ''}`}
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[13px] font-semibold text-slate-900">{KIND_LABEL[kind]}</span>
        <span className="min-w-0 break-all font-mono text-[11px] text-slate-600">{errorTitle(error)}</span>
        {apiError?.retriable && kind !== 'config' ? (
          <span className="badge bg-white text-slate-500 ring-slate-200">可重试</span>
        ) : null}
      </div>

      <p className="mt-1 break-words text-[12px] leading-5 text-slate-700">
        {apiError?.detail ?? '未识别的错误，请查看浏览器控制台。'}
      </p>

      {kind === 'forbidden' ? (
        <div className="mt-2 rounded-lg border border-rose-200 bg-white/80 px-2.5 py-2 text-[12px] text-rose-800">
          {categoryGate ? (
            <>
              <div className="font-semibold">业务类别闸拒绝</div>
              <p className="mt-1 break-words">
                该身份有审批角色，但不能复核这一类业务场景（判据见上方服务端原文）。这不是缺角色，也不是 scope 配置问题；
                换持有对应类别角色的身份复核，或按服务端原文处理。
              </p>
            </>
          ) : w3 ? (
            <>
              <div className="font-semibold">连接器零写入</div>
              <p className="mt-1 break-words">
                W3 动作在网关层整单拒绝，未向任何下游系统下发写入，工单状态与版本均未改变。这是安全策略，不是故障。
              </p>
            </>
          ) : (
            <>
              <div className="font-semibold">缺少所需角色或 scope</div>
              <p className="mt-1 break-words">
                请确认你需要的角色（reviewer 复核人 / approver 审批人 / cfo 财务负责人）与 scope（org / period / systems）。
                把 trace_id 发给管理员，请其在「设置 → 角色与 scope」为你开通后重试。
              </p>
            </>
          )}
          {approvalMissing.length > 0 ? (
            <div className="mt-1 flex flex-wrap items-center gap-1">
              <span>尚缺角色：</span>
              {approvalMissing.map((role) => (
                <RoleBadge key={role} role={role} />
              ))}
            </div>
          ) : null}
          {approvalRequired.length > 0 ? (
            <div className="mt-1 flex flex-wrap items-center gap-1 text-slate-600">
              <span>需要角色：</span>
              {approvalRequired.map((role) => (
                <RoleBadge key={role} role={role} />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {kind === 'notfound' ? (
        <div className="mt-2 rounded-lg border border-slate-200 bg-white/80 px-2.5 py-2 text-[12px] text-slate-700">
          <div className="font-semibold text-slate-900">{notFound.title}</div>
          <p className="mt-1 break-words">{notFound.description}</p>
        </div>
      ) : null}

      {kind === 'conflict' ? (
        <div className="mt-2 rounded-lg border border-amber-200 bg-white/80 px-2.5 py-2 text-[12px] text-amber-900">
          <div className="font-semibold">{conflict.title}</div>
          <p className="mt-1 break-words">{conflict.description}</p>
          {approval ? (
            <>
              {approvalMissing.length === 0 && executionMissing.length > 0 ? (
                <p className="mt-1 break-words text-[11px] text-amber-800">
                  审批链已满足；执行另需 B 级连接器强制双签（审批一票 ≠ 执行一票）。
                </p>
              ) : null}
              {executionMissing.length > 0 ? (
                <div className="mt-1 flex flex-wrap items-center gap-1">
                  <span>执行尚缺角色：</span>
                  {executionMissing.map((role) => (
                    <RoleBadge key={role} role={role} />
                  ))}
                </div>
              ) : null}
              {executionRequired.length > 0 ? (
                <div className="mt-1 flex flex-wrap items-center gap-1 text-slate-600">
                  <span>执行需要角色：</span>
                  {executionRequired.map((role) => (
                    <RoleBadge key={role} role={role} />
                  ))}
                </div>
              ) : null}
              {approvalMissing.length > 0 ? (
                <div className="mt-1 flex flex-wrap items-center gap-1">
                  <span>审批尚缺角色：</span>
                  {approvalMissing.map((role) => (
                    <RoleBadge key={role} role={role} />
                  ))}
                </div>
              ) : null}
              {approvalRequired.length > 0 ? (
                <div className="mt-1 flex flex-wrap items-center gap-1 text-slate-600">
                  <span>审批需要角色：</span>
                  {approvalRequired.map((role) => (
                    <RoleBadge key={role} role={role} />
                  ))}
                </div>
              ) : null}
            </>
          ) : null}
        </div>
      ) : null}

      {kind === 'config' ? (
        <div className="mt-2 rounded-lg border border-rose-200 bg-white/80 px-2.5 py-2 text-[12px] text-rose-800">
          <div className="font-semibold">配置缺失，不是系统故障</div>
          <p className="mt-1 break-words">
            单价未配置，请管理员设置环境变量 <span className="break-all font-mono">ERP_METERING_UNIT_PRICE</span>
            （例如 0.8000）后重新加载本页。在配置完成前反复重试不会成功。
          </p>
        </div>
      ) : null}

      {kind === 'network' ? (
        <div className="mt-2 rounded-lg border border-amber-200 bg-white/80 px-2.5 py-2 text-[12px] text-amber-900">
          <div className="font-semibold">网络中断或请求超时</div>
          <p className="mt-1 break-words">
            请求没有到达服务端或响应超时。可以重试；重试会沿用当前筛选条件与查询参数。
          </p>
        </div>
      ) : null}

      <p className="mt-2 break-words text-[11px] text-slate-500">{errorHint(error)}</p>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        {apiError?.traceId ? <span className="min-w-0 break-all font-mono">trace: {apiError.traceId}</span> : null}
        {apiError?.failCode ? <span className="break-all font-mono">fail_code: {apiError.failCode}</span> : null}
        {kind === 'forbidden' && onOpenSettings ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={onOpenSettings}>
            打开连接设置
          </button>
        ) : null}
        {backTo ? (
          <Link to={backTo.to} className="link inline-flex min-h-[44px] items-center text-[12px]">
            {backTo.label}
          </Link>
        ) : null}
        {kind !== 'config' && onRetry ? (
          <button type="button" className="btn btn-secondary btn-sm min-h-[44px]" onClick={onRetry}>
            {retryLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}
