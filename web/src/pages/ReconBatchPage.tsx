import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { useSession } from '../api/session';
import { DIFF_TYPES } from '../api/types';
import type { DiffTypeCode } from '../api/types';
import { DiffTypeBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { IconArrowRight } from '../components/icons';
import { dateTime, money, rate, shortId } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

function Metric({
  label,
  value,
  hint,
  tone = '',
  testId,
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: string;
  testId?: string;
}) {
  return (
    <div className="min-w-0 px-3 py-2">
      <div className="label">{label}</div>
      {/* 金额不能 truncate：7,108,301.10 CNY 被裁成 7,108,301. 比数字错还难解释。 */}
      <div
        className={`mt-0.5 break-words font-mono text-[15px] font-medium leading-5 num ${tone || 'text-slate-800'}`}
        data-testid={testId}
      >
        {value}
      </div>
      {hint ? <div className="mt-0.5 truncate text-[10px] text-slate-400" title={hint}>{hint}</div> : null}
    </div>
  );
}

const PHASES = ['取数', '匹配', '差异', '计量'];

function phaseIndex(status: string): number {
  if (status.includes('取数') || status.includes('pending') || status.includes('排队')) return 0;
  if (status.includes('匹配')) return 1;
  if (status.includes('差异')) return 2;
  if (status.includes('完成') || status.includes('done') || status.includes('成功')) return 4;
  return 3;
}

export function ReconBatchPage() {
  const { batchId = '' } = useParams();
  const session = useSession();
  const [diffType, setDiffType] = useState<DiffTypeCode | ''>('');
  const [needsManual, setNeedsManual] = useState(false);
  const [supplier, setSupplier] = useState('');
  const [cursor, setCursor] = useState<string | undefined>(undefined);

  const batch = useQuery({
    queryKey: ['recon', batchId],
    queryFn: () => api.getReconRun(batchId),
    enabled: session.canQuery && Boolean(batchId),
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
    enabled: session.canQuery && Boolean(batchId),
  });

  if (!batchId) {
    return <ErrorPanel error={{ status: 422, code: 'E-DATA-MISSING', detail: '缺少 batch_id' }} />;
  }

  const metrics = batch.data?.metrics;
  const diffItems = diffs.data?.items ?? [];
  // 页大小 20 是服务端分页上限；页脚必须同时给出总量，否则现场会以为差异只有一屏。
  const totalDiffCount = batch.data?.diff_count ?? metrics?.diff_lines ?? null;
  const currentPhase = batch.data ? phaseIndex(batch.data.status) : 0;

  return (
    <div className="min-w-0 space-y-4">
      <section className="card min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <Link to="/recon" className="link text-[11.5px]">
              ← 返回对账
            </Link>
            {/* 标题必须是「批次」本身：供应商串不能当标题，更不能和批次 ID 粘成一行。 */}
            <h1
              className="mt-1 flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[17px] font-semibold tracking-tight text-slate-900"
              data-testid="batch-title"
            >
              <span>对账批次</span>
              <span className="font-mono text-[11.5px] font-normal text-slate-500" title={batchId}>
                {shortId(batchId)}
              </span>
            </h1>
            {batch.data ? (
              <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] text-slate-500">
                <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{batch.data.status}</span>
                <span className="break-all">供应商 {batch.data.suppliers.join('、') || '—'}</span>
                <span className="text-slate-300">·</span>
                <span>创建 {dateTime(batch.data.created_at)}</span>
              </p>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Link to={`/recon/${batchId}/metering`} className="btn btn-primary btn-sm">
              计量表 →
            </Link>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                void batch.refetch();
                void diffs.refetch();
              }}
            >
              刷新
            </button>
          </div>
        </div>

        <div className="border-t border-slate-100 px-4 py-2.5">
          <div className="flex items-center justify-between text-[10.5px] text-slate-400">
            <span>批次阶段</span>
            <span>{batch.data?.status ?? '读取中…'}</span>
          </div>
          <div className="mt-1.5 flex items-center gap-1.5">
            {PHASES.map((phase, index) => (
              <div key={phase} className="flex min-w-0 flex-1 items-center gap-1.5">
                <span
                  className={`status-dot ${
                    index < currentPhase ? 'bg-emerald-500' : index === currentPhase ? 'bg-indigo-500' : 'bg-slate-200'
                  }`}
                />
                <span
                  className={`truncate text-[10.5px] ${
                    index <= currentPhase ? 'text-slate-700' : 'text-slate-400'
                  }`}
                >
                  {phase}
                </span>
                {index < PHASES.length - 1 ? <span className="h-px flex-1 bg-slate-200" /> : null}
              </div>
            ))}
          </div>
        </div>
      </section>

      {batch.error ? (
        <ErrorPanel
          error={batch.error}
          onRetry={() => void batch.refetch()}
          onOpenSettings={openSettingsPanel}
          notFoundKind="batch"
          backTo={{ to: '/recon', label: '← 返回对账' }}
        />
      ) : null}

      {batch.isPending ? (
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((key) => <div key={key} className="skeleton h-16 w-full" />)}
        </div>
      ) : batch.data ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="min-w-0 space-y-4 xl:order-1">
            <section className="card min-w-0 overflow-hidden">
              <div className="card-head">
                <h2 className="section-title">指标</h2>
                <span className="text-[10.5px] text-slate-400">来自批次返回，不是前端估算</span>
              </div>
              <div className="grid grid-cols-2 divide-x divide-y divide-slate-100 border-t border-slate-100 lg:grid-cols-4 lg:divide-y-0 lg:border-t-0">
                <Metric label="供应商行" value={metrics?.supplier_lines ?? '—'} />
                <Metric label="匹配行" value={metrics?.matched ?? '—'} hint={`匹配率 ${rate(metrics?.matched_rate)}`} tone="text-emerald-700" />
                <Metric label="差异行" value={metrics?.diff_lines ?? '—'} hint={`差异率 ${rate(metrics?.diff_rate)}`} tone="text-amber-700" />
                <Metric
                  label="差异金额"
                  value={money(metrics?.diff_amount)}
                  tone="text-rose-700"
                  testId="metric-diff-amount"
                />
                <Metric label="模糊/多候选" value={metrics?.ambiguous ?? '—'} />
                <Metric label="仅候选" value={metrics?.candidate_only ?? '—'} />
                <Metric label="未匹配" value={metrics?.unmatched ?? '—'} />
                <Metric label="自动处置" value={metrics?.auto_disposal_allowed ? '允许' : '禁止'} tone={metrics?.auto_disposal_allowed ? 'text-emerald-700' : 'text-amber-700'} />
              </div>
              {metrics ? (
                <>
                  <p className="border-t border-slate-100 px-3 py-2 text-[11px] leading-5 text-slate-600" data-testid="matched-caliber">
                    {metrics.matched} 行匹配（行级），其中 {metrics.diff_lines} 行存在差异；差异行仍计入匹配，不会从匹配行里扣除。
                  </p>
                  <p className="px-3 pb-2 text-[10.5px] leading-4 text-slate-400" data-testid="metric-caliber">
                    模糊/多候选（ambiguous）、仅候选（candidate_only）、未匹配（unmatched，我方 + 供应商）是三个互斥的行级口径；
                    数值相同只是巧合，不代表同一批行被重复统计。
                  </p>
                </>
              ) : null}
            </section>

            <section className="card min-w-0 overflow-hidden">
              <div className="card-head">
                <div className="flex items-center gap-2">
                  <h2 className="section-title">差异列表</h2>
                  <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{batch.data.diff_count ?? diffItems.length}</span>
                </div>
                <span className="text-[10.5px] text-slate-400">
                  点击行查看字段级 diff 与证据坐标；置信度来自规则引擎打分，不是模型输出
                </span>
              </div>

              <div className="space-y-2 border-b border-slate-100 px-3 py-2.5">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <button
                    type="button"
                    className={`chip ${diffType === '' ? 'chip-active' : ''}`}
                    onClick={() => {
                      setDiffType('');
                      setCursor(undefined);
                    }}
                  >
                    全部类型
                  </button>
                  {DIFF_TYPES.map((item) => (
                    <button
                      key={item.code}
                      type="button"
                      className={`chip ${diffType === item.code ? 'chip-active' : ''}`}
                      onClick={() => {
                        setDiffType(item.code);
                        setCursor(undefined);
                      }}
                      title={item.code}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>

                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <label className={`chip cursor-pointer ${needsManual ? 'chip-active' : ''}`}>
                    <input
                      type="checkbox"
                      className="sr-only"
                      checked={needsManual}
                      onChange={(event) => {
                        setNeedsManual(event.target.checked);
                        setCursor(undefined);
                      }}
                    />
                    只看需人工
                  </label>
                  <input
                    className="input h-8 min-w-0 flex-1 py-1 text-[12px] sm:max-w-[220px]"
                    placeholder="供应商编码"
                    value={supplier}
                    onChange={(event) => {
                      setSupplier(event.target.value);
                      setCursor(undefined);
                    }}
                  />
                  <span className="ml-auto text-[10.5px] text-slate-400">
                    {diffs.isFetching ? '同步中…' : `本页 ${diffItems.length} 条`}
                  </span>
                </div>
              </div>

              {diffs.error ? (
                <div className="px-3 py-3">
                  <ErrorPanel error={diffs.error} onRetry={() => void diffs.refetch()} onOpenSettings={openSettingsPanel} />
                </div>
              ) : null}

              {diffs.isPending ? (
                <div className="space-y-2 p-3">{[0, 1, 2, 3, 4].map((key) => <div key={key} className="skeleton h-9 w-full" />)}</div>
              ) : diffItems.length === 0 ? (
                <div className="p-3">
                  <EmptyState
                    variant="no-diff"
                    compact
                    actions={
                      (diffType || needsManual || supplier) ? (
                        <button
                          type="button"
                          className="btn btn-secondary btn-sm"
                          onClick={() => {
                            setDiffType('');
                            setNeedsManual(false);
                            setSupplier('');
                            setCursor(undefined);
                          }}
                        >
                          全部类型
                        </button>
                      ) : undefined
                    }
                  />
                </div>
              ) : (
                <>
                  <div className="hidden overflow-x-auto md:block">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th className="w-[124px]">类型</th>
                          <th className="w-[112px]">供应商</th>
                          <th className="w-[112px] text-right">金额</th>
                          <th className="w-[84px]">责任方</th>
                          <th
                            className="w-[92px] text-right"
                            title="规则引擎打分（置信度），不是模型输出；来源见列说明"
                          >
                            置信度（规则）
                          </th>
                          <th className="w-[72px]">人工</th>
                          <th className="w-[72px] text-right">证据</th>
                          <th>建议摘要</th>
                          <th className="w-[52px]" />
                        </tr>
                      </thead>
                      <tbody>
                        {diffItems.map((diff) => (
                          <tr key={diff.diff_id} className="group">
                            <td>
                              <Link to={`/recon/${batchId}/diffs/${encodeURIComponent(diff.diff_id)}`}>
                                <DiffTypeBadge type={diff.type} />
                              </Link>
                            </td>
                            <td className="font-mono text-[11px] text-slate-500">{diff.supplier_code}</td>
                            <td className="num font-medium text-rose-700">{money(diff.amount)}</td>
                            <td className="text-[11.5px] text-slate-600">{diff.owner}</td>
                            <td className="num text-[11.5px] text-slate-600">{rate(diff.confidence)}</td>
                            <td>
                              {diff.needs_manual ? (
                                <span className="badge bg-amber-50 text-amber-700 ring-amber-200">需人工</span>
                              ) : (
                                <span className="badge bg-slate-100 text-slate-500 ring-slate-200">自动</span>
                              )}
                            </td>
                            <td className="num text-[11.5px] text-slate-600">{diff.evidence?.length ?? 0}</td>
                            <td className="max-w-[320px] truncate text-[11.5px] text-slate-600" title={diff.suggestion}>
                              {diff.suggestion}
                            </td>
                            <td>
                              <Link
                                to={`/recon/${batchId}/diffs/${encodeURIComponent(diff.diff_id)}`}
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
                    {diffItems.map((diff) => (
                      <li key={diff.diff_id}>
                        <Link to={`/recon/${batchId}/diffs/${encodeURIComponent(diff.diff_id)}`} className="block px-3 py-2.5">
                          <div className="flex min-w-0 items-center gap-1.5">
                            <DiffTypeBadge type={diff.type} />
                            {diff.needs_manual ? (
                              <span className="badge bg-amber-50 text-amber-700 ring-amber-200">需人工</span>
                            ) : null}
                            <span className="font-mono text-[11px] text-slate-500">{diff.supplier_code}</span>
                            <span className="ml-auto font-mono text-[12px] font-medium text-rose-700 num">{money(diff.amount)}</span>
                          </div>
                          <div className="mt-1 line-clamp-2 text-[12px] text-slate-700">{diff.suggestion}</div>
                          <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 text-[10px] text-slate-400">
                            <span>责任方 {diff.owner}</span>
                            <span>置信度 {rate(diff.confidence)}</span>
                            <span>证据 {diff.evidence?.length ?? 0} 条</span>
                          </div>
                        </Link>
                      </li>
                    ))}
                  </ul>
                </>
              )}

              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-3 py-2">
                <span className="text-[10.5px] text-slate-400">
                  本页 {diffItems.length} 条
                  {totalDiffCount !== null ? ` / 共 ${totalDiffCount} 条` : ''}
                  {diffs.data?.next_cursor ? ' · 还有下一页' : ''}
                </span>
                <div className="flex gap-1.5">
                  <button type="button" className="btn btn-secondary btn-sm" disabled={!cursor} onClick={() => setCursor(undefined)}>
                    第一页
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    disabled={!diffs.data?.next_cursor}
                    onClick={() => setCursor(diffs.data?.next_cursor ?? undefined)}
                  >
                    下一页
                  </button>
                </div>
              </div>
            </section>
          </div>

          <aside className="min-w-0 space-y-3 xl:order-2 xl:sticky xl:top-[72px] xl:self-start">
            <section className="card min-w-0">
              <div className="card-head">
                <h2 className="section-title">批次结论</h2>
                <span
                  className={`badge ${
                    batch.data.conclusive
                      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                      : 'bg-amber-50 text-amber-700 ring-amber-200'
                  }`}
                >
                  {batch.data.conclusive ? '可下结论' : '不可下结论'}
                </span>
              </div>
              <div className="card-body space-y-2.5">
                <p className="break-words text-[12px] leading-5 text-slate-700">{batch.data.status_reason}</p>
                <div className="flex flex-wrap gap-1">
                  {metrics?.suspected_anomaly ? (
                    <span className="badge bg-rose-50 text-rose-700 ring-rose-200">疑似异常</span>
                  ) : null}
                  <span
                    className={`badge ${
                      metrics?.auto_disposal_allowed
                        ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                        : 'bg-amber-50 text-amber-700 ring-amber-200'
                    }`}
                  >
                    {metrics?.auto_disposal_allowed ? '允许自动处置' : '禁止自动处置'}
                  </span>
                  <span className="badge bg-slate-100 text-slate-600 ring-slate-200">
                    差异 {batch.data.diff_count ?? diffItems.length} 行
                  </span>
                </div>

                {(batch.data.missing_items ?? []).length > 0 ? (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2">
                    <div className="text-[11.5px] font-medium text-amber-900">缺失项（不猜测填充）</div>
                    <ul className="mt-1 space-y-1 text-[10.5px] leading-4 text-amber-800">
                      {(batch.data.missing_items ?? []).map((item) => (
                        <li key={`${item.kind}-${item.code}`} className="break-words">
                          [{item.kind}] {item.code}：{item.detail}（{item.source_ref}）
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  <p className="rounded-lg bg-emerald-50 px-2.5 py-2 text-[10.5px] leading-4 text-emerald-800">
                    无缺失项，数据集完整。
                  </p>
                )}
              </div>
            </section>

            <section className="card min-w-0">
              <div className="card-head">
                <h2 className="section-title">下一步</h2>
              </div>
              <div className="card-body space-y-2 text-[11px] leading-[17px] text-slate-600">
                <p>· 差异先看证据链，再决定是否采纳建议。</p>
                <p>· 聚合匹配与累计超容差一律人工复核。</p>
                <p>· 批次可下结论后再进入计量，避免数字随差异变化。</p>
                <Link to={`/recon/${batchId}/metering`} className="btn btn-secondary btn-sm w-full">
                  查看计量表
                </Link>
              </div>
            </section>
          </aside>
        </div>
      ) : null}
    </div>
  );
}
