import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { TICKET_STATES } from '../api/types';
import { LevelBadge, StateBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
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
        limit: 20,
      }),
  });

  const items = query.data?.items ?? [];

  const applyState = (next: string) => {
    setState(next);
    setCursor(undefined);
  };

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div className="min-w-0">
          <h1 className="text-[18px] font-semibold text-slate-900">工单</h1>
          <p className="mt-1 text-[12px] text-slate-500">真实 /v1/tickets，按 G-05 scope 过滤；点击进入 12 状态时间线与审计。</p>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => void query.refetch()}>
          刷新
        </button>
      </div>

      <div className="card min-w-0">
        <div className="card-body space-y-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <input
              className="input min-w-0 flex-1 sm:max-w-xs"
              placeholder="按工单号精确查询"
              value={ticketNo}
              onChange={(event) => {
                setTicketNo(event.target.value);
                setCursor(undefined);
              }}
            />
            <span className="text-[11px] text-slate-400">筛选状态</span>
            {(state || ticketNo ? (
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
            ) : null)}
          </div>

          <div className="flex min-w-0 flex-wrap gap-1.5">
            <button type="button" className={`chip ${state === '' ? 'chip-active' : ''}`} onClick={() => applyState('')}>
              全部
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
      </div>

      {query.error ? (
        <ErrorPanel error={query.error} onRetry={() => void query.refetch()} onOpenSettings={openSettingsPanel} />
      ) : null}

      {query.isLoading ? (
        <div className="space-y-2">
          {[0, 1, 2, 3, 4].map((key) => <div key={key} className="skeleton h-20 w-full" />)}
        </div>
      ) : items.length === 0 ? (
        <EmptyState
          variant="no-data"
          description="本期在 org / period / systems 范围内没有工单；可清空状态筛选，或确认 scope 是否收得太窄。"
        />
      ) : (
        <ul className="space-y-2">
          {items.map((ticket) => (
            <li key={ticket.ticket_id} className="card min-w-0">
              <Link to={`/tickets/${ticket.ticket_id}`} className="block min-w-0 rounded-xl p-3 hover:bg-slate-50">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="font-mono text-[12px] text-slate-500">{ticket.ticket_no}</span>
                  <StateBadge state={ticket.state} />
                  <LevelBadge level={ticket.write_level} />
                  {ticket.touches_fund ? (
                    <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉及资金</span>
                  ) : null}
                  <span className="ml-auto font-mono text-[13px] font-medium text-slate-900">{amount(ticket.amount)}</span>
                </div>

                <div className="mt-1.5 min-w-0 break-words text-[13px] font-medium text-slate-800">{ticket.business_key}</div>

                <div className="mt-1 grid grid-cols-1 gap-x-4 gap-y-1 text-[11px] text-slate-500 sm:grid-cols-2 lg:grid-cols-4">
                  <span className="min-w-0 truncate">场景：{ticket.scene_code}</span>
                  <span className="min-w-0 truncate">系统：{ticket.systems.join(' / ')}</span>
                  <span className="min-w-0 truncate">发起人：{shortId(ticket.initiator_id)}</span>
                  <span className="min-w-0 truncate">更新：{dateTime(ticket.updated_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
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
