import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { TICKET_STATES } from '../api/types';
import { LevelBadge, StateBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { IconArrowRight, IconSearch } from '../components/icons';
import { amount, dateTime, shortId } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

export function TicketsPage() {
  const [state, setState] = useState<string>('');
  const [ticketNo, setTicketNo] = useState('');
  const [cursor, setCursor] = useState<string | undefined>(undefined);

  const query = useQuery({
    queryKey: ['tickets', 'list', state, ticketNo.trim(), cursor ?? 'first'],
    queryFn: () =>
      api.listTickets({
        state: state || undefined,
        ticket_no: ticketNo.trim() || undefined,
        cursor: cursor ?? undefined,
        limit: 50,
      }),
  });

  const items = query.data?.items ?? [];
  const w3 = items.filter((item) => item.write_level === 'W3');
  const fund = items.filter((item) => item.touches_fund);
  const waiting = items.filter((item) => item.state === '待审批' || item.state === '待确认');

  const applyState = (next: string) => {
    setState(next);
    setCursor(undefined);
  };

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <div className="eyebrow">执行台账</div>
          <h1 className="mt-0.5 text-[20px] font-semibold tracking-tight text-slate-900">工单</h1>
          <p className="mt-1 text-[11.5px] text-slate-500">
            真实 /v1/tickets，按 G-05 scope 过滤；点击进入 12 状态时间线与审计。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="badge bg-slate-100 text-slate-600 ring-slate-200">本页 {items.length} 条</span>
          <span className="badge bg-rose-50 text-rose-700 ring-rose-200">W3 {w3.length}</span>
          <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉资金 {fund.length}</span>
          <span className="badge bg-indigo-50 text-indigo-700 ring-indigo-200">待决策 {waiting.length}</span>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void query.refetch()}>
            刷新
          </button>
        </div>
      </div>

      <div className="card min-w-0">
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-3 py-2.5">
          <div className="relative min-w-0 flex-1 sm:max-w-[280px]">
            <IconSearch className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" width={14} height={14} />
            <input
              className="input h-8 py-1 pl-8 text-[12px]"
              placeholder="按工单号精确查询"
              value={ticketNo}
              onChange={(event) => {
                setTicketNo(event.target.value);
                setCursor(undefined);
              }}
            />
          </div>
          <span className="hidden text-[10.5px] text-slate-400 sm:inline">筛选状态</span>
          {state || ticketNo ? (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setState('');
                setTicketNo('');
                setCursor(undefined);
              }}
            >
              清空
            </button>
          ) : null}
          <span className="ml-auto text-[10.5px] text-slate-400">
            {query.isFetching ? '同步中…' : query.dataUpdatedAt ? `更新 ${dateTime(new Date(query.dataUpdatedAt).toISOString())}` : '—'}
          </span>
        </div>

        <div className="flex min-w-0 flex-wrap gap-1.5 px-3 py-2">
          <button type="button" className={`chip ${state === '' ? 'chip-active' : ''}`} onClick={() => applyState('')}>
            全部状态
          </button>
          {TICKET_STATES.map((item) => (
            <button
              key={item}
              type="button"
              className={`chip ${state === item ? 'chip-active' : ''}`}
              onClick={() => applyState(item)}
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      {query.error ? (
        <ErrorPanel error={query.error} onRetry={() => void query.refetch()} onOpenSettings={openSettingsPanel} />
      ) : null}

      {query.isLoading ? (
        <div className="card space-y-2 p-3">
          {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((key) => (
            <div key={key} className="skeleton h-9 w-full" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          variant="no-data"
          description="本期在 org / period / systems 范围内没有工单；可清空状态筛选，或确认 scope 是否收得太窄。"
          actions={
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                setState('');
                setTicketNo('');
                setCursor(undefined);
              }}
            >
              全部
            </button>
          }
        />
      ) : (
        <div className="card min-w-0 overflow-hidden">
          <div className="hidden max-h-[calc(100vh-18rem)] overflow-auto md:block">
            <table className="data-table">
              <thead>
                <tr>
                  <th className="w-[104px]">工单号</th>
                  <th>业务键</th>
                  <th className="w-[92px]">状态</th>
                  <th className="w-[64px]">等级</th>
                  <th className="w-[128px] text-right">金额</th>
                  <th className="w-[132px]">场景</th>
                  <th className="w-[108px]">系统</th>
                  <th className="w-[96px]">发起人</th>
                  <th className="w-[148px]">更新时间</th>
                  <th className="w-[58px]" />
                </tr>
              </thead>
              <tbody>
                {items.map((ticket) => (
                  <tr key={ticket.ticket_id} className="group">
                    <td className="whitespace-nowrap">
                      <Link
                        to={`/tickets/${ticket.ticket_id}`}
                        className="font-mono text-[11.5px] text-indigo-600 hover:underline"
                      >
                        {ticket.ticket_no}
                      </Link>
                    </td>
                    <td className="max-w-[280px]">
                      <Link
                        to={`/tickets/${ticket.ticket_id}`}
                        className="block truncate font-medium text-slate-900 hover:underline"
                        title={ticket.business_key}
                      >
                        {ticket.business_key}
                      </Link>
                      {ticket.touches_fund ? (
                        <span className="mt-0.5 inline-flex text-[10px] text-amber-600">涉及资金</span>
                      ) : null}
                    </td>
                    <td>
                      <StateBadge state={ticket.state} />
                    </td>
                    <td>
                      <LevelBadge level={ticket.write_level} />
                    </td>
                    <td className="num font-medium text-slate-900">{amount(ticket.amount)}</td>
                    <td className="max-w-[132px] truncate text-[11.5px] text-slate-500" title={ticket.scene_code}>
                      {ticket.scene_code}
                    </td>
                    <td className="max-w-[108px] truncate text-[11.5px] text-slate-500" title={ticket.systems.join(' / ')}>
                      {ticket.systems.join(' / ')}
                    </td>
                    <td className="font-mono text-[11px] text-slate-500">{shortId(ticket.initiator_id)}</td>
                    <td className="whitespace-nowrap text-[11px] text-slate-500">{dateTime(ticket.updated_at)}</td>
                    <td>
                      <Link
                        to={`/tickets/${ticket.ticket_id}`}
                        className="inline-flex items-center gap-0.5 text-[11px] text-slate-400 group-hover:text-indigo-600"
                      >
                        查看
                        <IconArrowRight width={12} height={12} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul className="divide-y divide-slate-100 md:hidden">
            {items.map((ticket) => (
              <li key={ticket.ticket_id}>
                <Link to={`/tickets/${ticket.ticket_id}`} className="block px-3 py-2.5">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="font-mono text-[11px] text-slate-500">{ticket.ticket_no}</span>
                    <StateBadge state={ticket.state} />
                    <LevelBadge level={ticket.write_level} />
                    <span className="ml-auto font-mono text-[12px] font-medium text-slate-900 num">{amount(ticket.amount)}</span>
                  </div>
                  <div className="mt-1 truncate text-[12.5px] font-medium text-slate-900">{ticket.business_key}</div>
                  <div className="mt-0.5 flex flex-wrap gap-x-2 gap-y-0.5 text-[10.5px] text-slate-400">
                    <span>{ticket.scene_code}</span>
                    <span>{ticket.systems.join(' / ')}</span>
                    <span>{dateTime(ticket.updated_at)}</span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-[11px] text-slate-400">
          {items.length > 0 ? `本页 ${items.length} 条` : ''}
          {query.data?.next_cursor ? ' · 还有下一页' : ''}
        </span>
        <div className="flex gap-2">
          <button type="button" className="btn btn-secondary btn-sm" disabled={!cursor} onClick={() => setCursor(undefined)}>
            回到第一页
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!query.data?.next_cursor}
            onClick={() => setCursor(query.data?.next_cursor ?? undefined)}
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  );
}
