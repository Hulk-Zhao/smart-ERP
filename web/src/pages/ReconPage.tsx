import { useMemo, useState } from 'react';
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api, type ReconRunPayload } from '../api/client';
import type { ReconDatasetIn, ReconBatchSummaryOut } from '../api/generated/contract';
import { useApiConfig } from '../api/config';
import { useSession } from '../api/session';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { SessionNotice } from '../components/SessionNotice';
import { dateTime, money, shortId } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

const BATCH_PAGE_SIZE = 10;

/** 旧批次没有计数投影，服务端返回 null —— 必须渲染成「—」，不能折算成 0。 */
function countValue(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : String(value);
}

function missingMetric(value: unknown): boolean {
  return value === null || value === undefined;
}

function BatchStatusCell({ status }: { status: string }) {
  const tone =
    status === 'reconciled' || status === 'completed'
      ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
      : status === 'running' || status === 'pending'
        ? 'bg-sky-50 text-sky-700 ring-sky-200'
        : status === 'failed'
          ? 'bg-rose-50 text-rose-700 ring-rose-200'
          : 'bg-slate-100 text-slate-600 ring-slate-200';
  return <span className={`badge ${tone}`}>{status}</span>;
}

function BatchMetrics({ batch }: { batch: ReconBatchSummaryOut }) {
  const missing = missingMetric(batch.line_count) || missingMetric(batch.diff_count) || missingMetric(batch.diff_amount);
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
      <span className="text-slate-500">
        行数{' '}
        <span
          className="font-mono text-slate-700"
          data-testid={missingMetric(batch.line_count) ? 'batch-metric-missing' : undefined}
          title={missingMetric(batch.line_count) ? '旧批次没有计数投影，服务端返回 null' : undefined}
        >
          {countValue(batch.line_count)}
        </span>
      </span>
      <span className="text-slate-500">
        差异{' '}
        <span
          className="font-mono text-slate-700"
          data-testid={missingMetric(batch.diff_count) ? 'batch-metric-missing' : undefined}
          title={missingMetric(batch.diff_count) ? '旧批次没有计数投影，服务端返回 null' : undefined}
        >
          {countValue(batch.diff_count)}
        </span>
      </span>
      <span className="text-slate-500">
        差异金额{' '}
        <span
          className="font-mono text-slate-700"
          data-testid={missingMetric(batch.diff_amount) ? 'batch-metric-missing' : undefined}
          title={missingMetric(batch.diff_amount) ? '旧批次没有计数投影，服务端返回 null' : undefined}
        >
          {missingMetric(batch.diff_amount) ? '—' : money(batch.diff_amount)}
        </span>
      </span>
      {missing ? <span className="text-[10px] text-amber-600">未投影（非 0）</span> : null}
    </div>
  );
}

export function ReconPage() {
  const config = useApiConfig();
  const session = useSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [suppliers, setSuppliers] = useState('');
  const [erp, setErp] = useState(true);
  const [mes, setMes] = useState(false);
  const [wms, setWms] = useState(false);
  const [statementFiles, setStatementFiles] = useState('');
  const [datasetText, setDatasetText] = useState('');
  const [formError, setFormError] = useState<string | null>(null);

  const supplierList = useMemo(
    () => suppliers.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean),
    [suppliers],
  );

  // 批次列表同样是身份相关读接口：会话未就绪时不发，避免 401 被误读成「没有批次」。
  const batches = useInfiniteQuery({
    queryKey: ['recon', 'batches'],
    queryFn: ({ pageParam }) => api.listReconBatches({ limit: BATCH_PAGE_SIZE, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: session.canQuery,
  });
  const batchItems = batches.data?.pages.flatMap((page) => page.items) ?? [];

  const mutation = useMutation({
    mutationFn: (payload: ReconRunPayload) => api.createReconRun(payload),
    onSuccess: (batch) => {
      void queryClient.invalidateQueries({ queryKey: ['recon', 'batches'] });
      navigate(`/recon/${batch.batch_id}`);
    },
  });

  const submit = () => {
    setFormError(null);
    if (supplierList.length === 0) {
      setFormError('至少填写一个供应商编码（suppliers 为必填，最多 200 个）。');
      return;
    }
    let dataset: ReconDatasetIn | null = null;
    if (datasetText.trim()) {
      try {
        dataset = JSON.parse(datasetText) as ReconDatasetIn;
      } catch (error) {
        setFormError(`数据集 JSON 解析失败：${(error as Error).message}`);
        return;
      }
    }
    const files = statementFiles.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);
    mutation.mutate({
      suppliers: supplierList,
      sources: { erp, mes, wms, statement_files: files },
      dataset,
    });
  };

  return (
    <div className="min-w-0 space-y-4">
      <div className="min-w-0">
        <h1 className="text-[18px] font-semibold text-slate-900">对账</h1>
        <p className="mt-1 break-words text-[12px] text-slate-500">
          发起真实 /v1/recon/runs；最近批次来自 /v1/recon/batches（键集分页）。scope：{config.scope.org_id} · {config.scope.period} · {config.scope.system_ids.join(',')}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.5fr_1fr]">
        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">发起对账</h2>
            <span className="text-[11px] text-slate-400">供应商为必填</span>
          </div>
          <div className="card-body space-y-3">
            <label className="block min-w-0">
              <span className="label">供应商编码 suppliers（逗号/空格分隔，最多 200）</span>
              <input
                className="input mt-1"
                value={suppliers}
                onChange={(event) => setSuppliers(event.target.value)}
                placeholder="S-001, S-002"
              />
              {supplierList.length > 0 ? (
                <div className="mt-1 flex flex-wrap gap-1">
                  {supplierList.map((item) => <span key={item} className="chip">{item}</span>)}
                </div>
              ) : null}
            </label>

            <fieldset className="min-w-0">
              <legend className="label">取数来源 sources</legend>
              <div className="mt-1 flex flex-wrap gap-2">
                <label className={`chip cursor-pointer ${erp ? 'chip-active' : ''}`}>
                  <input type="checkbox" className="sr-only" checked={erp} onChange={(event) => setErp(event.target.checked)} />
                  ERP（u8）
                </label>
                <label className={`chip cursor-pointer ${mes ? 'chip-active' : ''}`}>
                  <input type="checkbox" className="sr-only" checked={mes} onChange={(event) => setMes(event.target.checked)} />
                  MES
                </label>
                <label className={`chip cursor-pointer ${wms ? 'chip-active' : ''}`}>
                  <input type="checkbox" className="sr-only" checked={wms} onChange={(event) => setWms(event.target.checked)} />
                  WMS
                </label>
              </div>
            </fieldset>

            <label className="block min-w-0">
              <span className="label">对账单文件 statement_files（可选，逗号分隔）</span>
              <input
                className="input mt-1"
                value={statementFiles}
                onChange={(event) => setStatementFiles(event.target.value)}
                placeholder="statement-2026-10.xlsx"
              />
            </label>

            <details className="min-w-0 rounded-lg border border-slate-200 px-3 py-2">
              <summary className="cursor-pointer text-[12px] font-medium text-slate-700">手工注入数据集（可选，离线联调）</summary>
              <p className="mt-1 text-[11px] leading-5 text-slate-500">
                必须是 ReconDatasetIn JSON：{'{ "our_lines": [...], "supplier_lines": [...], "master_data": {...} }'}。
                不填则由服务端按 statement_files 找已登记数据集；找不到会明确报缺，不会猜测填充。
              </p>
              <textarea
                className="input mt-2 min-h-[120px] font-mono text-[11px]"
                value={datasetText}
                onChange={(event) => setDatasetText(event.target.value)}
                placeholder='{"our_lines":[],"supplier_lines":[]}'
              />
            </details>

            {formError ? <p role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-700">{formError}</p> : null}
            {mutation.error ? (
              <ErrorPanel error={mutation.error} onRetry={submit} onOpenSettings={openSettingsPanel} compact />
            ) : null}

            <div className="flex flex-wrap justify-end gap-2">
              <button type="button" className="btn btn-secondary" onClick={() => { setSuppliers(''); setDatasetText(''); setFormError(null); mutation.reset(); }}>
                清空
              </button>
              <button type="button" className="btn btn-primary" disabled={mutation.isPending} onClick={submit}>
                {mutation.isPending ? '提交中…' : '发起对账'}
              </button>
            </div>
          </div>
        </section>

        <div className="min-w-0 space-y-4">
          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">最近批次</h2>
              <span className="text-[11px] text-slate-400">
                {session.canQuery ? `${batchItems.length} 条` : '—'}
              </span>
              <button
                type="button"
                className="btn btn-ghost btn-sm ml-auto"
                disabled={!session.canQuery || batches.isFetching}
                onClick={() => void batches.refetch()}
              >
                {batches.isFetching ? '刷新中…' : '刷新'}
              </button>
            </div>

            {session.status === 'loading' ? (
              <div className="space-y-2 p-3">
                {[0, 1, 2].map((key) => (
                  <div key={key} className="skeleton h-[62px] w-full" />
                ))}
              </div>
            ) : session.unavailable ? (
              <div className="p-3">
                <SessionNotice
                  error={session.error}
                  onRetry={session.refresh}
                  onOpenSettings={openSettingsPanel}
                  compact
                />
              </div>
            ) : batches.isLoading ? (
              <div className="space-y-2 p-3">
                {[0, 1, 2].map((key) => (
                  <div key={key} className="skeleton h-[62px] w-full" />
                ))}
              </div>
            ) : batches.error ? (
              <div className="p-3">
                <ErrorPanel
                  error={batches.error}
                  onRetry={() => void batches.refetch()}
                  onOpenSettings={openSettingsPanel}
                  compact
                />
              </div>
            ) : batchItems.length === 0 ? (
              <div className="p-3">
                <EmptyState
                  variant="not-run"
                  compact
                  description="服务端还没有本租户的对账批次；发起一次对账后这里会出现真实批次。"
                />
              </div>
            ) : (
              <>
                <ul className="divide-y divide-slate-100">
                  {batchItems.map((batch) => (
                    <li key={batch.batch_id} className="min-w-0 px-3 py-2" data-testid="batch-row">
                      <Link
                        to={`/recon/${batch.batch_id}`}
                        className="block min-w-0 rounded-lg px-1 py-1 hover:bg-slate-50"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span
                            className="break-all font-mono text-[11px] text-slate-700"
                            title={batch.batch_id}
                          >
                            {shortId(batch.batch_id)}
                          </span>
                          <BatchStatusCell status={batch.status} />
                          <span className="ml-auto shrink-0 text-[10.5px] text-slate-400">
                            {dateTime(batch.created_at)}
                          </span>
                        </div>
                        <BatchMetrics batch={batch} />
                      </Link>
                    </li>
                  ))}
                </ul>
                <p className="border-t border-slate-100 px-3 py-2 text-[10.5px] leading-5 text-slate-400">
                  键集分页，按创建时间倒序。「—」表示该批次没有计数投影（旧批次），不是 0。
                </p>
                {batches.hasNextPage ? (
                  <div className="px-3 pb-3">
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm w-full"
                      disabled={batches.isFetchingNextPage}
                      onClick={() => void batches.fetchNextPage()}
                    >
                      {batches.isFetchingNextPage ? '加载中…' : '加载更多'}
                    </button>
                  </div>
                ) : null}
              </>
            )}
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">对账口径</h2>
            </div>
            <ul className="card-body space-y-1 text-[11px] leading-5 text-slate-500">
              <li>· 差异行必须带字段级证据与原始单据坐标，可下钻核对。</li>
              <li>· 舍入容差仅放行「数量单价全等 + 单行 ≤ 0.05」；累计超阈值强制转人工。</li>
              <li>· 聚合匹配（拆单/合并）一律人工复核。</li>
            </ul>
          </section>
        </div>
      </div>
    </div>
  );
}
