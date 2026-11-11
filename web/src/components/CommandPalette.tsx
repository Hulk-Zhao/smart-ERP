import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { IconArrowRight, IconCheck, IconHome, IconScale, IconSearch, IconSettings, IconTicket } from './icons';

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onOpenSettings: () => void;
}

interface PaletteItem {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: 'home' | 'ticket' | 'approval' | 'recon' | 'settings';
  run: () => void;
}

/**
 * ⌘K 命令面板：页面跳转 + 工单号搜索 + 少量全局动作。
 * 工单结果来自与列表页同一个 `api.listTickets`，不另造数据源。
 */
export function CommandPalette({ open, onClose, onOpenSettings }: CommandPaletteProps) {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const tickets = useQuery({
    queryKey: ['command-palette', 'tickets'],
    queryFn: () => api.listTickets({ limit: 20 }),
    enabled: open,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!open) {
      setQuery('');
      setActive(0);
      return;
    }
    const timer = window.setTimeout(() => inputRef.current?.focus(), 20);
    return () => window.clearTimeout(timer);
  }, [open]);

  const items = useMemo<PaletteItem[]>(() => {
    const go = (to: string) => () => {
      navigate(to);
      onClose();
    };
    const base: PaletteItem[] = [
      { id: 'nav-home', group: '页面', label: '工作台', hint: '今日待你决策', icon: 'home', run: go('/') },
      { id: 'nav-tickets', group: '页面', label: '工单', hint: '全部工单与状态', icon: 'ticket', run: go('/tickets') },
      { id: 'nav-approvals', group: '页面', label: '审批', hint: '待办队列与双人复核', icon: 'approval', run: go('/approvals') },
      { id: 'nav-recon', group: '页面', label: '对账', hint: '发起对账与差异处理', icon: 'recon', run: go('/recon') },
      {
        id: 'action-settings',
        group: '动作',
        label: '打开连接设置',
        hint: 'API 地址 / 令牌 / scope / 角色',
        icon: 'settings',
        run: () => {
          onClose();
          onOpenSettings();
        },
      },
    ];
    const ticketItems: PaletteItem[] = (tickets.data?.items ?? []).map((ticket) => ({
      id: `ticket-${ticket.ticket_id}`,
      group: '工单',
      label: `${ticket.ticket_no} · ${ticket.business_key}`,
      hint: `${ticket.state} · ${ticket.write_level} · ${ticket.amount}`,
      icon: 'ticket',
      run: go(`/tickets/${encodeURIComponent(ticket.ticket_id)}`),
    }));
    const all = [...base, ...ticketItems];
    const needle = query.trim().toLowerCase();
    if (!needle) return all;
    return all.filter((item) =>
      `${item.label} ${item.hint ?? ''} ${item.group}`.toLowerCase().includes(needle),
    );
  }, [navigate, onClose, onOpenSettings, query, tickets.data]);

  useEffect(() => {
    setActive((current) => (current >= items.length ? 0 : current));
  }, [items.length]);

  if (!open) return null;

  const runActive = (index: number) => {
    const item = items[index];
    if (item) item.run();
  };

  const groups = items.reduce<Record<string, PaletteItem[]>>((acc, item) => {
    (acc[item.group] ??= []).push(item);
    return acc;
  }, {});

  let flatIndex = -1;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-slate-900/35 px-3 pt-[12vh] backdrop-blur-[2px]"
      role="dialog"
      aria-modal="true"
      aria-label="命令面板"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="w-full max-w-[620px] overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        <div className="flex items-center gap-2 border-b border-slate-200 px-3">
          <IconSearch className="shrink-0 text-slate-400" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                onClose();
              } else if (event.key === 'ArrowDown') {
                event.preventDefault();
                setActive((current) => (items.length ? (current + 1) % items.length : 0));
              } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setActive((current) => (items.length ? (current - 1 + items.length) % items.length : 0));
              } else if (event.key === 'Enter') {
                event.preventDefault();
                runActive(active);
              }
            }}
            placeholder="搜索工单号、业务键或页面…"
            className="h-12 w-full bg-transparent text-[14px] outline-none placeholder:text-slate-400"
            aria-label="搜索命令"
          />
          <kbd className="kbd mr-1 hidden sm:inline-flex">ESC</kbd>
        </div>

        <div className="max-h-[56vh] overflow-y-auto py-1.5">
          {items.length === 0 ? (
            <div className="px-4 py-8 text-center text-[13px] text-slate-500">
              没有匹配项。试试工单号 <span className="font-mono text-slate-700">T-1001</span> 或页面名「审批」。
            </div>
          ) : (
            Object.entries(groups).map(([group, groupItems]) => (
              <div key={group} className="px-1.5 py-1">
                <div className="px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.08em] text-slate-400">
                  {group}
                </div>
                {groupItems.map((item) => {
                  flatIndex += 1;
                  const index = flatIndex;
                  const selected = index === active;
                  const Icon = {
                    home: IconHome,
                    ticket: IconTicket,
                    approval: IconCheck,
                    recon: IconScale,
                    settings: IconSettings,
                  }[item.icon];
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onMouseEnter={() => setActive(index)}
                      onClick={() => runActive(index)}
                      className={`flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left ${
                        selected ? 'bg-indigo-50 text-indigo-900' : 'text-slate-700 hover:bg-slate-50'
                      }`}
                    >
                      <span
                        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md ${
                          selected ? 'bg-white text-indigo-600 ring-1 ring-indigo-200' : 'bg-slate-100 text-slate-500'
                        }`}
                      >
                        <Icon />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[13px] font-medium">{item.label}</span>
                        {item.hint ? <span className="block truncate text-[11px] text-slate-500">{item.hint}</span> : null}
                      </span>
                      {selected ? <IconArrowRight className="shrink-0 text-indigo-500" /> : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        <div className="flex items-center justify-between border-t border-slate-200 bg-slate-50 px-3 py-2 text-[10.5px] text-slate-500">
          <span className="flex items-center gap-2">
            <kbd className="kbd">↑</kbd>
            <kbd className="kbd">↓</kbd> 选择
            <kbd className="kbd">↵</kbd> 打开
          </span>
          <span>{tickets.isFetching ? '正在检索工单…' : `共 ${items.length} 项`}</span>
        </div>
      </div>
    </div>
  );
}
