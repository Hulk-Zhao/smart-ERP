import type { ReactNode } from 'react';

export type EmptyVariant = 'no-data' | 'not-run' | 'no-diff';

const COPY: Record<EmptyVariant, { title: string; description: string }> = {
  'no-data': {
    title: '本期确实无数据',
    description: '当前 scope 与筛选条件下没有记录。',
  },
  'not-run': {
    title: '还没跑过对账',
    description: '发起对账后，批次会出现在这里。',
  },
  'no-diff': {
    title: '当前筛选下没有差异行',
    description: '换一个差异类型或取消筛选后再看。',
  },
};

export interface EmptyStateProps {
  variant?: EmptyVariant;
  title?: string;
  description?: string;
  actions?: ReactNode;
  compact?: boolean;
}

/**
 * 统一空态：明确区分「本期确实无数据」与「还没跑过对账」，
 * 避免各页手写一句「暂无数据」让用户无法判断下一步。
 */
export function EmptyState({
  variant = 'no-data',
  title,
  description,
  actions,
  compact = false,
}: EmptyStateProps) {
  const copy = COPY[variant];
  return (
    <div
      data-testid="empty-state"
      data-variant={variant}
      className={`min-w-0 max-w-full rounded-xl border border-dashed border-slate-200 bg-slate-50/60 text-center ${
        compact ? 'px-3 py-4' : 'px-4 py-8'
      }`}
    >
      <p className="break-words text-[13px] font-medium text-slate-700">{title ?? copy.title}</p>
      <p className="mt-1 break-words text-[12px] leading-5 text-slate-500">{description ?? copy.description}</p>
      {actions ? <div className="mt-3 flex flex-wrap items-center justify-center gap-2">{actions}</div> : null}
    </div>
  );
}
