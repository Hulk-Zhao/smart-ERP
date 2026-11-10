import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useApiConfig } from '../api/config';
import { TICKET_STATES } from '../api/types';
import { LevelBadge, StateBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { amount, dateTime, shortId } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

function Metric({ label, value, hint, tone = '' }: { label: string; value: string | number; hint?: string; tone?: string }) {
  return (
    <div className="card min-w-0 p-3">
      <div className="label">{label}</div>
      <div className={`mt-1 truncate text-[20px] font-semibold ${tone || 'text-slate-900'}`}>{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-slate-500">{hint}</div> : null}
    </div>
  );
}

export function OverviewPage() {
  const config = useApiConfig();
  const tickets = useQuery({
    queryKey: ['tickets', 'overview'],
    queryFn: () => api.listTickets({ limit: 20 }),
  });
  const approvals = useQuery({
    queryKey: ['approvals', 'pending', 'overview'],
    queryFn: () => api.listPendingApprovals({ limit: 10 }),
  });

  const items = tickets.data?.items ?? [];
  const pending = items.filter((item) => item.state === '待审批');
  const executing = items.filter((item) => item.state === '执行中' || item.state === '校验中');
  const finished = items.filter((item) => item.state === '已完成');
  const w3 = items.filter((item) => item.write_level === 'W3');
  const distribution = TICKET_STATES.map((state) => ({
    state,
    count: items.filter((item) => item.state === state).length,
  })).filter((item) => item.count > 0);

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <h1 className="text-[18px] font-semibold text-slate-900">工作台</h1>
          <p className="mt-1 break-words text-[12px] text-slate-500">
            {config.scope.org_id} · {config.scope.period} · 系统 {config.scope.system_ids.join(' / ')} · 身份 {config.viewerName}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => { void tickets.refetch(); void approvals.refetch(); }}>
            刷新
          </button>
          <Link to="/recon" className="btn btn-primary btn-sm">
            发起对账
          </Link>
        </div>
      </div>

      {tickets.error ? (
        <ErrorPanel error={tickets.error} onRetry={() => void tickets.refetch()} onOpenSettings={openSettingsPanel} />
      ) : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Metric label="待审批（当前页）" value={pending.length} hint={`共取 ${items.length} 条工单`} tone="text-amber-700" />
        <Metric label="待办审批任务" value={approvals.data?.items.length ?? '—'} hint={approvals.error ? '审批待办加载失败' : '需要你处理的角色'} />
        <Metric label="执行中 / 校验中" value={executing.length} hint="有外部写入在途" tone="text-blue-700" />
        <Metric label="W3（禁止自动执行）" value={w3.length} hint="网关整单拒绝，零写入" tone={w3.length > 0 ? 'text-rose-700' : 'text-slate-900'} />
      </div>

      {approvals.error ? (
        <ErrorPanel error={approvals.error} onRetry={() => void approvals.refetch()} onOpenSettings={openSettingsPanel} compact />
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[2fr_1fr]">
        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">最近工单</h2>
            <Link to="/tickets" className="link text-[12px]">全部工单</Link>
          </div>

          {tickets.isLoading ? (
            <div className="space-y-2 p-3">
              {[0, 1, 2, 3].map((key) => <div key={key} className="skeleton h-12 w-full" />)}
            </div>
          ) : items.length === 0 ? (
            <div className="p-3">
              <EmptyState variant="no-data" compact description="当前 scope 下没有工单；确认 org / period / systems 后再看。" />
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {items.slice(0, 8).map((ticket) => (
                <li key={ticket.ticket_id} className="min-w-0 px-3 py-2.5">
                  <Link to={`/tickets/${ticket.ticket_id}`} className="block min-w-0 rounded-lg px-1 py-0.5 hover:bg-slate-50">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span className="font-mono text-[12px] text-slate-500">{ticket.ticket_no}</span>
                      <span className="min-w-0 truncate text-[13px] font-medium text-slate-900">{ticket.business_key}</span>
                      <StateBadge state={ticket.state} />
                      <LevelBadge level={ticket.write_level} />
                      <span className="ml-auto font-mono text-[12px] text-slate-700">{amount(ticket.amount)}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-400">
                      <span>{ticket.scene_code}</span>
                      <span>发起人 {shortId(ticket.initiator_id)}</span>
                      <span>更新 {dateTime(ticket.updated_at)}</span>
                      {ticket.touches_fund ? <span className="text-amber-600">涉及资金</span> : null}
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="min-w-0 space-y-4">
          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">状态分布（当前页）</h2>
              <span className="text-[11px] text-slate-400">共 {items.length} 条</span>
            </div>
            <div className="card-body space-y-2">
              {distribution.length === 0 ? (
                <EmptyState variant="no-data" compact description="当前页没有可统计的状态分布。" />
              ) : (
                distribution.map((item) => (
                  <div key={item.state} className="min-w-0">
                    <div className="flex items-center justify-between gap-2 text-[11px] text-slate-600">
                      <span className="truncate">{item.state}</span>
                      <span className="font-mono">{item.count}</span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                      <div className="h-full rounded-full bg-slate-700" style={{ width: `${(item.count / items.length) * 100}%` }} />
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">我的待办</h2>
              <Link to="/approvals" className="link text-[12px]">去审批</Link>
            </div>
            <div className="card-body space-y-2">
              {(approvals.data?.items ?? []).length === 0 ? (
                <EmptyState variant="no-data" compact description="没有待办审批；如需审批请确认当前角色。" />
              ) : (
                (approvals.data?.items ?? []).slice(0, 4).map((task) => (
                  <Link
                    key={task.ticket_id}
                    to={`/approvals?ticket=${task.ticket_id}`}
                    className="block min-w-0 rounded-lg border border-slate-200 px-2.5 py-2 text-[12px] hover:bg-slate-50"
                  >
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium text-slate-800">{task.ticket_no}</span>
                      <span className="ml-auto shrink-0 font-mono text-slate-600">{amount(task.amount)}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      <StateBadge state={task.state} />
                      <LevelBadge level={task.write_level} />
                      <span className="badge bg-slate-100 text-slate-500 ring-slate-200">
                        缺 {task.missing_roles.join('、') || '—'}
                      </span>
                    </div>
                  </Link>
                ))
              )}
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">完成情况</h2>
            </div>
            <div className="card-body grid grid-cols-2 gap-2 text-[12px]">
              <div>
                <div className="label">已完成</div>
                <div className="mt-0.5 font-mono text-[16px] text-emerald-700">{finished.length}</div>
              </div>
              <div>
                <div className="label">当前页总量</div>
                <div className="mt-0.5 font-mono text-[16px]">{tickets.data?.items.length ?? '—'}</div>
              </div>
              <div className="col-span-2 text-[10px] leading-4 text-slate-400">
                指标仅基于当前页样本，不是全量统计；全量指标以服务端报表为准。
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
