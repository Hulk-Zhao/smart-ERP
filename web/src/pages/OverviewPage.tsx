import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useApiConfig } from '../api/config';
import { useSession } from '../api/session';
import { ROLE_LABELS, TICKET_STATES } from '../api/types';
import { LevelBadge, StateBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { SessionNotice } from '../components/SessionNotice';
import { IconAlert, IconArrowRight, IconCheck, IconScale } from '../components/icons';
import { amount, dateTime, money, shortId } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

function waitingSince(value: string): string {
  const started = new Date(value).getTime();
  if (!Number.isFinite(started)) return '—';
  const minutes = Math.max(0, Math.floor((Date.now() - started) / 60_000));
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时`;
  return `${Math.floor(hours / 24)} 天`;
}

function Kpi({
  label,
  value,
  hint,
  tone = 'text-slate-900',
  accent,
}: {
  label: string;
  value: string | number;
  hint: string;
  tone?: string;
  accent?: string;
}) {
  return (
    <div className="min-w-0 px-4 py-3" data-kpi={label}>
      <div className="flex items-center gap-1.5">
        <span className={`status-dot ${accent ?? 'bg-slate-300'}`} />
        <span className="label">{label}</span>
      </div>
      <div className={`stat-value ${tone}`}>{value}</div>
      <div className="mt-0.5 truncate text-[10.5px] text-slate-500" title={hint}>
        {hint}
      </div>
    </div>
  );
}

export function OverviewPage() {
  const config = useApiConfig();
  const session = useSession();
  const tickets = useQuery({
    queryKey: ['tickets', 'overview'],
    // 真实接口上限 100（apps/api/deps.py PageLimit le=100）；取满以覆盖 KPI 与风险雷达。
    queryFn: () => api.listTickets({ limit: 100 }),
    // 待办与工单都按会话身份过滤：身份没拿到之前不发，避免把 401/空列表当成「没有待办」。
    enabled: session.canQuery,
  });
  const approvals = useQuery({
    queryKey: ['approvals', 'pending', 'overview'],
    queryFn: () => api.listPendingApprovals({ limit: 20 }),
    enabled: session.canQuery,
  });
  // 对账摘要必须来自真实批次列表：本 scope 已有批次时不能再显示「从本期对账开始」的引导空态。
  const recentBatches = useQuery({
    queryKey: ['recon', 'batches', 'overview'],
    queryFn: () => api.listReconBatches({ limit: 3 }),
    enabled: session.canQuery,
  });
  const latestBatch = recentBatches.data?.items[0];

  const items = tickets.data?.items ?? [];
  const tasks = approvals.data?.items ?? [];
  const pending = items.filter((item) => item.state === '待审批');
  const executing = items.filter((item) => item.state === '执行中' || item.state === '校验中');
  const w3 = items.filter((item) => item.write_level === 'W3');
  const fundAmount = tasks.filter((item) => item.touches_fund).reduce((sum, item) => sum + Number(item.amount || 0), 0);
  const missingRole = tasks.filter((item) => item.approval_missing_roles.length > 0);
  const distribution = TICKET_STATES.map((state) => ({
    state,
    count: items.filter((item) => item.state === state).length,
  })).filter((item) => item.count > 0);

  // 决策队列：W3 > 涉资金 > 缺角色 > 等待时长；同源数据都来自审批待办接口。
  const queue = [...tasks].sort((left, right) => {
    const score = (task: typeof left) =>
      (task.write_level === 'W3' ? 100 : task.write_level === 'W2' ? 40 : 0) +
      (task.touches_fund ? 30 : 0) +
      (task.approval_missing_roles.length > 0 ? 20 : 0);
    const diff = score(right) - score(left);
    if (diff !== 0) return diff;
    return new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
  });

  return (
    <div className="min-w-0 space-y-4">
      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-end justify-between gap-3 px-4 py-4">
          <div className="min-w-0">
            <div className="eyebrow">今日指挥台</div>
            <h1 className="mt-0.5 text-[20px] font-semibold tracking-tight text-slate-900">工作台</h1>
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] text-slate-500">
              <span className="inline-flex items-center gap-1">
                <span className="status-dot bg-emerald-500" />
                {config.scope.org_id}
              </span>
              <span className="text-slate-300">·</span>
              <span>{config.scope.period}</span>
              <span className="text-slate-300">·</span>
              <span>{config.scope.system_ids.join(' / ')}</span>
              <span className="text-slate-300">·</span>
              <span
                title={
                  session.session
                    ? `user ${session.session.user_id} · tenant ${session.session.tenant_id}`
                    : '会话身份未取到'
                }
              >
                {session.session
                  ? `${shortId(session.session.user_id)} · ${session.session.roles.join(' / ')}`
                  : session.status === 'unavailable'
                    ? '未启用演示会话'
                    : config.viewerName}
              </span>
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="hidden items-center gap-1 text-[10.5px] text-slate-400 sm:flex">
              <IconCheck width={12} height={12} />
              数据 {tickets.dataUpdatedAt ? dateTime(new Date(tickets.dataUpdatedAt).toISOString()) : '—'}
            </span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                void tickets.refetch();
                void approvals.refetch();
              }}
            >
              刷新
            </button>
            <Link to="/recon" className="btn btn-primary btn-sm">
              发起对账
            </Link>
          </div>
        </div>
        <div className="grid grid-cols-2 divide-x divide-y divide-slate-100 border-t border-slate-100 sm:grid-cols-4 sm:divide-y-0">
          <Kpi
            label="待你决策"
            value={!session.canQuery ? '—' : tasks.length || (approvals.isLoading ? '—' : 0)}
            hint={
              !session.canQuery
                ? '未启用演示会话，身份未知'
                : missingRole.length > 0
                  ? `${missingRole.length} 张仍缺角色`
                  : '角色已满足'
            }
            accent="bg-amber-500"
            tone={!session.canQuery ? 'text-amber-700' : tasks.length > 0 ? 'text-amber-700' : 'text-slate-900'}
          />
          <Kpi
            label="涉及资金"
            value={!session.canQuery ? '—' : amount(fundAmount)}
            hint={
              !session.canQuery
                ? '未启用演示会话，审批待办未取'
                : `${tasks.filter((item) => item.touches_fund).length} 张待审批含资金写入`
            }
            accent="bg-indigo-500"
          />
          <Kpi
            label="W3 禁止自动执行"
            value={!session.canQuery ? '—' : w3.length}
            hint={!session.canQuery ? '未启用演示会话，工单未取' : '网关整单拒绝，零写入'}
            accent={!session.canQuery ? 'bg-slate-300' : w3.length > 0 ? 'bg-rose-500' : 'bg-slate-300'}
            tone={!session.canQuery ? 'text-slate-900' : w3.length > 0 ? 'text-rose-700' : 'text-slate-900'}
          />
          <Kpi
            label="执行中 / 校验中"
            value={!session.canQuery ? '—' : executing.length}
            hint={!session.canQuery ? '未启用演示会话，工单未取' : `当前页 ${items.length} 条工单样本`}
            accent="bg-sky-500"
          />
        </div>
      </section>

      {session.unavailable ? (
        <SessionNotice error={session.error} onRetry={session.refresh} onOpenSettings={openSettingsPanel} />
      ) : null}

      {tickets.error ? (
        <ErrorPanel error={tickets.error} onRetry={() => void tickets.refetch()} onOpenSettings={openSettingsPanel} />
      ) : null}
      {approvals.error ? (
        <ErrorPanel error={approvals.error} onRetry={() => void approvals.refetch()} onOpenSettings={openSettingsPanel} compact />
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <section className="card min-w-0 overflow-hidden">
          <div className="card-head">
            <div className="flex min-w-0 items-center gap-2">
              <h2 className="section-title">待你决策</h2>
              <span className="badge bg-amber-50 text-amber-700 ring-amber-200">{tasks.length}</span>
              <span className="hidden text-[10.5px] text-slate-400 sm:inline">按 W 等级 → 涉资金 → 等待时长排序</span>
            </div>
            <Link to="/approvals" className="link inline-flex items-center gap-1 text-[12px]">
              审批队列
              <IconArrowRight width={12} height={12} />
            </Link>
          </div>

          {session.status === 'loading' ? (
            <div className="space-y-2 p-3">
              {[0, 1, 2].map((key) => (
                <div key={key} className="skeleton h-[54px] w-full" />
              ))}
            </div>
          ) : session.unavailable ? (
            <div className="p-3">
              <SessionNotice
                error={session.error}
                onRetry={session.refresh}
                onOpenSettings={openSettingsPanel}
                compact
              />
            </div>
          ) : approvals.isLoading ? (
            <div className="space-y-2 p-3">
              {[0, 1, 2, 3].map((key) => (
                <div key={key} className="skeleton h-[58px] w-full" />
              ))}
            </div>
          ) : queue.length === 0 ? (
            <div className="p-3">
              <EmptyState
                variant="no-data"
                compact
                description="没有待你决策的审批。可以核对当前身份与角色，或发起新一轮对账。"
                actions={
                  <>
                    <button type="button" className="btn btn-secondary btn-sm" onClick={() => void approvals.refetch()}>
                      刷新
                    </button>
                    <Link to="/recon" className="btn btn-secondary btn-sm">
                      发起对账
                    </Link>
                  </>
                }
              />
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {queue.slice(0, 6).map((task) => {
                const risk = task.write_level === 'W3' || task.touches_fund;
                return (
                  <li key={task.ticket_id} className="min-w-0">
                    <Link
                      to={`/approvals?ticket=${encodeURIComponent(task.ticket_id)}`}
                      className="flex min-w-0 items-center gap-3 px-4 py-2.5 hover:bg-slate-50"
                    >
                      <span
                        className={`h-8 w-[3px] shrink-0 rounded-full ${
                          task.write_level === 'W3' ? 'bg-rose-500' : risk ? 'bg-amber-400' : 'bg-slate-200'
                        }`}
                      />
                      <span className="min-w-0 flex-1">
                        <span className="flex min-w-0 flex-wrap items-center gap-1.5">
                          <span className="font-mono text-[11.5px] text-slate-500">{task.ticket_no}</span>
                          <span className="max-w-full truncate text-[12.5px] font-medium text-slate-900">
                            {task.business_key}
                          </span>
                          <StateBadge state={task.state} />
                          <LevelBadge level={task.write_level} />
                          {task.touches_fund ? (
                            <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉资金</span>
                          ) : null}
                        </span>
                        <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10.5px] text-slate-400">
                          <span>{task.scene_code}</span>
                          <span>发起 {shortId(task.initiator_id)}</span>
                          <span>等 {waitingSince(task.created_at)}</span>
                          {task.approval_missing_roles.length > 0 ? (
                            <span className="inline-flex items-center gap-0.5 text-amber-600">
                              <IconAlert width={11} height={11} />
                              缺 {task.approval_missing_roles.map((role) => ROLE_LABELS[role] ?? role).join('、')}
                            </span>
                          ) : (
                            <span className="text-emerald-600">角色齐</span>
                          )}
                        </span>
                      </span>
                      <span className="shrink-0 text-right">
                        <span className="block font-mono text-[12.5px] font-medium text-slate-900 num">
                          {amount(task.amount)}
                        </span>
                        <span className="text-[10px] text-indigo-600">去审批 →</span>
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <div className="min-w-0 space-y-4">
          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">风险雷达</h2>
              <span className="text-[10.5px] text-slate-400">当前 scope</span>
            </div>
            <div className="card-body space-y-3">
              {[
                {
                  label: 'W3 禁止自动执行',
                  value: w3.length,
                  tone: 'bg-rose-500',
                  hint: w3.length > 0 ? '只能人工处置或终止' : '没有 W3 工单',
                },
                {
                  label: '审批缺角色',
                  value: missingRole.length,
                  tone: 'bg-amber-400',
                  hint: missingRole.length > 0 ? '通过后仍不会推进到执行' : '角色已满足',
                },
                {
                  label: '执行中 / 校验中',
                  value: executing.length,
                  tone: 'bg-indigo-500',
                  hint: '有外部写入在途',
                },
              ].map((row) => (
                <div key={row.label} className="min-w-0">
                  <div className="flex items-center justify-between gap-2 text-[11.5px]">
                    <span className="truncate text-slate-600">{row.label}</span>
                    <span className="font-mono font-medium text-slate-900 num">{row.value}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <div className="progress flex-1">
                      <span
                        className={row.tone}
                        style={{ width: `${items.length ? Math.min(100, (row.value / items.length) * 100) : 0}%` }}
                      />
                    </div>
                    <span className="w-[112px] shrink-0 truncate text-right text-[10px] text-slate-400" title={row.hint}>
                      {row.hint}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">对账摘要</h2>
              <Link to="/recon" className="link text-[12px]">
                全部批次
              </Link>
            </div>
            <div className="card-body">
              {recentBatches.error ? (
                <ErrorPanel
                  error={recentBatches.error}
                  compact
                  onRetry={() => void recentBatches.refetch()}
                  onOpenSettings={openSettingsPanel}
                />
              ) : recentBatches.isLoading ? (
                <div className="skeleton h-[64px] w-full" />
              ) : latestBatch ? (
                <Link
                  to={`/recon/${latestBatch.batch_id}`}
                  className="block rounded-lg bg-indigo-50/70 p-2.5 hover:bg-indigo-50"
                  data-testid="overview-recon-summary"
                >
                  <div className="flex items-start gap-2.5">
                    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white text-indigo-600 ring-1 ring-indigo-100">
                      <IconScale />
                    </span>
                    <div className="min-w-0 text-[11px] leading-[17px] text-indigo-900">
                      <div className="font-medium">
                        最近批次 <span className="font-mono">{shortId(latestBatch.batch_id)}</span>
                      </div>
                      <p className="text-indigo-700/80">
                        {latestBatch.status} · 创建 {dateTime(latestBatch.created_at)}
                      </p>
                    </div>
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-600">
                    <span>
                      行数{' '}
                      <span className="font-mono text-slate-800">
                        {latestBatch.line_count ?? '—'}
                      </span>
                    </span>
                    <span>
                      差异{' '}
                      <span className="font-mono text-slate-800">
                        {latestBatch.diff_count ?? '—'}
                      </span>
                    </span>
                    <span>
                      差异金额{' '}
                      <span className="font-mono text-slate-800">{money(latestBatch.diff_amount)}</span>
                    </span>
                    <span className="ml-auto text-indigo-600">查看批次 →</span>
                  </div>
                </Link>
              ) : (
                <div className="flex items-start gap-2.5 rounded-lg bg-indigo-50/70 p-2.5">
                  <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-white text-indigo-600 ring-1 ring-indigo-100">
                    <IconScale />
                  </span>
                  <div className="min-w-0 text-[11px] leading-[17px] text-indigo-900">
                    <div className="font-medium">该 scope 还没有对账批次</div>
                    <p className="text-indigo-700/80">
                      发起后在这里看到最近批次的行数、差异与计量入口；服务端没有批次时不会伪造数字。
                    </p>
                  </div>
                </div>
              )}
              <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
                <div className="rounded-lg border border-slate-200 px-2.5 py-2">
                  <div className="label">待审批</div>
                  <div className="mt-0.5 font-mono text-[15px] font-medium num">
                    {session.canQuery ? tasks.length : '—'}
                  </div>
                </div>
                <div className="rounded-lg border border-slate-200 px-2.5 py-2">
                  <div className="label">当前页工单</div>
                  <div className="mt-0.5 font-mono text-[15px] font-medium num">
                    {session.canQuery ? items.length : '—'}
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">状态分布</h2>
              <span className="text-[10.5px] text-slate-400">
                当前页 {items.length} 条 · 非零状态 {distribution.length} 个（全部列出）
              </span>
            </div>
            <div className="card-body space-y-2">
              {distribution.length === 0 ? (
                <EmptyState variant="no-data" compact description="当前页没有可统计的状态分布。" />
              ) : (
                distribution.map((item) => (
                  <div key={item.state} className="min-w-0" data-testid="distribution-row">
                    <div className="flex items-center justify-between gap-2 text-[11px] text-slate-600">
                      <span className="truncate">{item.state}</span>
                      <span className="font-mono num">{item.count}</span>
                    </div>
                    <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                      <div
                        className="h-full rounded-full bg-slate-700"
                        style={{ width: `${(item.count / items.length) * 100}%` }}
                      />
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        </div>
      </div>

      <section className="card min-w-0 overflow-hidden">
        <div className="card-head">
          <div className="flex items-center gap-2">
            <h2 className="section-title">最近工单</h2>
            <span className="text-[10.5px] text-slate-400">最多 8 条 · 全量以工单页为准</span>
          </div>
          <Link to="/tickets" className="link text-[12px]">
            全部工单
          </Link>
        </div>

        {tickets.isLoading ? (
          <div className="space-y-2 p-3">
            {[0, 1, 2, 3].map((key) => (
              <div key={key} className="skeleton h-9 w-full" />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="p-3">
            <EmptyState variant="no-data" compact description="当前 scope 下没有工单；确认 org / period / systems 后再看。" />
          </div>
        ) : (
          <>
            <div className="hidden overflow-x-auto sm:block">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>工单号</th>
                    <th>业务键</th>
                    <th>状态</th>
                    <th>等级</th>
                    <th className="text-right">金额</th>
                    <th>场景</th>
                    <th>更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {items.slice(0, 8).map((ticket) => (
                    <tr key={ticket.ticket_id}>
                      <td>
                        <Link to={`/tickets/${ticket.ticket_id}`} className="font-mono text-[11.5px] text-indigo-600 hover:underline">
                          {ticket.ticket_no}
                        </Link>
                      </td>
                      <td className="max-w-[240px] truncate">
                        <Link to={`/tickets/${ticket.ticket_id}`} className="font-medium text-slate-900 hover:underline">
                          {ticket.business_key}
                        </Link>
                      </td>
                      <td>
                        <StateBadge state={ticket.state} />
                      </td>
                      <td>
                        <LevelBadge level={ticket.write_level} />
                      </td>
                      <td className="num font-medium">{amount(ticket.amount)}</td>
                      <td className="max-w-[160px] truncate text-slate-500">{ticket.scene_code}</td>
                      <td className="whitespace-nowrap text-[11px] text-slate-500">{dateTime(ticket.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <ul className="divide-y divide-slate-100 sm:hidden">
              {items.slice(0, 8).map((ticket) => (
                <li key={ticket.ticket_id}>
                  <Link to={`/tickets/${ticket.ticket_id}`} className="block px-3 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-[11px] text-slate-500">{ticket.ticket_no}</span>
                      <StateBadge state={ticket.state} />
                      <LevelBadge level={ticket.write_level} />
                      <span className="ml-auto font-mono text-[12px] font-medium num">{amount(ticket.amount)}</span>
                    </div>
                    <div className="mt-1 truncate text-[12.5px] font-medium text-slate-900">{ticket.business_key}</div>
                    <div className="mt-0.5 text-[10.5px] text-slate-400">{dateTime(ticket.updated_at)}</div>
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  );
}
