import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { useSession } from '../api/session';
import { diffTypeMeta, type EvidenceOut } from '../api/types';
import { DiffTypeBadge } from '../components/Badges';
import { EmptyState } from '../components/EmptyState';
import { ErrorPanel } from '../components/ErrorPanel';
import { FieldDiffTable, type FieldDiffRow } from '../components/FieldDiffTable';
import { IconAlert } from '../components/icons';
import { delta, money, rate } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

function EvidenceCard({ evidence, onClose }: { evidence: EvidenceOut; onClose?: () => void }) {
  const coord = evidence.coord ?? null;
  const page = coord && typeof coord.page === 'object' ? JSON.stringify(coord.page) : coord?.page;
  const bbox = coord && Array.isArray(coord.bbox) ? (coord.bbox as unknown[]).join(', ') : null;

  return (
    <div className="min-w-0 rounded-xl border border-indigo-200 bg-indigo-50/60 px-3 py-2.5" data-testid="evidence-detail">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="badge bg-white text-indigo-800 ring-indigo-200">{evidence.system}</span>
        <span className="break-all font-mono text-[12px] font-medium text-slate-800">{evidence.doc_no}</span>
        <span className="badge bg-white text-slate-600 ring-slate-200">字段 {evidence.field}</span>
        {onClose ? (
          <button type="button" className="btn btn-ghost btn-sm ml-auto" onClick={onClose}>
            收起
          </button>
        ) : null}
      </div>

      <div className="mt-2 grid grid-cols-1 gap-1 text-[11px] text-slate-600 sm:grid-cols-2">
        <div>
          页码：<span className="font-mono font-medium text-slate-800">{page === null || page === undefined ? '未提供' : String(page)}</span>
        </div>
        <div className="break-all">
          坐标 bbox：<span className="font-mono text-slate-800">{bbox ?? '未提供'}</span>
        </div>
      </div>

      <details className="mt-1.5">
        <summary className="cursor-pointer text-[11px] text-indigo-800">原始坐标 JSON</summary>
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-white/80 p-2 font-mono text-[10px] text-slate-700">
          {coord ? JSON.stringify(coord, null, 2) : '契约未返回 coord，无法定位到原始单据坐标。'}
        </pre>
      </details>
    </div>
  );
}

function EvidenceGroup({ evidences, empty }: { evidences: EvidenceOut[]; empty: string }) {
  const [active, setActive] = useState(0);
  if (evidences.length === 0) {
    return (
      <div className="p-2.5">
        <EmptyState variant="no-data" compact title={empty} description="本期确实无数据：没有可下钻的原始单据坐标。" />
      </div>
    );
  }
  return (
    <div className="min-w-0 px-3 py-2.5">
      <ol className="space-y-1">
        {evidences.map((item, index) => {
          const selected = active === index;
          return (
            <li key={`${item.system}-${item.doc_no}-${item.field}-${index}`}>
              <button
                type="button"
                className={`flex w-full min-w-0 items-center gap-2 rounded-lg px-2 py-1.5 text-left ${
                  selected ? 'bg-indigo-50 ring-1 ring-indigo-200' : 'hover:bg-slate-50'
                }`}
                onClick={() => setActive(index)}
              >
                <span
                  className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full font-mono text-[10px] ${
                    selected ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-500'
                  }`}
                >
                  {index + 1}
                </span>
                <span className="min-w-0 flex-1 truncate text-[11.5px] text-slate-700">
                  <span className="font-mono">{item.system}</span> · {item.doc_no} · {item.field}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <div className="mt-2">
        <EvidenceCard evidence={evidences[active]} />
      </div>
    </div>
  );
}

export function ReconDiffPage() {
  const { batchId = '', diffId = '' } = useParams();
  const session = useSession();

  const query = useQuery({
    queryKey: ['recon', batchId, 'diff', diffId],
    queryFn: () => api.getReconDiff(batchId, diffId),
    enabled: session.canQuery && Boolean(batchId && diffId),
  });

  if (!batchId || !diffId) {
    return <ErrorPanel error={{ status: 422, code: 'E-DATA-MISSING', detail: '缺少 batch_id 或 diff_id' }} />;
  }

  if (query.isPending) {
    return (
      <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <div className="space-y-3">
          <div className="skeleton h-20 w-full" />
          <div className="skeleton h-64 w-full" />
        </div>
        <div className="skeleton h-72 w-full" />
      </div>
    );
  }

  if (query.error) {
    return (
      <ErrorPanel
        error={query.error}
        onRetry={() => void query.refetch()}
        onOpenSettings={openSettingsPanel}
        notFoundKind="diff"
        backTo={{ to: `/recon/${batchId}`, label: '← 返回差异列表' }}
      />
    );
  }

  const diff = query.data!;
  const meta = diffTypeMeta(diff.type);

  /**
   * 字段级 diff 只从契约真实字段构造：
   * - 数量：qty_mine / qty_supplier 是分侧值；
   * - 单据引用：our_ref / supplier_ref；
   * - 金额：契约只给单侧/归因金额，没有分侧金额时不伪造，差异列展示引擎归因金额。
   */
  const rows: FieldDiffRow[] = [];
  diff.related_lines?.forEach((line, index) => {
    const prefix = line.item ?? `行 ${index + 1}`;
    rows.push({
      key: `qty-${index}`,
      field: `数量（${prefix}）`,
      ours: line.qty_mine ?? '未提供',
      theirs: line.qty_supplier ?? '未提供',
      delta: delta(line.qty_mine, line.qty_supplier),
      reason: diff.type,
      reasonLabel: line.note || meta.label,
    });
    rows.push({
      key: `ref-${index}`,
      field: `单据引用（${prefix}）`,
      ours: line.our_ref ?? '未提供',
      theirs: line.supplier_ref ?? '未提供',
      delta: '—',
      reason: diff.type,
      reasonLabel: line.status,
    });
    rows.push({
      key: `amount-${index}`,
      field: `金额（${prefix}）`,
      ours: '契约未提供分侧金额',
      theirs: '契约未提供分侧金额',
      delta: money(line.amount ?? diff.amount),
      reason: diff.type,
      reasonLabel: `差异金额 ${money(diff.amount)}`,
    });
  });

  if (rows.length === 0) {
    rows.push({
      key: 'amount-header',
      field: '差异金额',
      ours: '未提供',
      theirs: '未提供',
      delta: money(diff.amount),
      reason: diff.type,
      reasonLabel: diff.suggestion,
    });
  }

  return (
    <div className="min-w-0 space-y-4">
      <section className="card min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <Link to={`/recon/${batchId}`} className="link text-[11.5px]">
              ← 返回批次差异列表
            </Link>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5">
              <h1 className="break-all text-[17px] font-semibold tracking-tight text-slate-900">
                {diffTypeMeta(diff.type).label}
              </h1>
              <DiffTypeBadge type={diff.type} />
              {diff.needs_manual ? (
                <span className="badge bg-amber-50 text-amber-700 ring-amber-200">需人工</span>
              ) : (
                <span className="badge bg-emerald-50 text-emerald-700 ring-emerald-200">可自动处置</span>
              )}
            </div>
            <p className="mt-1 break-all text-[11px] text-slate-400">
              {diff.ref} · 供应商 {diff.supplier_code} · 责任方 {diff.owner} · {diff.diff_id}
            </p>
          </div>
          <span className="badge bg-slate-100 text-slate-600 ring-slate-200">置信度 {rate(diff.confidence)}</span>
        </div>
        <div className="grid grid-cols-2 divide-x divide-y divide-slate-100 border-t border-slate-100 sm:grid-cols-4 sm:divide-y-0">
          {[
            { label: '差异金额', value: money(diff.amount), tone: 'text-rose-700' },
            { label: '置信度', value: rate(diff.confidence), tone: 'text-slate-900' },
            { label: '关联行', value: String(diff.related_lines?.length ?? 0), tone: 'text-slate-900' },
            { label: '证据', value: `${diff.evidence?.length ?? 0} 条`, tone: 'text-slate-900' },
          ].map((item) => (
            <div key={item.label} className="min-w-0 px-4 py-2.5">
              <div className="label">{item.label}</div>
              <div className={`mt-0.5 truncate font-mono text-[15px] font-medium num ${item.tone}`}>{item.value}</div>
            </div>
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
        <div className="min-w-0 space-y-4 xl:order-1">
          <section className="card min-w-0 overflow-hidden">
            <div className="card-head">
              <h2 className="section-title">字段级差异</h2>
              <span className="text-[10.5px] text-slate-400">我方值 / 对方值 / 差异 / 原因码 · 变更单元格已高亮</span>
            </div>
            <div className="px-3 pt-2.5">
              <p className="break-words rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] leading-5 text-slate-600">
                {diff.suggestion}
              </p>
              <p className="mt-1 text-[10px] leading-4 text-slate-400">
                口径：契约提供分侧数量与单据引用；金额只提供单侧/归因值。没有分侧金额时界面不猜测，差异列展示引擎归因金额。
              </p>
            </div>
            <div className="mt-2">
              <FieldDiffTable rows={rows} />
            </div>
          </section>

          {diff.draft ? (
            <section className="card min-w-0">
              <div className="card-head">
                <h2 className="section-title">处置草稿</h2>
                <span className="badge bg-amber-50 text-amber-700 ring-amber-200">待确认 · {diff.draft.status}</span>
              </div>
              <dl className="grid grid-cols-1 gap-x-4 gap-y-2 px-4 py-3 text-[12px] sm:grid-cols-2">
                <div className="min-w-0"><dt className="label">草稿号</dt><dd className="mt-0.5 break-all font-mono">{diff.draft.draft_no}</dd></div>
                <div className="min-w-0"><dt className="label">差异类型</dt><dd className="mt-0.5">{diffTypeMeta(diff.draft.diff_type).label}</dd></div>
                <div className="min-w-0"><dt className="label">金额</dt><dd className="mt-0.5 font-mono num">{money(diff.draft.amount)}</dd></div>
                <div className="min-w-0"><dt className="label">责任方 / 置信度</dt><dd className="mt-0.5">{diff.draft.owner} · {rate(diff.draft.confidence)}</dd></div>
                <div className="min-w-0 sm:col-span-2"><dt className="label">建议</dt><dd className="mt-0.5 break-words">{diff.draft.suggestion}</dd></div>
                <div className="min-w-0 sm:col-span-2"><dt className="label">引用</dt><dd className="mt-0.5 break-all font-mono text-[11px]">{(diff.draft.refs ?? []).join('、') || '—'}</dd></div>
                <div className="min-w-0 sm:col-span-2"><dt className="label">需人工</dt><dd className="mt-0.5">{diff.draft.needs_manual ? '是' : '否'}</dd></div>
              </dl>
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-4 py-2">
                <span className="text-[10.5px] text-slate-400">草稿不代表已执行；采纳前需人工确认。</span>
                <div className="flex gap-1.5">
                  <button type="button" className="btn btn-secondary btn-sm" disabled title="处置写接口不在本轮范围">
                    转人工
                  </button>
                  <button type="button" className="btn btn-primary btn-sm" disabled title="处置写接口不在本轮范围">
                    采纳草稿
                  </button>
                </div>
              </div>
            </section>
          ) : null}

          {diff.unattributed ? (
            <section className="card min-w-0">
              <div className="card-head">
                <h2 className="section-title">未归因金额</h2>
                <span className="font-mono text-[12px] text-rose-700 num">{money(diff.unattributed.amount_diff)}</span>
              </div>
              <p className="break-words px-4 py-2 text-[12px] text-slate-700">{diff.unattributed.reason}</p>
              <p className="break-all px-4 pb-2 text-[11px] text-slate-400">ref: {diff.unattributed.ref}</p>
              <EvidenceGroup evidences={diff.unattributed.evidence ?? []} empty="未归因金额没有证据坐标。" />
            </section>
          ) : null}
        </div>

        <aside className="min-w-0 space-y-3 xl:order-2 xl:sticky xl:top-[72px] xl:self-start">
          <section className="card min-w-0 overflow-hidden">
            <div className="card-head">
              <div className="flex items-center gap-2">
                <h2 className="section-title">证据下钻</h2>
                <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{diff.evidence?.length ?? 0}</span>
              </div>
              <span className="text-[10.5px] text-slate-400">原始单据坐标</span>
            </div>
            <EvidenceGroup evidences={diff.evidence ?? []} empty="该差异未附带证据坐标，不能自动处置，需转人工核查。" />
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">判断提示</h2>
            </div>
            <div className="card-body space-y-2 text-[11px] leading-[17px] text-slate-600">
              <p className="flex items-start gap-1.5">
                <IconAlert className="mt-0.5 shrink-0 text-amber-500" />
                证据缺失或坐标不完整时，不展示「可自动处置」。
              </p>
              <p>· 舍入容差只放行「数量单价全等 + 单行 ≤ 0.05」。</p>
              <p>· 聚合匹配（拆单/合并）即使置信度高也转人工。</p>
              <p>· 草稿是建议，不是执行结果。</p>
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}
