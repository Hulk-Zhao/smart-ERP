import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, type ApprovalDecision, type ApprovalPayload } from '../api/client';
import { useApiConfig, type ApprovalRole } from '../api/config';
import { useSession } from '../api/session';
import { ApprovalOpinions } from '../components/ApprovalOpinions';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { SessionNotice } from '../components/SessionNotice';
import { LevelBadge, RoleBadge, StateBadge } from '../components/Badges';
import { IconAlert, IconCheck } from '../components/icons';
import { amount, dateTime } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

type RiskFilter = 'all' | 'fund' | 'w3' | 'w2' | 'missing';

const FILTERS: { key: RiskFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'fund', label: '涉资金' },
  { key: 'w3', label: 'W3' },
  { key: 'w2', label: 'W2' },
  { key: 'missing', label: '缺角色' },
];

function waitingSince(value: string): string {
  const started = new Date(value).getTime();
  if (!Number.isFinite(started)) return '—';
  const minutes = Math.max(0, Math.floor((Date.now() - started) / 60_000));
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} 小时` : `${Math.floor(hours / 24)} 天`;
}

export function ApprovalsPage() {
  const config = useApiConfig();
  const session = useSession();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [comment, setComment] = useState('');
  const [role, setRole] = useState<ApprovalRole>(config.roles[0] ?? 'reviewer');
  const [filter, setFilter] = useState<RiskFilter>('all');

  const tasks = useQuery({
    queryKey: ['approvals', 'pending', 'page'],
    queryFn: () => api.listPendingApprovals({ limit: 50 }),
    // 「待你决策」由会话角色决定，必须先拿到 /v1/auth/session 的身份再查。
    enabled: session.canQuery,
  });

  const items = tasks.data?.items ?? [];
  const visibleItems = useMemo(
    () =>
      items.filter((item) => {
        if (filter === 'fund') return item.touches_fund;
        if (filter === 'w3') return item.write_level === 'W3';
        if (filter === 'w2') return item.write_level === 'W2';
        if (filter === 'missing') return item.approval_missing_roles.length > 0;
        return true;
      }),
    [filter, items],
  );
  const selectedId = searchParams.get('ticket') || visibleItems[0]?.ticket_id || items[0]?.ticket_id || '';
  const selected = items.find((item) => item.ticket_id === selectedId);

  const detail = useQuery({
    queryKey: ['ticket', selectedId],
    queryFn: () => api.getTicket(selectedId),
    enabled: session.canQuery && Boolean(selectedId),
  });

  useEffect(() => {
    // 会话身份到达前默认落在第一个可用角色；会话到达后按缺角色规则重算。
    const fallback = config.roles[0] ?? 'reviewer';
    if (!selected) {
      setRole(fallback);
      return;
    }
    const nextRole = config.roles.find((item) => selected.approval_missing_roles.includes(item)) ?? fallback;
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

  // A=通过，R=驳回；输入框聚焦时不触发，避免误提交。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (!selectedId || mutation.isPending) return;
      if (event.key.toLowerCase() === 'a') {
        event.preventDefault();
        submit('approve');
      } else if (event.key.toLowerCase() === 'r') {
        event.preventDefault();
        if (!rejectDisabled) submit('reject');
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
    // submit 依赖 selectedId / role / comment，下方依赖已覆盖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, role, comment, mutation.isPending, rejectDisabled]);

  const kpis = selected
    ? [
        { label: '金额', value: amount(selected.amount), mono: true },
        { label: '等级', value: selected.write_level, mono: false },
        { label: '缺角色', value: `${selected.approval_missing_roles.length} 个`, mono: false },
        { label: '等待', value: waitingSince(selected.created_at), mono: false },
      ]
    : [];

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <div className="eyebrow">双人复核</div>
          <h1 className="mt-0.5 text-[20px] font-semibold tracking-tight text-slate-900">审批</h1>
          <p className="mt-1 text-[11.5px] text-slate-500">
            真实 /v1/approvals/pending。意见按当前身份脱敏；快捷键 A 通过、R 驳回。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge bg-slate-100 text-slate-600 ring-slate-200">
            队列 {session.canQuery ? items.length : '—'}
          </span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void tasks.refetch()}>
            刷新
          </button>
        </div>
      </div>

      {tasks.error ? (
        <ErrorPanel error={tasks.error} onRetry={() => void tasks.refetch()} onOpenSettings={openSettingsPanel} />
      ) : null}

      {session.unavailable ? (
        <SessionNotice error={session.error} onRetry={session.refresh} onOpenSettings={openSettingsPanel} />
      ) : null}

      {session.status === 'loading' ? (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[340px_minmax(0,1fr)]">
          <div className="skeleton h-72 w-full" />
          <div className="skeleton h-72 w-full" />
        </div>
      ) : session.canQuery ? (
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[340px_minmax(0,1fr)]">
        <section className="card min-w-0 overflow-hidden self-start">
          <div className="card-head">
            <h2 className="section-title">待办列表</h2>
            <span className="text-[10.5px] text-slate-400">{visibleItems.length} / {items.length}</span>
          </div>
          <div className="flex flex-wrap gap-1 border-b border-slate-100 px-2.5 py-2">
            {FILTERS.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`chip ${filter === item.key ? 'chip-active' : ''}`}
                onClick={() => setFilter(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
          {tasks.isLoading ? (
            <div className="space-y-2 p-2.5">{[0, 1, 2].map((key) => <div key={key} className="skeleton h-16 w-full" />)}</div>
          ) : visibleItems.length === 0 ? (
            <div className="p-2.5">
              <EmptyState
                variant="no-data"
                compact
                description={items.length === 0 ? '本期确实无数据：没有待办审批。' : '当前筛选下没有审批；切回「全部」查看。'}
                actions={
                  <button type="button" className="btn btn-secondary btn-sm" onClick={() => setFilter('all')}>
                    全部
                  </button>
                }
              />
            </div>
          ) : (
            <ul className="max-h-[calc(100vh-17rem)] divide-y divide-slate-100 overflow-y-auto">
              {visibleItems.map((task) => {
                const active = task.ticket_id === selectedId;
                return (
                  <li key={task.ticket_id} className="min-w-0">
                    <button
                      type="button"
                      onClick={() => setSearchParams({ ticket: task.ticket_id })}
                      className={`relative block w-full min-w-0 px-3 py-2.5 text-left ${
                        active ? 'bg-indigo-50/80' : 'hover:bg-slate-50'
                      }`}
                    >
                      {active ? <span className="absolute inset-y-2 left-0 w-[3px] rounded-r-full bg-indigo-500" /> : null}
                      <div className="flex min-w-0 items-center gap-1.5">
                        <span className="font-mono text-[11px] text-slate-500">{task.ticket_no}</span>
                        <StateBadge state={task.state} />
                        <LevelBadge level={task.write_level} />
                        {task.touches_fund ? <span className="badge bg-amber-50 text-amber-700 ring-amber-200">资金</span> : null}
                        <span className="ml-auto font-mono text-[12px] font-medium text-slate-900 num">{amount(task.amount)}</span>
                      </div>
                      <div className="mt-1 min-w-0 truncate text-[12.5px] font-medium text-slate-800">{task.business_key}</div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10.5px] text-slate-400">
                        <span>等 {waitingSince(task.created_at)}</span>
                        {task.approval_missing_roles.length > 0 ? (
                          <span className="inline-flex items-center gap-0.5 text-amber-600">
                            <IconAlert width={10} height={10} />
                            缺 {task.approval_missing_roles.join('、')}
                          </span>
                        ) : (
                          <span className="text-emerald-600">角色齐</span>
                        )}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section className="card min-w-0 overflow-hidden">
          {!selectedId ? (
            <div className="p-3">
              <EmptyState variant="no-data" compact description="选择左侧工单开始审批。" />
            </div>
          ) : detail.isPending ? (
            <div className="space-y-2 p-3">{[0, 1, 2].map((key) => <div key={key} className="skeleton h-20 w-full" />)}</div>
          ) : detail.error ? (
            <div className="p-3">
              <ErrorPanel
                error={detail.error}
                onRetry={() => void detail.refetch()}
                onOpenSettings={openSettingsPanel}
                notFoundKind="ticket"
                backTo={{ to: '/approvals', label: '← 返回审批待办' }}
              />
            </div>
          ) : detail.data ? (
            <div className="flex min-h-0 flex-col">
              <div className="flex flex-wrap items-start justify-between gap-2 border-b border-slate-200 px-4 py-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <h2 className="font-mono text-[15px] font-semibold text-slate-900">{detail.data.ticket_no}</h2>
                    <StateBadge state={detail.data.state} />
                    <LevelBadge level={detail.data.write_level} />
                    {detail.data.touches_fund ? (
                      <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉及资金</span>
                    ) : null}
                  </div>
                  <p className="mt-0.5 break-words text-[12px] text-slate-500">{detail.data.business_key}</p>
                </div>
                <span className="text-[10.5px] text-slate-400">创建 {dateTime(detail.data.created_at)}</span>
              </div>

              <div className="grid grid-cols-2 divide-x divide-y divide-slate-100 border-b border-slate-100 text-[12px] sm:grid-cols-4 sm:divide-y-0">
                {kpis.map((item) => (
                  <div key={item.label} className="min-w-0 px-3 py-2">
                    <div className="label">{item.label}</div>
                    <div className={`mt-0.5 truncate text-[14px] font-medium text-slate-900 ${item.mono ? 'font-mono num' : ''}`}>
                      {item.value}
                    </div>
                  </div>
                ))}
              </div>

              <div className="grid grid-cols-1 gap-x-4 gap-y-2 px-4 py-3 text-[12px] sm:grid-cols-2">
                <div className="min-w-0 sm:col-span-2">
                  <div className="label">审批角色进度</div>
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {detail.data.approval_required_roles.length === 0 ? (
                      <span className="text-[12px] text-slate-500">无需审批角色</span>
                    ) : null}
                    {detail.data.approval_required_roles.map((item) => {
                      const missing = detail.data!.approval_missing_roles.includes(item);
                      return (
                        <span key={item} className="inline-flex items-center gap-1">
                          <RoleBadge role={item} />
                          <span className={`text-[10px] ${missing ? 'text-amber-700' : 'text-emerald-700'}`}>
                            {missing ? '待提交' : '已满足'}
                          </span>
                        </span>
                      );
                    })}
                  </div>
                  {detail.data.approval_missing_roles.length > 0 ? (
                    <p className="mt-2 rounded-lg bg-amber-50 px-2.5 py-1.5 text-[10.5px] leading-4 text-amber-800">
                      仍需角色：{detail.data.approval_missing_roles.join('、')}。现在通过只会记录你的决定，不会推进到执行。
                    </p>
                  ) : detail.data.execution_missing_roles.length > 0 ? (
                    <p className="mt-2 rounded-lg bg-sky-50 px-2.5 py-1.5 text-[10.5px] leading-4 text-sky-800">
                      审批链已满足；执行仍需角色：{detail.data.execution_missing_roles.join('、')}
                      （B 级连接器强制双签，审批一票 ≠ 执行一票）。
                    </p>
                  ) : (
                    <p className="mt-2 inline-flex items-center gap-1 rounded-lg bg-emerald-50 px-2.5 py-1.5 text-[10.5px] text-emerald-800">
                      <IconCheck width={11} height={11} />
                      审批链已满足，通过后进入执行。
                    </p>
                  )}
                </div>

                <label className="block min-w-0">
                  <span className="label">以该角色提交</span>
                  <select className="input mt-1 h-8 py-1" value={role} onChange={(event) => setRole(event.target.value as ApprovalRole)}>
                    {(config.roles.length > 0 ? config.roles : ['reviewer']).map((item) => (
                      <option key={item} value={item}>{item}</option>
                    ))}
                  </select>
                  {config.roles.length === 0 ? (
                    <span className="mt-1 block text-[10.5px] leading-4 text-amber-700">
                      会话未返回审批角色，提交会被服务端拒绝
                    </span>
                  ) : null}
                </label>
                <div className="min-w-0 text-[11px] text-slate-500">
                  <div className="label">当前身份</div>
                  <div className="mt-1 break-all">
                    {config.viewerName}
                    {config.viewerId
                      ? `（${config.viewerId}）`
                      : '（会话未返回 user_id，意见默认不展示正文）'}
                  </div>
                </div>
              </div>

              <div className="px-4 pb-3">
                <label className="block min-w-0">
                  <span className="label">审批意见（驳回必填；意见仅你本人可见正文）</span>
                  <textarea
                    className="input mt-1 min-h-[86px] resize-y text-[12px]"
                    maxLength={2000}
                    value={comment}
                    onChange={(event) => setComment(event.target.value)}
                    placeholder="写下你的判断依据；不要转述其他审批人的意见"
                  />
                </label>
              </div>

              <div className="border-t border-slate-200 bg-white px-4 py-2.5">
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <span className="mr-auto hidden items-center gap-1 text-[10px] text-slate-400 sm:flex">
                    <kbd className="kbd">A</kbd> 通过
                    <kbd className="kbd">R</kbd> 驳回
                  </span>
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
                    <ErrorPanel error={mutation.error} onOpenSettings={openSettingsPanel} compact />
                  </div>
                ) : null}

                {mutation.isSuccess ? (
                  <div className="mt-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">
                    已提交：{mutation.data.decision === 'approved' ? '通过' : '驳回'}（{mutation.data.role}）。
                    {mutation.data.satisfied
                      ? ' 审批链已满足。'
                      : ` 仍需角色：${mutation.data.approval_missing_roles.join('、') || '—'}。`}
                    {mutation.data.ticket_state ? ` 工单状态：${mutation.data.ticket_state}。` : ''}
                  </div>
                ) : null}
              </div>

              <div className="border-t border-slate-100 px-4 py-3">
                <ApprovalOpinions
                  approvals={detail.data.approvals}
                  requiredRoles={detail.data.approval_required_roles}
                  viewerRole={role}
                  viewerId={config.viewerId}
                  initiatorId={detail.data.initiator_id}
                />
              </div>
            </div>
          ) : null}
        </section>
      </div>
      ) : null}
    </div>
  );
}
