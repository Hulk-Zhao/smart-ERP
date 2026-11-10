import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, type ApprovalDecision, type ApprovalPayload } from '../api/client';
import { useApiConfig, type ApprovalRole } from '../api/config';
import { ApprovalOpinions } from '../components/ApprovalOpinions';
import { ErrorPanel } from '../components/ErrorPanel';
import { LevelBadge, RoleBadge, StateBadge } from '../components/Badges';
import { amount, dateTime } from '../lib/format';

export function ApprovalsPage() {
  const config = useApiConfig();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [comment, setComment] = useState('');
  const [role, setRole] = useState<ApprovalRole>(config.roles[0] ?? 'reviewer');

  const tasks = useQuery({
    queryKey: ['approvals', 'pending', 'page'],
    queryFn: () => api.listPendingApprovals({ limit: 50 }),
  });

  const items = tasks.data?.items ?? [];
  const selectedId = searchParams.get('ticket') || items[0]?.ticket_id || '';
  const selected = items.find((item) => item.ticket_id === selectedId);

  const detail = useQuery({
    queryKey: ['ticket', selectedId],
    queryFn: () => api.getTicket(selectedId),
    enabled: Boolean(selectedId),
  });

  useEffect(() => {
    if (!selected) return;
    const nextRole = config.roles.find((item) => selected.missing_roles.includes(item)) ?? config.roles[0] ?? 'reviewer';
    setRole(nextRole);
    setComment('');
    mutation.reset();
    // 切换工单时重置表单；mutation 不参与依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, config.roles.join(',')]);

  const mutation = useMutation({
    mutationFn: ({ decision, payload }: { decision: ApprovalDecision; payload: ApprovalPayload }) =>
      api.decideApproval(selectedId, decision, payload),
    onSuccess: () => {
      setComment('');
      void queryClient.invalidateQueries({ queryKey: ['approvals'] });
      void queryClient.invalidateQueries({ queryKey: ['ticket', selectedId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
    },
  });

  const submit = (decision: ApprovalDecision) => {
    if (!selectedId) return;
    mutation.mutate({ decision, payload: { role, comment: comment.trim() } });
  };

  const rejectDisabled = comment.trim().length === 0;

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <h1 className="text-[18px] font-semibold text-slate-900">审批待办</h1>
          <p className="mt-1 text-[12px] text-slate-500">
            真实 /v1/approvals/pending。审批意见按当前身份脱敏，提交前看不到其他审批人的意见。
          </p>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void tasks.refetch()}>
          刷新
        </button>
      </div>

      {tasks.error ? <ErrorPanel error={tasks.error} onRetry={() => void tasks.refetch()} /> : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_1.8fr]">
        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">待办列表</h2>
            <span className="text-[11px] text-slate-400">{items.length} 条</span>
          </div>
          {tasks.isLoading ? (
            <div className="space-y-2 p-3">{[0, 1, 2].map((key) => <div key={key} className="skeleton h-16 w-full" />)}</div>
          ) : items.length === 0 ? (
            <p className="px-3 py-8 text-center text-[12px] text-slate-500">没有待办审批。</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {items.map((task) => {
                const active = task.ticket_id === selectedId;
                return (
                  <li key={task.ticket_id} className="min-w-0">
                    <button
                      type="button"
                      onClick={() => setSearchParams({ ticket: task.ticket_id })}
                      className={`block w-full min-w-0 px-3 py-2.5 text-left ${active ? 'bg-slate-900 text-white' : 'hover:bg-slate-50'}`}
                    >
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className={`font-mono text-[12px] ${active ? 'text-slate-300' : 'text-slate-500'}`}>{task.ticket_no}</span>
                        {active ? (
                          <span className="badge bg-white/20 text-white ring-white/30">{task.state}</span>
                        ) : (
                          <StateBadge state={task.state} />
                        )}
                        <span className={`ml-auto font-mono text-[12px] ${active ? 'text-white' : 'text-slate-700'}`}>{amount(task.amount)}</span>
                      </div>
                      <div className={`mt-1 min-w-0 truncate text-[12px] ${active ? 'text-slate-200' : 'text-slate-600'}`}>{task.business_key}</div>
                      <div className={`mt-1 flex flex-wrap items-center gap-1 text-[11px] ${active ? 'text-slate-300' : 'text-slate-500'}`}>
                        <span>缺：{task.missing_roles.join('、') || '—'}</span>
                        <span>·</span>
                        <span>{task.write_level}</span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="card min-w-0">
          {!selectedId ? (
            <p className="px-3 py-10 text-center text-[12px] text-slate-500">选择左侧工单开始审批。</p>
          ) : detail.isLoading ? (
            <div className="space-y-2 p-3">{[0, 1, 2].map((key) => <div key={key} className="skeleton h-20 w-full" />)}</div>
          ) : detail.error ? (
            <div className="p-3">
              <ErrorPanel error={detail.error} onRetry={() => void detail.refetch()} />
            </div>
          ) : detail.data ? (
            <div className="min-w-0">
              <div className="card-head">
                <div className="min-w-0">
                  <h2 className="section-title">{detail.data.ticket_no} · 审批</h2>
                  <p className="mt-0.5 break-words text-[11px] text-slate-500">{detail.data.business_key}</p>
                </div>
                <div className="flex flex-wrap gap-1">
                  <StateBadge state={detail.data.state} />
                  <LevelBadge level={detail.data.write_level} />
                </div>
              </div>

              <div className="grid grid-cols-1 gap-x-4 gap-y-2 px-3 py-3 text-[12px] sm:grid-cols-2">
                <div><div className="label">金额</div><div className="mt-0.5 font-mono">{amount(detail.data.amount)}</div></div>
                <div><div className="label">创建时间</div><div className="mt-0.5">{dateTime(detail.data.created_at)}</div></div>
                <div className="min-w-0 sm:col-span-2">
                  <div className="label">审批角色进度</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {detail.data.required_roles.map((item) => (
                      <span key={item} className={detail.data?.missing_roles.includes(item) ? '' : ''}>
                        <RoleBadge role={item} />
                        <span className="ml-1 text-[10px] text-slate-500">{detail.data?.missing_roles.includes(item) ? '待提交' : '已满足'}</span>
                      </span>
                    ))}
                  </div>
                </div>
              </div>

              <div className="border-t border-slate-100 px-3 py-3">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <label className="block min-w-0">
                    <span className="label">以该角色提交</span>
                    <select className="input mt-1" value={role} onChange={(event) => setRole(event.target.value as ApprovalRole)}>
                      {(config.roles.length > 0 ? config.roles : ['reviewer']).map((item) => (
                        <option key={item} value={item}>{item}</option>
                      ))}
                    </select>
                  </label>
                  <div className="min-w-0 text-[11px] text-slate-500">
                    <div className="label">当前身份</div>
                    <div className="mt-1 break-all">{config.viewerName}{config.viewerId ? `（${config.viewerId}）` : '（未设置用户 ID，意见默认不展示正文）'}</div>
                  </div>
                </div>

                <label className="mt-2 block min-w-0">
                  <span className="label">审批意见（驳回必填；意见仅你本人可见正文）</span>
                  <textarea
                    className="input mt-1 min-h-[80px] resize-y"
                    maxLength={2000}
                    value={comment}
                    onChange={(event) => setComment(event.target.value)}
                    placeholder="写下你的判断依据；不要转述其他审批人的意见"
                  />
                </label>

                <div className="mt-2 flex flex-wrap items-center justify-end gap-2">
                  <button
                    type="button"
                    className="btn btn-danger btn-sm"
                    disabled={rejectDisabled || mutation.isPending}
                    title={rejectDisabled ? '驳回必须填写意见' : undefined}
                    onClick={() => submit('reject')}
                  >
                    {mutation.isPending && mutation.variables?.decision === 'reject' ? '提交中…' : '驳回'}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={mutation.isPending}
                    onClick={() => submit('approve')}
                  >
                    {mutation.isPending && mutation.variables?.decision === 'approve' ? '提交中…' : '通过'}
                  </button>
                </div>

                {mutation.error ? (
                  <div className="mt-2">
                    <ErrorPanel error={mutation.error} compact />
                  </div>
                ) : null}

                {mutation.isSuccess ? (
                  <div className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">
                    已提交：{mutation.data.decision === 'approved' ? '通过' : '驳回'}（{mutation.data.role}）。
                    {mutation.data.satisfied
                      ? ' 审批链已满足。'
                      : ` 仍需角色：${mutation.data.missing_roles.join('、') || '—'}。`}
                    {mutation.data.ticket_state ? ` 工单状态：${mutation.data.ticket_state}。` : ''}
                  </div>
                ) : null}
              </div>

              <div className="border-t border-slate-100 px-3 py-3">
                <ApprovalOpinions
                  approvals={detail.data.approvals}
                  requiredRoles={detail.data.required_roles}
                  viewerRole={role}
                  viewerId={config.viewerId}
                  initiatorId={detail.data.initiator_id}
                />
              </div>
            </div>
          ) : null}
        </section>
      </div>
    </div>
  );
}
