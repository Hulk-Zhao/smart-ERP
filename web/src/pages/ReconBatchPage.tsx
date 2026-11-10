import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { DIFF_TYPES } from '../api/types';
import type { DiffTypeCode } from '../api/types';
import { DiffTypeBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { amount, dateTime, money, rate } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

function Metric({ label, value, hint, tone = '' }: { label: string; value: string | number; hint?: string; tone?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 px-2.5 py-2">
      <div className="label">{label}</div>
      <div className={`mt-0.5 truncate font-mono text-[15px] ${tone || 'text-slate-800'}`}>{value}</div>
      {hint ? <div className="mt-0.5 text-[10px] text-slate-400">{hint}</div> : null}
    </div>
  );
}

export function ReconBatchPage() {
  const { batchId = '' } = useParams();
  const [diffType, setDiffType] = useState<DiffTypeCode | ''>('');
  const [needsManual, setNeedsManual] = useState(false);
  const [supplier, setSupplier] = useState('');
  const [cursor, setCursor] = useState<string | undefined>(undefined);

  const batch = useQuery({
    queryKey: ['recon', batchId],
    queryFn: () => api.getReconRun(batchId),
    enabled: Boolean(batchId),
  });

  const diffs = useQuery({
    queryKey: ['recon', batchId, 'diffs', diffType, needsManual, supplier, cursor ?? 'first'],
    queryFn: () =>
      api.listReconDiffs(batchId, {
        // diffType 来自 DIFF_TYPES.code，已是契约 DiffTypeCode，无需断言。
        diff_type: diffType || undefined,
        needs_manual: needsManual ? true : undefined,
        supplier_code: supplier.trim() || undefined,
        cursor: cursor ?? undefined,
        limit: 20,
      }),
    enabled: Boolean(batchId),
  });

  if (!batchId) {
    return <ErrorPanel error={{ status: 422, code: 'E-DATA-MISSING', detail: '缺少 batch_id' }} />;
  }

  const metrics = batch.data?.metrics;
  const diffItems = diffs.data?.items ?? [];

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link to="/recon" className="link text-[12px]">← 返回对账</Link>
          <h1 className="mt-1 break-all text-[16px] font-semibold text-slate-900">批次 {batchId}</h1>
          {batch.data ? (
            <p className="mt-1 break-words text-[12px] text-slate-500">
              {batch.data.status} · 供应商 {batch.data.suppliers.join('、') || '—'} · 创建 {dateTime(batch.data.created_at)}
            </p>
          ) : null}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Link to={`/recon/${batchId}/metering`} className="btn btn-secondary btn-sm">
            计量表 →
          </Link>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => { void batch.refetch(); void diffs.refetch(); }}
          >
            刷新
          </button>
        </div>
      </div>

      {batch.error ? (
        <ErrorPanel
          error={batch.error}
          onRetry={() => void batch.refetch()}
          onOpenSettings={openSettingsPanel}
          notFoundKind="batch"
          backTo={{ to: '/recon', label: '← 返回对账' }}
        />
      ) : null}

      {batch.isLoading ? (
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((key) => <div key={key} className="skeleton h-16 w-full" />)}
        </div>
      ) : batch.data ? (
        <>
          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">结论</h2>
              <span className={`badge ${batch.data.conclusive ? 'bg-emerald-50 text-emerald-700 ring-emerald-200' : 'bg-amber-50 text-amber-700 ring-amber-200'}`}>
                {batch.data.conclusive ? '可下结论' : '不可下结论'}
              </span>
            </div>
            <div className="card-body space-y-2">
              <p className="break-words text-[12px] text-slate-700">{batch.data.status_reason}</p>
              <div className="flex flex-wrap gap-1">
                {metrics?.suspected_anomaly ? <span className="badge bg-rose-50 text-rose-700 ring-rose-200">疑似异常</span> : null}
                <span className={`badge ${metrics?.auto_disposal_allowed ? 'bg-emerald-50 text-emerald-700 ring-emerald-200' : 'bg-amber-50 text-amber-700 ring-amber-200'}`}>
                  {metrics?.auto_disposal_allowed ? '允许自动处置' : '禁止自动处置'}
                </span>
                <span className="badge bg-slate-100 text-slate-600 ring-slate-200">差异 {batch.data.diff_count ?? diffItems.length} 行</span>
              </div>

              {(batch.data.missing_items ?? []).length > 0 ? (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
                  <div className="text-[12px] font-medium text-amber-900">缺失项（不猜测填充）</div>
                  <ul className="mt-1 space-y-1 text-[11px] text-amber-800">
                    {(batch.data.missing_items ?? []).map((item) => (
                      <li key={`${item.kind}-${item.code}`} className="break-words">
                        [{item.kind}] {item.code}：{item.detail}（{item.source_ref}）
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">指标</h2>
              <span className="text-[11px] text-slate-400">来自批次返回，不是前端估算</span>
            </div>
            <div className="card-body grid grid-cols-2 gap-2 lg:grid-cols-4">
              <Metric label="供应商行" value={metrics?.supplier_lines ?? '—'} />
              <Metric label="匹配行" value={metrics?.matched ?? '—'} hint={`匹配率 ${rate(metrics?.matched_rate)}`} tone="text-emerald-700" />
              <Metric label="差异行" value={metrics?.diff_lines ?? '—'} hint={`差异率 ${rate(metrics?.diff_rate)}`} tone="text-amber-700" />
              <Metric label="差异金额" value={amount(metrics?.diff_amount)} tone="text-rose-700" />
              <Metric label="模糊/多候选" value={metrics?.ambiguous ?? '—'} />
              <Metric label="仅候选" value={metrics?.candidate_only ?? '—'} />
              <Metric label="未匹配" value={metrics?.unmatched ?? '—'} />
              <Metric label="自动处置" value={metrics?.auto_disposal_allowed ? '允许' : '禁止'} />
            </div>
          </section>
        </>
      ) : null}

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">差异列表</h2>
          <span className="text-[11px] text-slate-400">点击行查看字段级 diff 与证据坐标</span>
        </div>

        <div className="card-body space-y-2">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <button type="button" className={`chip ${diffType === '' ? 'chip-active' : ''}`} onClick={() => { setDiffType(''); setCursor(undefined); }}>
              全部类型
            </button>
            {DIFF_TYPES.map((item) => (
              <button
                key={item.code}
                type="button"
                className={`chip ${diffType === item.code ? 'chip-active' : ''}`}
                onClick={() => { setDiffType(item.code); setCursor(undefined); }}
                title={item.code}
              >
                {item.label}
              </button>
            ))}
          </div>

          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <label className="chip cursor-pointer">
              <input type="checkbox" checked={needsManual} onChange={(event) => { setNeedsManual(event.target.checked); setCursor(undefined); }} />
              只看需人工
            </label>
            <input
              className="input min-w-0 flex-1 sm:max-w-[220px]"
              placeholder="供应商编码"
              value={supplier}
              onChange={(event) => { setSupplier(event.target.value); setCursor(undefined); }}
            />
          </div>
        </div>

        {diffs.error ? (
          <div className="px-3 pb-3">
            <ErrorPanel error={diffs.error} onRetry={() => void diffs.refetch()} onOpenSettings={openSettingsPanel} />
          </div>
        ) : null}

        {diffs.isLoading ? (
          <div className="space-y-2 px-3 pb-3">{[0, 1, 2].map((key) => <div key={key} className="skeleton h-14 w-full" />)}</div>
        ) : diffItems.length === 0 ? (
          <div className="p-3">
            <EmptyState variant="no-diff" compact />
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {diffItems.map((diff) => (
              <li key={diff.diff_id} className="min-w-0">
                <Link to={`/recon/${batchId}/diffs/${encodeURIComponent(diff.diff_id)}`} className="block min-w-0 px-3 py-2.5 hover:bg-slate-50">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <DiffTypeBadge type={diff.type} />
                    {diff.needs_manual ? <span className="badge bg-amber-50 text-amber-700 ring-amber-200">需人工</span> : null}
                    <span className="font-mono text-[11px] text-slate-500">{diff.supplier_code}</span>
                    <span className="ml-auto font-mono text-[12px] text-rose-700">{money(diff.amount)}</span>
                  </div>
                  <div className="mt-1 break-words text-[12px] text-slate-700">{diff.suggestion}</div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-slate-400">
                    <span className="break-all">{diff.ref}</span>
                    <span>责任方 {diff.owner}</span>
                    <span>置信度 {rate(diff.confidence)}</span>
                    <span>证据 {diff.evidence?.length ?? 0} 条</span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
          <span className="text-[11px] text-slate-400">本页 {diffItems.length} 条</span>
          <div className="flex gap-2">
            <button type="button" className="btn btn-secondary btn-sm" disabled={!cursor} onClick={() => setCursor(undefined)}>
              回到第一页
            </button>
            <button type="button" className="btn btn-primary btn-sm" disabled={!diffs.data?.next_cursor} onClick={() => setCursor(diffs.data?.next_cursor ?? undefined)}>
              下一页
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
