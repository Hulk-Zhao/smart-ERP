import { diffTypeMeta, ROLE_LABELS, STEP_STATE_LABELS, WRITE_LEVEL_LABELS } from '../api/types';

const STATE_TONES: Record<string, string> = {
  计划中: 'bg-slate-100 text-slate-600 ring-slate-200',
  待确认: 'bg-sky-50 text-sky-700 ring-sky-200',
  待审批: 'bg-amber-50 text-amber-700 ring-amber-200',
  执行中: 'bg-blue-50 text-blue-700 ring-blue-200',
  校验中: 'bg-indigo-50 text-indigo-700 ring-indigo-200',
  已完成: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  补偿中: 'bg-orange-50 text-orange-700 ring-orange-200',
  已回滚: 'bg-rose-50 text-rose-700 ring-rose-200',
  需人工处置: 'bg-fuchsia-50 text-fuchsia-700 ring-fuchsia-200',
  已锁定: 'bg-slate-800 text-white ring-slate-800',
  已终止: 'bg-slate-100 text-slate-500 ring-slate-200',
  已驳回: 'bg-rose-100 text-rose-700 ring-rose-200',
};

export function StateBadge({ state }: { state: string }) {
  return <span className={`badge ${STATE_TONES[state] ?? 'bg-slate-100 text-slate-600 ring-slate-200'}`}>{state}</span>;
}

export function LevelBadge({ level }: { level: string }) {
  const tone =
    level === 'W3'
      ? 'bg-rose-600 text-white ring-rose-600'
      : level === 'W2'
        ? 'bg-amber-100 text-amber-800 ring-amber-200'
        : 'bg-slate-100 text-slate-600 ring-slate-200';
  return (
    <span className={`badge ${tone}`} title={WRITE_LEVEL_LABELS[level] ?? level}>
      {level}
    </span>
  );
}

export function DiffTypeBadge({ type }: { type: string }) {
  const meta = diffTypeMeta(type);
  return <span className={`badge ${meta.tone}`} title={type}>{meta.label}</span>;
}

export function RoleBadge({ role }: { role: string }) {
  return <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{ROLE_LABELS[role] ?? role}</span>;
}

export function StepStateBadge({ state }: { state: string }) {
  const label = STEP_STATE_LABELS[state] ?? state;
  const tone =
    state === 'succeeded' || state === 'compensated' || state === 'rolled_back'
      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
      : state === 'failed' || state === 'unresolved'
        ? 'bg-rose-50 text-rose-700 ring-rose-200'
        : state === 'compensating'
          ? 'bg-orange-50 text-orange-700 ring-orange-200'
          : 'bg-slate-100 text-slate-600 ring-slate-200';
  return <span className={`badge ${tone}`}>{label}</span>;
}

export function FieldDiffKindBadge({ kind }: { kind: string }) {
  return <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{kind}</span>;
}
