import type { ApprovalOut } from '../api/types';
import { dateTime, shortId } from '../lib/format';
import { RoleBadge } from './Badges';

const DECISION_LABELS: Record<string, string> = {
  approved: '已通过',
  rejected: '已驳回',
};

/**
 * 双人复核意见区：界面层脱敏。
 *
 * - 只有当前身份自己那一条 approval 才渲染 comment；
 * - 其他审批人的意见、审批人 ID 一律不渲染（只显示「已提交」）；
 * - 发起人/未登录身份（viewerRole=null）看不到任何意见正文。
 *
 * 服务端是否返回全量意见不是界面泄露的理由，渲染前必须先过滤。
 */
export function ApprovalOpinions({
  approvals,
  requiredRoles,
  viewerRole,
  viewerId,
  initiatorId,
}: {
  approvals: ApprovalOut[];
  requiredRoles: string[];
  viewerRole: string | null;
  viewerId?: string;
  initiatorId?: string;
}) {
  const roles = [...new Set([...requiredRoles, ...approvals.map((item) => item.role)])];
  const isInitiator = !viewerRole || viewerRole === 'initiator';
  const submitted = approvals.length > 0;

  return (
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="section-title">双人复核意见</h3>
        <span className="badge bg-slate-100 text-slate-600 ring-slate-200">意见互不可见</span>
      </div>

      <p className="mb-2 text-[11px] leading-5 text-slate-500">
        审批人与发起人互不可见对方意见。提交前，你只能看到自己的意见草稿；其他角色只显示是否已提交。
        {isInitiator ? ' 当前身份为发起人/未登录，所有审批意见均不展示正文。' : ''}
        {initiatorId ? ` 发起人：${shortId(initiatorId)}` : ''}
      </p>

      <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
        {roles.length === 0 ? (
          <li className="px-3 py-3 text-[12px] text-slate-500">无需审批角色。</li>
        ) : null}
        {roles.map((role) => {
          const record = approvals.find((item) => item.role === role);
          const own = Boolean(viewerId) && record?.approver_id === viewerId && record?.role === viewerRole && !isInitiator;
          return (
            <li key={role} className="min-w-0 px-3 py-2" data-testid={`opinion-${role}`}>
              <div className="flex flex-wrap items-center gap-2">
                <RoleBadge role={role} />
                {record ? (
                  <span className="badge bg-emerald-50 text-emerald-700 ring-emerald-200">
                    {own ? DECISION_LABELS[record.decision] ?? record.decision : '已提交'}
                  </span>
                ) : (
                  <span className="badge bg-slate-100 text-slate-500 ring-slate-200">待提交</span>
                )}
                {record ? <span className="text-[11px] text-slate-400">{dateTime(record.decided_at)}</span> : null}
              </div>

              {record ? (
                own ? (
                  <div className="mt-1 min-w-0 rounded-lg bg-slate-50 px-2.5 py-2">
                    <div className="label">我的意见</div>
                    <p className="mt-0.5 whitespace-pre-wrap break-words text-[12px] text-slate-700">
                      {record.comment?.trim() ? record.comment : '未填写意见'}
                    </p>
                  </div>
                ) : (
                  <div className="mt-1 rounded-lg border border-dashed border-slate-200 px-2.5 py-2 text-[11px] text-slate-500">
                    意见已提交，当前身份不可见（审批人 ID 与意见正文均不展示）。
                  </div>
                )
              ) : (
                <div className="mt-1 text-[11px] text-slate-400">尚未提交意见。</div>
              )}
            </li>
          );
        })}
      </ul>

      {submitted && !viewerRole ? (
        <p className="mt-2 text-[11px] text-amber-700">当前未选择审批身份，仅展示是否存在审批记录，不展示任何意见正文。</p>
      ) : null}
    </div>
  );
}
