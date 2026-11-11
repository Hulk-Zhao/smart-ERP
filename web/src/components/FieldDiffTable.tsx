import type { FieldDiffEntry } from '../api/types';
import { delta as computeDelta, text } from '../lib/format';
import { FieldDiffKindBadge } from './Badges';

export interface FieldDiffRow {
  key: string;
  field: string;
  ours: unknown;
  theirs: unknown;
  delta?: string;
  reason: string;
  reasonLabel?: string;
  unit?: string;
}

interface Props {
  rows: FieldDiffRow[];
  mineLabel?: string;
  theirsLabel?: string;
  deltaLabel?: string;
  reasonLabel?: string;
}

/**
 * 字段级 diff：窄屏按「字段 + 四列」纵向堆叠，宽屏五列；
 * 不使用 min-width 表格，避免 390px 下撑破容器。
 */
export function FieldDiffTable({
  rows,
  mineLabel = '我方值',
  theirsLabel = '对方值',
  deltaLabel = '差异',
  reasonLabel = '原因码',
}: Props) {
  if (rows.length === 0) {
    return <p className="px-3 py-3 text-[12px] text-slate-500">没有可展示的字段差异。</p>;
  }

  return (
    <div className="min-w-0">
      <div className="hidden gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2 text-[11px] font-medium text-slate-500 sm:grid sm:grid-cols-5">
        <span>字段</span>
        <span>{mineLabel}</span>
        <span>{theirsLabel}</span>
        <span>{deltaLabel}</span>
        <span>{reasonLabel}</span>
      </div>
      <ul className="divide-y divide-slate-100">
        {rows.map((row) => {
          const oursText = text(row.ours);
          const theirsText = text(row.theirs);
          const changed = oursText !== theirsText;
          const deltaTone =
            row.delta && row.delta.startsWith('+')
              ? 'text-emerald-700'
              : row.delta && row.delta.startsWith('-')
                ? 'text-rose-700'
                : 'text-slate-600';
          return (
            <li key={row.key} className="grid min-w-0 gap-2 px-3 py-2 text-[12px] sm:grid-cols-5 sm:items-start">
              <div className="min-w-0">
                <div className="font-medium text-slate-800">{row.field}</div>
                {row.unit ? <div className="text-[10px] text-slate-400">单位：{row.unit}</div> : null}
              </div>
              <div className="min-w-0 break-words">
                <span className="label mr-1 sm:hidden">{mineLabel}</span>
                <span
                  className={`inline-block max-w-full break-words rounded px-1 font-mono ${
                    changed ? 'diff-cell-ours text-rose-800' : 'text-slate-700'
                  }`}
                >
                  {oursText}
                </span>
              </div>
              <div className="min-w-0 break-words">
                <span className="label mr-1 sm:hidden">{theirsLabel}</span>
                <span
                  className={`inline-block max-w-full break-words rounded px-1 font-mono ${
                    changed ? 'diff-cell-theirs text-emerald-800' : 'text-slate-700'
                  }`}
                >
                  {theirsText}
                </span>
              </div>
              <div className="min-w-0 break-words">
                <span className="label mr-1 sm:hidden">{deltaLabel}</span>
                <span className={`rounded px-1 font-mono ${deltaTone} ${row.delta && row.delta !== '—' ? 'diff-cell-changed' : ''}`}>
                  {row.delta ?? '—'}
                </span>
              </div>
              <div className="min-w-0">
                <span className="label mr-1 sm:hidden">{reasonLabel}</span>
                <span className="inline-flex flex-wrap items-center gap-1">
                  <FieldDiffKindBadge kind={row.reason} />
                  {row.reasonLabel ? <span className="text-[11px] text-slate-500">{row.reasonLabel}</span> : null}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function asScalar(value: unknown): string | number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number' || typeof value === 'string') return value;
  return null;
}

/** 审计影像的 field_diff 是宽松对象；这里把 before/after 映射成前像/后像。 */
export function AuditFieldDiff({ entries }: { entries: FieldDiffEntry[] }) {
  const rows: FieldDiffRow[] = entries.map((entry, index) => {
    const field = String(entry.field ?? entry.key ?? `字段 ${index + 1}`);
    const label = entry.label ? String(entry.label) : '';
    const ours = entry.before;
    const theirs = entry.after;
    const scalarDelta = computeDelta(asScalar(ours), asScalar(theirs));
    return {
      key: `${field}-${index}`,
      field: label ? `${label}（${field}）` : field,
      ours,
      theirs,
      delta: scalarDelta,
      reason: String(entry.kind ?? entry.reason ?? 'field'),
      unit: entry.unit ? String(entry.unit) : undefined,
    };
  });

  return <FieldDiffTable rows={rows} mineLabel="前像" theirsLabel="后像" deltaLabel="变化" reasonLabel="字段类型" />;
}
