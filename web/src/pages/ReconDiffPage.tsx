import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { diffTypeMeta, type EvidenceOut } from '../api/types';
import { DiffTypeBadge } from '../components/Badges';
import { ErrorPanel } from '../components/ErrorPanel';
import { FieldDiffTable, type FieldDiffRow } from '../components/FieldDiffTable';
import { delta, money, rate } from '../lib/format';

function EvidenceCard({ evidence, onClose }: { evidence: EvidenceOut; onClose?: () => void }) {
  const coord = evidence.coord ?? null;
  const page = coord && typeof coord.page === 'object' ? JSON.stringify(coord.page) : coord?.page;
  const bbox = coord && Array.isArray(coord.bbox) ? (coord.bbox as unknown[]).join(', ') : null;

  return (
    <div className="min-w-0 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2" data-testid="evidence-detail">
      <div className="flex flex-wrap items-center gap-2">
        <span className="badge bg-white text-sky-800 ring-sky-200">{evidence.system}</span>
        <span className="break-all font-mono text-[12px] text-slate-800">{evidence.doc_no}</span>
        <span className="badge bg-white text-slate-600 ring-slate-200">字段 {evidence.field}</span>
        {onClose ? (
          <button type="button" className="btn btn-ghost btn-sm ml-auto" onClick={onClose}>
            收起
          </button>
        ) : null}
      </div>

      <div className="mt-2 grid grid-cols-1 gap-1 text-[11px] text-slate-600 sm:grid-cols-2">
        <div>页码：<span className="font-mono">{page === null || page === undefined ? '未提供' : String(page)}</span></div>
        <div className="break-all">坐标 bbox：<span className="font-mono">{bbox ?? '未提供'}</span></div>
      </div>

      <details className="mt-1">
        <summary className="cursor-pointer text-[11px] text-sky-800">原始坐标 JSON</summary>
        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded bg-white/70 p-2 font-mono text-[10px] text-slate-700">
          {coord ? JSON.stringify(coord, null, 2) : '契约未返回 coord，无法定位到原始单据坐标。'}
        </pre>
      </details>
    </div>
  );
}

function EvidenceGroup({ evidences, empty }: { evidences: EvidenceOut[]; empty: string }) {
  const [active, setActive] = useState(0);
  if (evidences.length === 0) {
    return <p className="px-3 py-3 text-[12px] text-slate-500">{empty}</p>;
  }
  return (
    <div className="min-w-0 px-3 py-2">
      <div className="flex min-w-0 flex-wrap gap-1.5">
        {evidences.map((item, index) => (
          <button
            key={`${item.system}-${item.doc_no}-${item.field}-${index}`}
            type="button"
            className={`chip ${active === index ? 'chip-active' : ''}`}
            onClick={() => setActive(index)}
          >
            {item.system} · {item.doc_no} · {item.field}
          </button>
        ))}
      </div>
      <div className="mt-2">
        <EvidenceCard evidence={evidences[active]} />
      </div>
    </div>
  );
}

export function ReconDiffPage() {
  const { batchId = '', diffId = '' } = useParams();

  const query = useQuery({
    queryKey: ['recon', batchId, 'diff', diffId],
    queryFn: () => api.getReconDiff(batchId, diffId),
    enabled: Boolean(batchId && diffId),
  });

  if (!batchId || !diffId) {
    return <ErrorPanel error={{ status: 422, code: 'E-DATA-MISSING', detail: '缺少 batch_id 或 diff_id' }} />;
  }

  if (query.isLoading) {
    return <div className="space-y-2">{[0, 1, 2].map((key) => <div key={key} className="skeleton h-24 w-full" />)}</div>;
  }

  if (query.error) {
    return (
      <div className="space-y-3">
        <Link to={`/recon/${batchId}`} className="link text-[12px]">← 返回差异列表</Link>
        <ErrorPanel error={query.error} onRetry={() => void query.refetch()} />
      </div>
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
      <div className="min-w-0">
        <Link to={`/recon/${batchId}`} className="link text-[12px]">← 返回批次差异列表</Link>
        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
          <h1 className="break-all text-[16px] font-semibold text-slate-900">差异 {diff.diff_id}</h1>
          <DiffTypeBadge type={diff.type} />
          {diff.needs_manual ? <span className="badge bg-amber-50 text-amber-700 ring-amber-200">需人工</span> : null}
        </div>
        <p className="mt-1 break-all text-[11px] text-slate-400">{diff.ref} · 供应商 {diff.supplier_code} · 责任方 {diff.owner}</p>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="card min-w-0 p-3">
          <div className="label">差异金额</div>
          <div className="mt-0.5 break-words font-mono text-[15px] text-rose-700">{money(diff.amount)}</div>
        </div>
        <div className="card min-w-0 p-3">
          <div className="label">置信度</div>
          <div className="mt-0.5 font-mono text-[15px]">{rate(diff.confidence)}</div>
        </div>
        <div className="card min-w-0 p-3">
          <div className="label">关联行</div>
          <div className="mt-0.5 font-mono text-[15px]">{diff.related_lines?.length ?? 0}</div>
        </div>
        <div className="card min-w-0 p-3">
          <div className="label">证据</div>
          <div className="mt-0.5 font-mono text-[15px]">{diff.evidence?.length ?? 0}</div>
        </div>
      </div>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">字段级差异</h2>
          <span className="text-[11px] text-slate-400">我方值 / 对方值 / 差异 / 原因码</span>
        </div>
        <div className="px-3 pt-2">
          <p className="break-words rounded-lg bg-slate-50 px-2.5 py-2 text-[11px] leading-5 text-slate-500">
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

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">证据下钻</h2>
          <span className="text-[11px] text-slate-400">原始单据坐标</span>
        </div>
        <EvidenceGroup evidences={diff.evidence ?? []} empty="该差异未附带证据坐标，不能自动处置，需转人工核查。" />
      </section>

      {diff.draft ? (
        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">处置草稿</h2>
            <span className="badge bg-slate-100 text-slate-600 ring-slate-200">{diff.draft.status}</span>
          </div>
          <dl className="grid grid-cols-1 gap-x-4 gap-y-2 px-3 py-3 text-[12px] sm:grid-cols-2">
            <div className="min-w-0"><dt className="label">草稿号</dt><dd className="mt-0.5 break-all font-mono">{diff.draft.draft_no}</dd></div>
            <div className="min-w-0"><dt className="label">差异类型</dt><dd className="mt-0.5">{diffTypeMeta(diff.draft.diff_type).label}</dd></div>
            <div className="min-w-0"><dt className="label">金额</dt><dd className="mt-0.5 font-mono">{money(diff.draft.amount)}</dd></div>
            <div className="min-w-0"><dt className="label">责任方 / 置信度</dt><dd className="mt-0.5">{diff.draft.owner} · {rate(diff.draft.confidence)}</dd></div>
            <div className="min-w-0 sm:col-span-2"><dt className="label">建议</dt><dd className="mt-0.5 break-words">{diff.draft.suggestion}</dd></div>
            <div className="min-w-0 sm:col-span-2"><dt className="label">引用</dt><dd className="mt-0.5 break-all font-mono text-[11px]">{(diff.draft.refs ?? []).join('、') || '—'}</dd></div>
            <div className="min-w-0 sm:col-span-2"><dt className="label">需人工</dt><dd className="mt-0.5">{diff.draft.needs_manual ? '是' : '否'}</dd></div>
          </dl>
        </section>
      ) : null}

      {diff.unattributed ? (
        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">未归因金额</h2>
            <span className="font-mono text-[12px] text-rose-700">{money(diff.unattributed.amount_diff)}</span>
          </div>
          <p className="break-words px-3 py-2 text-[12px] text-slate-700">{diff.unattributed.reason}</p>
          <p className="break-all px-3 pb-2 text-[11px] text-slate-400">ref: {diff.unattributed.ref}</p>
          <EvidenceGroup evidences={diff.unattributed.evidence ?? []} empty="未归因金额没有证据坐标。" />
        </section>
      ) : null}
    </div>
  );
}
