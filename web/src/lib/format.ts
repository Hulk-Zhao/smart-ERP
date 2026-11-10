import type { MoneyOut } from '../api/types';

const AMOUNT_FORMAT = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

export function amount(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return AMOUNT_FORMAT.format(parsed);
}

export function money(value: MoneyOut | string | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') return `${amount(value)} CNY`;
  return `${amount(value.amount)} ${value.currency || 'CNY'}`;
}

export function delta(mine: string | number | null | undefined, theirs: string | number | null | undefined): string {
  if (mine === null || mine === undefined || theirs === null || theirs === undefined) return '—';
  const left = Number(mine);
  const right = Number(theirs);
  if (!Number.isFinite(left) || !Number.isFinite(right)) return '—';
  const diff = left - right;
  const sign = diff > 0 ? '+' : '';
  return `${sign}${amount(diff)}`;
}

export function rate(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—';
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  return `${(parsed * 100).toFixed(2)}%`;
}

export function dateTime(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

export function shortHash(value: string | null | undefined, keep = 14): string {
  if (!value) return '—';
  if (value.length <= keep * 2) return value;
  return `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

export function shortId(value: string | null | undefined): string {
  if (!value) return '—';
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

export function text(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export function signedAmountClass(value: string | number | null | undefined): string {
  const parsed = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(parsed) || parsed === 0) return 'text-slate-700';
  return parsed > 0 ? 'text-emerald-700' : 'text-rose-700';
}
