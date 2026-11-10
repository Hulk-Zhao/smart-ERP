import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, downloadText, type MeteringExport } from '../api/client';
import type { BillingBucket, MeteringDataGapOut, MeteringStatementOut } from '../api/types';
import { DiffTypeBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { amount, dateTime, text } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

function gapFor(gaps: MeteringDataGapOut[], patterns: string[]): MeteringDataGapOut | undefined {
  return gaps.find((gap) => patterns.some((pattern) => gap.field.toLowerCase().includes(pattern)));
}

/** 契约明确为 null 的字段：显示「—」，并给出为什么，不用 0 冒充。 */
function missingReason(gaps: MeteringDataGapOut[], patterns: string[], fallback: string): string {
  return gapFor(gaps, patterns)?.reason ?? fallback;
}

function Metric({
  label,
  value,
  unit,
  hint,
  tone = '',
}: {
  label: string;
  value: string;
  unit?: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 px-2.5 py-2">
      <div className="label">{label}</div>
      <div className={`mt-0.5 break-words font-mono text-[15px] ${tone || 'text-slate-800'}`}>
        {value}
        {unit ? <span className="ml-1 text-[11px] font-normal text-slate-400">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-0.5 break-words text-[10px] leading-4 text-slate-400">{hint}</div> : null}
    </div>
  );
}

const BUCKET_LABEL: Record<BillingBucket, string> = {
  billable: '应处理',
  auto_released: '自动放行',
  dispute_hold: '争议挂起',
  other_unbillable: '其他不计费',
};

function CountCard({
  label,
  value,
  hint,
  tone = '',
  active = false,
  onClick,
}: {
  label: string;
  value: number;
  hint?: string;
  tone?: string;
  active?: boolean;
  onClick?: () => void;
}) {
  const className = `min-w-0 rounded-lg border px-2.5 py-2 text-left ${
    active ? 'border-sky-400 bg-sky-50 ring-1 ring-sky-200' : 'border-slate-200'
  } ${onClick ? 'cursor-pointer hover:border-sky-300' : ''}`;
  const content = (
    <>
      <div className="label">{label}</div>
      <div className={`mt-0.5 font-mono text-[18px] ${tone || 'text-slate-800'}`}>{value}</div>
      {hint ? <div className="mt-0.5 break-words text-[10px] leading-4 text-slate-400">{hint}</div> : null}
      {onClick ? (
        <div className="mt-0.5 text-[10px] text-sky-700">{active ? '再次点击取消筛选' : '点击查看该桶行列表'}</div>
      ) : null}
    </>
  );
  return onClick ? (
    <button type="button" aria-pressed={active} className={className} onClick={onClick}>
      {content}
    </button>
  ) : (
    <div className={className}>{content}</div>
  );
}

function DataGapPanel({ gaps }: { gaps: MeteringDataGapOut[] }) {
  if (gaps.length === 0) return null;
  return (
    <div
      className="min-w-0 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2"
      data-testid="metering-data-gaps"
    >
      <div className="text-[12px] font-medium text-amber-900">这几项暂无数据源</div>
      <p className="mt-0.5 text-[10px] leading-4 text-amber-700">
        以下字段当前数据模型提供不了，界面按「—」展示并保留扣减口径，不用 0 冒充真实值。
      </p>
      <ul className="mt-1 space-y-1">
        {gaps.map((gap) => (
          <li key={`${gap.field}-${gap.reason}`} className="min-w-0 text-[11px] leading-4 text-amber-800">
            <span className="break-all font-mono">{gap.field}</span>
            <span className="mx-1">·</span>
            <span className="break-words">{gap.reason}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function LineCard({ line }: { line: NonNullable<MeteringStatementOut['lines']>[number] }) {
  return (
    <div className="min-w-0 rounded-lg border border-slate-200 px-2.5 py-2">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <span className="font-mono text-[12px] text-slate-800">#{line.line_no}</span>
        <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{line.side === 'supplier' ? '供应商侧' : '我方'}</span>
        <span className="badge bg-white text-slate-600 ring-slate-200">{line.status}</span>
        {line.auto_released ? (
          <span className="badge bg-emerald-50 text-emerald-700 ring-emerald-200">自动放行</span>
        ) : (
          <span className="badge bg-amber-50 text-amber-700 ring-amber-200">未自动放行</span>
        )}
        {line.needs_manual ? <span className="badge bg-rose-50 text-rose-700 ring-rose-200">需人工</span> : null}
        {line.human_modified === null ? (
          <span className="badge bg-slate-100 text-slate-500 ring-slate-200">人工改动 —</span>
        ) : line.human_modified ? (
          <span className="badge bg-sky-50 text-sky-700 ring-sky-200">人工改动过</span>
        ) : null}
        <span className="ml-auto font-mono text-[10px] text-slate-400">置信度 {line.confidence}</span>
      </div>

      <dl className="mt-1.5 grid grid-cols-1 gap-x-4 gap-y-1 text-[11px] sm:grid-cols-2">
        <div className="min-w-0">
          <dt className="label">责任方 / 供应商</dt>
          <dd className="mt-0.5 break-words">{text(line.owner)} · {text(line.supplier_code)}</dd>
        </div>
        <div className="min-w-0">
          <dt className="label">工单</dt>
          <dd className="mt-0.5 break-all font-mono">{text(line.ticket_no)}</dd>
        </div>
        <div className="min-w-0">
          <dt className="label">我方 / 对方单据</dt>
          <dd className="mt-0.5 break-all font-mono">{text(line.our_ref)} / {text(line.supplier_ref)}</dd>
        </div>
        <div className="min-w-0">
          <dt className="label">前像 / 后像</dt>
          <dd className="mt-0.5 break-all font-mono">{text(line.before_ref)} / {text(line.after_ref)}</dd>
        </div>
        {(line.source_refs ?? []).length > 0 ? (
          <div className="min-w-0 sm:col-span-2">
            <dt className="label">来源</dt>
            <dd className="mt-0.5 break-all font-mono text-[10px]">{(line.source_refs ?? []).join('、')}</dd>
          </div>
        ) : null}
        {line.excluded_reason ? (
          <div className="min-w-0 sm:col-span-2">
            <dt className="label">不计费原因</dt>
            <dd className="mt-0.5 break-words">{line.excluded_reason}</dd>
          </div>
        ) : null}
      </dl>

      {(line.diff_types ?? []).length > 0 ? (
        <div className="mt-1.5 flex min-w-0 flex-wrap gap-1">
          {(line.diff_types ?? []).map((type, index) => (
            <DiffTypeBadge key={`${type}-${index}`} type={type} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function normalizeSha(value: string | undefined): string | undefined {
  const cleaned = value?.replace(/^sha256:/i, '').trim().toLowerCase();
  return cleaned || undefined;
}

/** 用 Web Crypto 重算下载文件的 SHA-256；环境不支持时返回 null，由界面显式降级。 */
async function sha256Hex(text: string): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return null;
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

type DigestState =
  | { status: 'unsupported' }
  | { status: 'done'; local: string; match: boolean | null };

export function MeteringPage() {
  const { batchId = '' } = useParams();
  const [exported, setExported] = useState<
    (MeteringExport & { downloadedAt: string; digest?: DigestState }) | null
  >(null);
  const [bucket, setBucket] = useState<BillingBucket | null>(null);
  const toggleBucket = (next: BillingBucket) => setBucket((current) => (current === next ? null : next));

  const statement = useQuery({
    queryKey: ['metering', batchId],
    queryFn: () => api.getMeteringStatement(batchId),
    enabled: Boolean(batchId),
  });

  const csv = useMutation({
    mutationFn: () => api.exportMeteringCsv(batchId),
    onSuccess: (result) => {
      const body = downloadText(result.filename, result.csv);
      setExported({ ...result, downloadedAt: new Date().toISOString(), digest: { status: 'unsupported' } });
      const server = normalizeSha(result.sha256);
      void sha256Hex(body)
        .then((local) => {
          setExported((current) =>
            current
              ? {
                  ...current,
                  digest: local
                    ? { status: 'done', local, match: server ? local === server : null }
                    : { status: 'unsupported' },
                }
              : current,
          );
        })
        .catch(() => undefined);
    },
  });

  if (!batchId) {
    return <ErrorPanel error={{ status: 422, code: 'E-DATA-MISSING', detail: '缺少 batch_id' }} />;
  }

  if (statement.isLoading) {
    return (
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {[0, 1, 2, 3, 4, 5, 6, 7].map((key) => <div key={key} className="skeleton h-16 w-full" />)}
      </div>
    );
  }

  if (statement.error) {
    return (
      <ErrorPanel
        error={statement.error}
        onRetry={() => void statement.refetch()}
        onOpenSettings={openSettingsPanel}
        notFoundKind="metering"
        backTo={{ to: `/recon/${batchId}`, label: '← 返回批次' }}
      />
    );
  }

  const data = statement.data!;
  const allLines = data.lines ?? [];
  const visibleLines = bucket ? allLines.filter((line) => line.billing_bucket === bucket) : allLines;
  const gaps = data.data_gaps ?? [];
  const excluded = data.excluded_lines;
  const capGap = missingReason(gaps, ['cap_amount', 'cap'], '服务端未返回封顶配置');

  // 上界说明点名「哪几项按 0 扣减」，而不是写死两项。
  const missingDeductions = [
    { field: 'confirmed_misrelease_lines', label: '已确认错放行', value: data.confirmed_misrelease_lines },
    { field: 'customer_reason_lines', label: '客户原因', value: data.customer_reason_lines },
  ].filter((item) => item.value === null || item.value === undefined);
  const deductionText =
    missingDeductions.length === 0
      ? '两项扣减项均有数据源，无需按 0 兜底；'
      : `${missingDeductions.map((item) => `${item.label}（${item.field}）`).join(' 与 ')}当前无数据源，按 0 扣减，因此有效行数只会小于等于真实值；`;

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link to={`/recon/${batchId}`} className="link inline-flex min-h-[44px] items-center text-[12px]">
            ← 返回批次
          </Link>
          <h1 className="mt-1 break-all text-[16px] font-semibold text-slate-900">计量表 · 批次 {data.batch_id}</h1>
          <p className="mt-1 break-words text-[11px] text-slate-400">
            {data.period} · {data.batch_status} · {data.currency} · 计算于 {dateTime(data.computed_at)}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-secondary min-h-[44px]"
          disabled={csv.isPending}
          onClick={() => csv.mutate()}
        >
          {csv.isPending ? '导出中…' : '下载 CSV'}
        </button>
      </div>

      {csv.error ? <ErrorPanel error={csv.error} onRetry={() => csv.mutate()} onOpenSettings={openSettingsPanel} /> : null}

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">金额与口径</h2>
          <span className="text-[11px] text-slate-400">金额均为契约原值，前端不重算</span>
        </div>
        <div className="card-body space-y-2">
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            <Metric label="本期金额" value={amount(data.period_amount)} unit={data.currency} tone="text-rose-700" />
            <Metric label="累计金额" value={amount(data.cumulative_amount)} unit={data.currency} hint={`上期累计 ${amount(data.prior_cumulative_amount)}`} />
            <Metric
              label="封顶余额"
              value={data.cap_remaining === null || data.cap_remaining === undefined ? '—' : amount(data.cap_remaining)}
              unit={data.cap_remaining === null || data.cap_remaining === undefined ? undefined : data.currency}
              hint={
                data.cap_remaining === null || data.cap_remaining === undefined
                  ? capGap
                  : `封顶 ${amount(data.cap_amount)} / ${data.cap_exceeded ? '已超封顶' : '未超封顶'}`
              }
              tone={data.cap_exceeded ? 'text-rose-700' : 'text-slate-800'}
            />
            <Metric
              label="单价"
              value={amount(data.unit_price)}
              unit={data.currency}
              hint={`来源：${data.unit_price_source}`}
            />
          </div>

          {/* data_gaps 必须显式可见，紧贴金额，不折叠 */}
          <DataGapPanel gaps={gaps} />

          {data.conclusive === false ? (
            <div className="min-w-0 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-4 text-amber-800">
              该批次结论未定（conclusive=false），本计量表仅反映当前程序性事实。
            </div>
          ) : null}
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">四类行</h2>
          <span className="text-[11px] text-slate-400">计数来自契约，不是前端估算</span>
        </div>
        <div className="card-body space-y-2">
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            <CountCard
              label={BUCKET_LABEL.billable}
              value={data.expected_lines}
              hint="本批次需要对账的行"
              active={bucket === 'billable'}
              onClick={() => toggleBucket('billable')}
            />
            <CountCard
              label={BUCKET_LABEL.auto_released}
              value={data.auto_released_lines}
              hint="程序性事实：自动通过"
              tone="text-emerald-700"
              active={bucket === 'auto_released'}
              onClick={() => toggleBucket('auto_released')}
            />
            <CountCard
              label={BUCKET_LABEL.dispute_hold}
              value={excluded.dispute_hold}
              hint={`不计费合计 ${excluded.total}`}
              tone="text-amber-700"
              active={bucket === 'dispute_hold'}
              onClick={() => toggleBucket('dispute_hold')}
            />
            <CountCard
              label={BUCKET_LABEL.other_unbillable}
              value={excluded.other_unbillable}
              hint="含客户原因等其它口径"
              tone="text-slate-700"
              active={bucket === 'other_unbillable'}
              onClick={() => toggleBucket('other_unbillable')}
            />
          </div>

          <div className="grid grid-cols-1 gap-1.5 text-[11px] text-slate-600 sm:grid-cols-2">
            <div className="min-w-0">
              已确认错放行：
              <span className="font-mono">
                {data.confirmed_misrelease_lines === null || data.confirmed_misrelease_lines === undefined
                  ? '—'
                  : data.confirmed_misrelease_lines}
              </span>
              {data.confirmed_misrelease_lines === null || data.confirmed_misrelease_lines === undefined ? (
                <span className="ml-1 text-[10px] text-amber-700">
                  （{missingReason(gaps, ['misrelease'], '暂无数据源')}）
                </span>
              ) : null}
            </div>
            <div className="min-w-0">
              客户原因：
              <span className="font-mono">
                {data.customer_reason_lines === null || data.customer_reason_lines === undefined
                  ? '—'
                  : data.customer_reason_lines}
              </span>
              {data.customer_reason_lines === null || data.customer_reason_lines === undefined ? (
                <span className="ml-1 text-[10px] text-amber-700">
                  （{missingReason(gaps, ['customer_reason'], '暂无数据源')}）
                </span>
              ) : null}
            </div>
            <div className="min-w-0">
              人工复核行数：
              <span className="font-mono">
                {data.human_reviewed_lines === null || data.human_reviewed_lines === undefined
                  ? '—'
                  : data.human_reviewed_lines}
              </span>
              {data.human_reviewed_lines === null || data.human_reviewed_lines === undefined ? (
                <span className="ml-1 text-[10px] text-amber-700">
                  （{missingReason(gaps, ['human_reviewed'], '暂无数据源')}）
                </span>
              ) : null}
            </div>
            {Object.entries(excluded.other_reasons ?? {}).map(([reason, count]) => (
              <div key={reason} className="min-w-0 break-words">
                {reason}：<span className="font-mono">{String(count)}</span>
              </div>
            ))}
          </div>

          <div
            className="min-w-0 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-[11px] leading-5 text-sky-900"
            data-testid="metering-upper-bound"
          >
            <b>有效对账行数是上界，不是精确值。</b>口径：{data.billable_basis}
            。{deductionText}{data.line_accounting}
            。当前有效对账行数：<span className="font-mono">{data.billable_lines}</span>。
          </div>
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">算式</h2>
          <span className="text-[11px] text-slate-400">契约 calculation 原文</span>
        </div>
        <ol className="card-body space-y-1 text-[11px] leading-5 text-slate-700">
          {(data.calculation ?? []).length === 0 ? (
            <li className="text-slate-400">服务端未返回算式。</li>
          ) : (
            (data.calculation ?? []).map((step, index) => (
              <li key={`${index}-${step}`} className="min-w-0 break-words rounded bg-slate-50 px-2 py-1 font-mono">
                {index + 1}. {step}
              </li>
            ))
          )}
        </ol>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">CSV 导出与完整性</h2>
          <span className="text-[11px] text-slate-400">响应头 X-Content-SHA256</span>
        </div>
        <div className="card-body space-y-1.5 text-[11px] leading-5">
          {exported ? (
            <>
              <div className="min-w-0">
                已下载 <span className="break-all font-mono">{exported.filename}</span>
                （{exported.rowCount} 行，{dateTime(exported.downloadedAt)}）
              </div>
              <div className="min-w-0">
                服务端 X-Content-SHA256：
                <span className="break-all font-mono text-[10px]" data-testid="metering-sha256">
                  {exported.sha256 ?? '—'}
                </span>
              </div>
              <div className="min-w-0">
                本地重算：
                <span className="break-all font-mono text-[10px]" data-testid="metering-local-sha256">
                  {exported.digest?.status === 'done' ? exported.digest.local : '—'}
                </span>
              </div>
              <p className="break-words text-[10px]" data-testid="metering-sha-verdict">
                {exported.digest?.status !== 'done' ? (
                  <span className="text-amber-700">
                    当前环境不支持 Web Crypto，未做本地重算；可手动用 sha256sum 与服务端摘要比对。
                  </span>
                ) : exported.digest.match === true ? (
                  <span className="text-emerald-700">一致：文件内容未被改动。</span>
                ) : exported.digest.match === false ? (
                  <span className="text-rose-700">不一致：文件可能被改动，请重新下载并核对。</span>
                ) : (
                  <span className="text-amber-700">
                    服务端未返回摘要，无法比对；请检查网关是否剥离了 X-Content-SHA256。
                  </span>
                )}
              </p>
              <p className="break-words text-[10px] text-slate-500">
                也可下载后用 <span className="font-mono">sha256sum</span> 计算，与服务端摘要逐位比对。
              </p>
            </>
          ) : (
            <p className="text-slate-500">
              点击右上「下载 CSV」后，这里会显示服务端 X-Content-SHA256 与本地重算结果，用于核验文件未被改动。
            </p>
          )}
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">逐行明细</h2>
          <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
            {bucket ? (
              <>
                已筛选：{BUCKET_LABEL[bucket]}桶 · {visibleLines.length} 行 / 共 {data.line_count} 行
                <button type="button" className="link text-[11px]" onClick={() => setBucket(null)}>
                  清除筛选
                </button>
              </>
            ) : (
              <>共 {data.line_count} 行 · 点击上方计数卡可按桶下钻</>
            )}
          </span>
        </div>
        <div className="card-body space-y-2">
          {visibleLines.length === 0 ? (
            <EmptyState
              variant="no-data"
              compact
              description={
                bucket
                  ? `${BUCKET_LABEL[bucket]}桶下暂无行；可清除筛选查看全部。`
                  : '该批次没有计量行：本期确实无数据，不是加载失败。'
              }
            />
          ) : (
            visibleLines.map((line) => <LineCard key={`${line.line_no}-${line.side}`} line={line} />)
          )}
        </div>
      </section>
    </div>
  );
}
