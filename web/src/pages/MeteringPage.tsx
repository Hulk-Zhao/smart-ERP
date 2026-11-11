import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, downloadText, type MeteringExport } from '../api/client';
import { useSession } from '../api/session';
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
    <div className="min-w-0 px-3 py-2">
      <div className="label">{label}</div>
      <div className={`mt-0.5 break-words font-mono text-[16px] font-medium num ${tone || 'text-slate-800'}`}>
        {value}
        {unit ? <span className="ml-1 text-[11px] font-normal text-slate-400">{unit}</span> : null}
      </div>
      {hint ? <div className="mt-0.5 break-words text-[10px] leading-4 text-slate-400">{hint}</div> : null}
    </div>
  );
}

// 桶名按接口 billing_bucket 的实际语义命名：
// billable 只是计费桶，不等于「应处理行数」（expected_lines），也不等于「有效对账行数」（auto_released_lines）。
const BUCKET_LABEL: Record<BillingBucket, string> = {
  billable: '计费行（billable 桶）',
  auto_released: '自动放行但扣减',
  dispute_hold: '争议挂起',
  other_unbillable: '其他不计费',
};

const BUCKET_SHORT: Record<BillingBucket, string> = {
  billable: '计费',
  auto_released: '自动放行·扣减',
  dispute_hold: '争议挂起',
  other_unbillable: '其他不计费',
};

const BUCKET_HINT: Record<BillingBucket, string> = {
  billable: '自动放行且计入计费；本期金额 = 本数 × 单价',
  auto_released: '自动放行但被扣减；扣减源接入后才会出现',
  dispute_hold: '需人工处理，不计费',
  other_unbillable: '未匹配 / 重复计费 / 不可解析',
};

// auto_released「桶」已由契约流决定删除（按该桶过滤将返回 422），生成契约更新后需从 BUCKET_ORDER/BUCKET_LABEL 移除；
// 但计数字段 auto_released_lines 保留（= 行级 auto 合计）。两者同名不同命，删桶时不要连带删计数。
const BUCKET_ORDER: BillingBucket[] = ['billable', 'auto_released', 'dispute_hold', 'other_unbillable'];

function CountCard({
  label,
  value,
  hint,
  tone = '',
  active = false,
  onClick,
  testId,
}: {
  label: string;
  value: number;
  hint?: string;
  tone?: string;
  active?: boolean;
  onClick?: () => void;
  testId?: string;
}) {
  const className = `min-w-0 rounded-xl border px-3 py-2.5 text-left transition-colors ${
    active ? 'border-indigo-400 bg-indigo-50 ring-1 ring-indigo-200' : 'border-slate-200 bg-white'
  } ${onClick ? 'cursor-pointer hover:border-indigo-300' : ''}`;
  const content = (
    <>
      <div className="flex items-center justify-between gap-1">
        <span className="label truncate">{label}</span>
        {onClick ? <span className="text-[9px] text-slate-400">{active ? '取消' : '下钻'}</span> : null}
      </div>
      <div
        className={`mt-0.5 font-mono text-[20px] font-semibold num ${tone || 'text-slate-800'}`}
        data-testid={testId ? `${testId}-value` : undefined}
      >
        {value}
      </div>
      {hint ? <div className="mt-0.5 break-words text-[10px] leading-4 text-slate-400">{hint}</div> : null}
    </>
  );
  return onClick ? (
    <button type="button" aria-pressed={active} className={className} onClick={onClick} data-testid={testId}>
      {content}
    </button>
  ) : (
    <div className={className} data-testid={testId}>
      {content}
    </div>
  );
}

function DataGapPanel({ gaps }: { gaps: MeteringDataGapOut[] }) {
  if (gaps.length === 0) return null;
  return (
    <div
      className="min-w-0 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2.5"
      data-testid="metering-data-gaps"
    >
      <div className="text-[12px] font-semibold text-amber-900">这几项暂无数据源</div>
      <p className="mt-0.5 text-[10px] leading-4 text-amber-700">
        以下字段当前数据模型提供不了，界面按「—」展示并保留扣减口径，不用 0 冒充真实值。
      </p>
      <ul className="mt-1.5 space-y-1">
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

type MeteringLine = NonNullable<MeteringStatementOut['lines']>[number];

function MeteringRow({ line }: { line: MeteringLine }) {
  return (
    <tr>
      <td className="sticky left-0 z-[1] bg-white font-mono text-[11.5px] text-slate-700 group-hover:bg-slate-50">
        #{line.line_no}
      </td>
      <td className="hidden text-[11px] text-slate-500 sm:table-cell">{line.side === 'supplier' ? '供应商侧' : '我方'}</td>
      <td>
        <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{line.status}</span>
      </td>
      <td>
        <span
          className={`badge ${
            line.billing_bucket === 'billable'
              ? 'bg-indigo-50 text-indigo-700 ring-indigo-200'
              : line.billing_bucket === 'dispute_hold'
                ? 'bg-amber-50 text-amber-700 ring-amber-200'
                : 'bg-slate-100 text-slate-600 ring-slate-200'
          }`}
          title={BUCKET_LABEL[line.billing_bucket]}
        >
          {BUCKET_SHORT[line.billing_bucket]}
        </span>
      </td>
      <td className="hidden lg:table-cell">
        {line.auto_released ? (
          <span className="inline-flex items-center gap-1 text-[11px] text-emerald-700">
            <span className="status-dot bg-emerald-500" />
            自动
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[11px] text-amber-700">
            <span className="status-dot bg-amber-400" />
            未自动
          </span>
        )}
        {line.needs_manual ? <span className="ml-1 text-[10px] text-rose-600">需人工</span> : null}
      </td>
      <td className="hidden max-w-[90px] truncate text-[11px] text-slate-600 lg:table-cell" title={text(line.owner)}>
        {text(line.owner)}
      </td>
      <td className="hidden max-w-[110px] truncate font-mono text-[11px] text-slate-500 md:table-cell" title={text(line.supplier_code)}>
        {text(line.supplier_code)}
      </td>
      <td className="hidden max-w-[110px] truncate font-mono text-[11px] text-slate-500 xl:table-cell" title={text(line.ticket_no)}>
        {text(line.ticket_no)}
      </td>
      <td className="hidden max-w-[220px] xl:table-cell">
        <div className="truncate font-mono text-[10.5px] text-slate-600" title={`${text(line.our_ref)} / ${text(line.supplier_ref)}`}>
          {text(line.our_ref)} / {text(line.supplier_ref)}
        </div>
        <div className="truncate font-mono text-[10px] text-slate-400" title={`前像 ${text(line.before_ref)} / 后像 ${text(line.after_ref)}`}>
          前 {text(line.before_ref)} · 后 {text(line.after_ref)}
        </div>
      </td>
      <td className="hidden text-right font-mono text-[11px] text-slate-600 sm:table-cell">{line.confidence}</td>
      <td className="hidden max-w-[180px] lg:table-cell">
        {line.excluded_reason ? (
          <span className="block truncate text-[10.5px] text-amber-700" title={line.excluded_reason}>
            {line.excluded_reason}
          </span>
        ) : null}
        {(line.diff_types ?? []).length > 0 ? (
          <span className="mt-0.5 flex flex-wrap gap-0.5">
            {(line.diff_types ?? []).slice(0, 2).map((type, index) => (
              <DiffTypeBadge key={`${type}-${index}`} type={type} />
            ))}
          </span>
        ) : null}
        {line.human_modified === true ? <span className="text-[10px] text-sky-700">人工改动过</span> : null}
      </td>
    </tr>
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
  const session = useSession();
  const { batchId = '' } = useParams();
  const [exported, setExported] = useState<
    (MeteringExport & { downloadedAt: string; digest?: DigestState }) | null
  >(null);
  const [bucket, setBucket] = useState<BillingBucket | null>(null);
  const toggleBucket = (next: BillingBucket) => setBucket((current) => (current === next ? null : next));

  const statement = useQuery({
    queryKey: ['metering', batchId],
    queryFn: () => api.getMeteringStatement(batchId),
    enabled: session.canQuery && Boolean(batchId),
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

  if (statement.isPending) {
    return (
      <div className="space-y-3">
        <div className="skeleton h-16 w-full" />
        <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((key) => <div key={key} className="skeleton h-20 w-full" />)}
        </div>
        <div className="skeleton h-72 w-full" />
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
  // 卡片数字与下钻过滤共用同一个数组与同一个 billing_bucket 判定，保证「点进去的数 = 卡片上的数」。
  const bucketCounts = allLines.reduce<Record<BillingBucket, number>>(
    (counts, line) => {
      counts[line.billing_bucket] += 1;
      return counts;
    },
    { billable: 0, auto_released: 0, dispute_hold: 0, other_unbillable: 0 },
  );
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
      : `${missingDeductions.map((item) => `${item.label}（${item.field}）`).join(' 与 ')}当前无数据源，按 0 扣减，因此计费行数只会小于等于真实值；`;

  return (
    <div className="min-w-0 space-y-4">
      <section className="card min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <Link to={`/recon/${batchId}`} className="link inline-flex min-h-[32px] items-center text-[11.5px]">
              ← 返回批次
            </Link>
            <h1 className="mt-0.5 break-all text-[18px] font-semibold tracking-tight text-slate-900">
              计量表 · 批次 {data.batch_id}
            </h1>
            <p className="mt-1 break-words text-[11px] text-slate-500">
              {data.period} · {data.batch_status} · {data.currency} · 计算于 {dateTime(data.computed_at)}
            </p>
          </div>
          <button
            type="button"
            className="btn btn-primary min-h-[36px]"
            disabled={csv.isPending}
            onClick={() => csv.mutate()}
          >
            {csv.isPending ? '导出中…' : '下载 CSV'}
          </button>
        </div>
        <div className="grid grid-cols-2 divide-x divide-y divide-slate-100 border-t border-slate-100 lg:grid-cols-4 lg:divide-y-0">
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
          <Metric label="单价" value={amount(data.unit_price)} unit={data.currency} hint={`来源：${data.unit_price_source}`} />
        </div>
      </section>

      {csv.error ? <ErrorPanel error={csv.error} onRetry={() => csv.mutate()} onOpenSettings={openSettingsPanel} /> : null}

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">金额与口径</h2>
          <span className="text-[10.5px] text-slate-400">金额均为契约原值，前端不重算</span>
        </div>
        <div className="card-body space-y-2.5">
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
          <span className="text-[10.5px] text-slate-400">卡片数字与下钻列表同源（接口 billing_bucket），点进去的行数 = 卡片数字</span>
        </div>
        <div className="card-body space-y-3">
          {/* 口径数字：只展示、不可下钻；术语与接口字段一一对应，不互相借用 */}
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="eyebrow">口径数字 · 不可下钻</span>
              <span className="text-[10px] text-slate-400">字段名与接口一致</span>
            </div>
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4" data-testid="metering-caliber">
              <Metric
                label="供应商对账单行数（应处理）"
                value={String(data.expected_lines)}
                hint="接口 expected_lines（metrics.supplier_lines），含不计费行"
              />
              {/* auto_released_lines 是计数字段（行级 auto 合计），与已删除的 auto_released 桶不是一回事：
                  字段值 ≠ 桶计数（桶恒 0，字段为 59）。删桶不等于删这个字段。 */}
              <Metric
                label="有效对账行数"
                value={String(data.auto_released_lines)}
                hint="行级 auto，含已自动归因的差异行（auto_released_lines）"
                tone="text-emerald-700"
              />
              <Metric
                label="计费行数（billable 桶）"
                value={String(data.billable_lines)}
                hint="billable_lines = 自动放行 − 已确认错放行 − 客户原因（上界）"
              />
              <Metric
                label="本次明细行数"
                value={String(data.line_count)}
                hint={`接口本次返回 ${allLines.length} 行；与对账单不一致时见 data_gaps`}
              />
            </div>
          </div>

          {/* 两数本期相等必须当场解释，否则扣减上线后分开会被当成系统算错 */}
          <p className="break-words rounded-lg bg-slate-50 px-3 py-2 text-[10.5px] leading-4 text-slate-600" data-testid="metering-valid-vs-billable">
            有效对账行数 {data.auto_released_lines} 与计费行数 {data.billable_lines} 定义不同：
            {'本期无扣减，故两者相等；发生扣减后计费行数将小于有效对账行数。'}
          </p>

          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="eyebrow">四类行 · 可下钻</span>
              <span className="text-[10px] text-slate-400">按明细行 billing_bucket 现算</span>
            </div>
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              {BUCKET_ORDER.map((bucketKey) => (
                <CountCard
                  key={bucketKey}
                  testId={`metering-bucket-${bucketKey}`}
                  label={BUCKET_LABEL[bucketKey]}
                  value={bucketCounts[bucketKey]}
                  hint={BUCKET_HINT[bucketKey]}
                  tone={
                    bucketKey === 'auto_released'
                      ? 'text-emerald-700'
                      : bucketKey === 'dispute_hold'
                        ? 'text-amber-700'
                        : bucketKey === 'other_unbillable'
                          ? 'text-slate-700'
                          : 'text-indigo-700'
                  }
                  active={bucket === bucketKey}
                  onClick={() => toggleBucket(bucketKey)}
                />
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-1.5 rounded-lg border border-slate-200 px-3 py-2 text-[11px] text-slate-600 sm:grid-cols-2">
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
            className="min-w-0 rounded-xl border border-indigo-200 bg-indigo-50/70 px-3 py-2.5 text-[11px] leading-5 text-indigo-950"
            data-testid="metering-upper-bound"
          >
            <b>计费行数（billable 桶）是上界，不是精确值。</b>口径：{data.billable_basis}
            。{deductionText}{data.line_accounting}
            。当前计费行数（billable_lines）：
            <span className="font-mono" data-testid="metering-billable-lines">
              {data.billable_lines}
            </span>
            。
            <span className="mt-1 block break-words">
              注意：billable_lines 只数 billable 桶，不等于「有效对账行数」；有效对账行数为 auto_released_lines ={' '}
              <span className="font-mono" data-testid="metering-valid-lines">
                {data.auto_released_lines}
              </span>
              （行级 auto，含已自动归因的差异行）。
            </span>
          </div>
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">算式</h2>
          <span className="text-[10.5px] text-slate-400">契约 calculation 原文</span>
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
        {(data.calculation ?? []).length > 0 ? (
          <p className="mt-1 break-words px-4 pb-3 text-[10px] leading-4 text-amber-700" data-testid="metering-calculation-gap">
            服务端 calculation 文案把 billable_lines 写作「有效对账行数」，与商务口径不一致；前端不改写契约原文
            （mock.ts 中同样是错的：那两行镜像自 apps/api/metering.py，演示与截图都取自它）。
            字段映射以上方「有效对账行数 = auto_released_lines、计费行数 = billable_lines」为准。
            删除条件：API 修好文案后，删除本标注、mock 中的镜像文案，以及断言本标注的用例。
          </p>
        ) : null}
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">CSV 导出与完整性</h2>
          <span className="text-[10.5px] text-slate-400">响应头 X-Content-SHA256</span>
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

      <section className="card min-w-0 overflow-hidden">
        <div className="card-head">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h2 className="section-title">逐行明细</h2>
            <span className="text-[10.5px] text-slate-400">移动端隐藏次要列，不横向滚动</span>
          </div>
          <span className="flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
            {bucket ? (
              <>
                已筛选：{BUCKET_LABEL[bucket]} · {visibleLines.length} 行 / 共 {data.line_count} 行
                <button type="button" className="link text-[11px]" onClick={() => setBucket(null)}>
                  清除筛选
                </button>
              </>
            ) : (
              <>共 {data.line_count} 行 · 点击上方计数卡可按桶下钻</>
            )}
          </span>
        </div>

        {visibleLines.length === 0 ? (
          <div className="p-3">
            <EmptyState
              variant="no-data"
              compact
              description={
                bucket
                  ? `${BUCKET_LABEL[bucket]}下暂无行；可清除筛选查看全部。`
                  : '该批次没有计量行：本期确实无数据，不是加载失败。'
              }
            />
          </div>
        ) : (
          <div className="max-h-[640px] overflow-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th className="w-[54px]">行号</th>
                  <th className="hidden w-[64px] sm:table-cell">侧</th>
                  <th className="w-[86px]">状态</th>
                  <th className="w-[96px]">桶</th>
                  <th className="hidden w-[92px] lg:table-cell">自动放行</th>
                  <th className="hidden w-[76px] lg:table-cell">责任方</th>
                  <th className="hidden w-[104px] md:table-cell">供应商</th>
                  <th className="hidden w-[104px] xl:table-cell">工单</th>
                  <th className="hidden xl:table-cell">我方 / 对方单据</th>
                  <th className="hidden w-[52px] text-right sm:table-cell">置信度</th>
                  <th className="hidden w-[150px] lg:table-cell">不计费原因 / 差异</th>
                </tr>
              </thead>
              <tbody>
                {visibleLines.map((line) => (
                  <MeteringRow key={`${line.line_no}-${line.side}`} line={line} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
