import { TERMINAL_STATES, TICKET_STATES, TRANSITIONS, type TicketState } from '../api/types';

export function StateRail({ state }: { state: string }) {
  const known = TICKET_STATES.includes(state as TicketState);
  const next = known ? TRANSITIONS[state as TicketState] : [];
  const terminal = TERMINAL_STATES.includes(state as TicketState);

  return (
    <div className="min-w-0">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="section-title">12 状态机</h3>
        <span className="text-[11px] text-slate-500">
          {known ? `当前：${state}${terminal ? '（终态）' : ''}` : `当前状态不在 12 状态机内：${state}`}
        </span>
      </div>

      <ol className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6" aria-label="工单状态机">
        {TICKET_STATES.map((item, index) => {
          const active = item === state;
          const isNext = next.includes(item);
          return (
            <li
              key={item}
              aria-current={active ? 'step' : undefined}
              className={`min-w-0 rounded-lg border px-2.5 py-2 text-[12px] ${
                active
                  ? 'border-slate-900 bg-slate-900 text-white'
                  : isNext
                    ? 'border-sky-300 bg-sky-50 text-sky-800'
                    : 'border-slate-200 bg-white text-slate-500'
              }`}
            >
              <div className="flex items-center gap-1.5">
                <span className={`font-mono text-[10px] ${active ? 'text-slate-300' : 'text-slate-400'}`}>
                  {String(index + 1).padStart(2, '0')}
                </span>
                <span className="truncate font-medium">{item}</span>
              </div>
              {isNext ? <div className="mt-0.5 text-[10px] text-sky-700">下一步可达</div> : null}
              {active && !terminal ? <div className="mt-0.5 text-[10px] text-slate-300">当前位置</div> : null}
              {active && terminal ? <div className="mt-0.5 text-[10px] text-slate-300">终态，不可再迁移</div> : null}
            </li>
          );
        })}
      </ol>

      {next.length > 0 ? (
        <p className="mt-2 break-words text-[11px] text-slate-500">
          允许的下一状态：{next.join('、')}（以服务端状态机校验为准）
        </p>
      ) : null}
    </div>
  );
}
