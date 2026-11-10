import { useMemo, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Link, useNavigate } from 'react-router-dom';
import { api, type ReconRunPayload } from '../api/client';
import type { ReconDatasetIn } from '../api/generated/contract';
import { useApiConfig } from '../api/config';
import { ErrorPanel } from '../components/ErrorPanel';
import { dateTime, shortId } from '../lib/format';

const RECENT_KEY = 'smart-erp.web.recon-batches.v1';

interface RecentBatch {
  batch_id: string;
  suppliers: string[];
  created_at: string;
}

function readRecent(): RecentBatch[] {
  try {
    const raw = localStorage.getItem(RECENT_KEY);
    return raw ? (JSON.parse(raw) as RecentBatch[]) : [];
  } catch {
    return [];
  }
}

function writeRecent(next: RecentBatch[]): void {
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next.slice(0, 8)));
  } catch {
    // 忽略隐私模式下的存储失败
  }
}

export function ReconPage() {
  const config = useApiConfig();
  const navigate = useNavigate();
  const [suppliers, setSuppliers] = useState('');
  const [erp, setErp] = useState(true);
  const [mes, setMes] = useState(false);
  const [wms, setWms] = useState(false);
  const [statementFiles, setStatementFiles] = useState('');
  const [datasetText, setDatasetText] = useState('');
  const [batchId, setBatchId] = useState('');
  const [recent, setRecent] = useState<RecentBatch[]>(() => readRecent());
  const [formError, setFormError] = useState<string | null>(null);

  const supplierList = useMemo(
    () => suppliers.split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean),
    [suppliers],
  );

  const mutation = useMutation({
    mutationFn: (payload: ReconRunPayload) => api.createReconRun(payload),
    onSuccess: (batch) => {
      const entry: RecentBatch = { batch_id: batch.batch_id, suppliers: batch.suppliers, created_at: batch.created_at };
      const next = [entry, ...readRecent().filter((item) => item.batch_id !== batch.batch_id)];
      writeRecent(next);
      setRecent(next);
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
          发起真实 /v1/recon/runs，或打开已有批次查看状态与差异。scope：{config.scope.org_id} · {config.scope.period} · {config.scope.system_ids.join(',')}
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
            {mutation.error ? <ErrorPanel error={mutation.error} compact /> : null}

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
              <h2 className="section-title">打开已有批次</h2>
            </div>
            <div className="card-body space-y-2">
              <label className="block min-w-0">
                <span className="label">batch_id</span>
                <input className="input mt-1" value={batchId} onChange={(event) => setBatchId(event.target.value)} placeholder="UUID" />
              </label>
              <button
                type="button"
                className="btn btn-secondary w-full"
                disabled={!batchId.trim()}
                onClick={() => navigate(`/recon/${batchId.trim()}`)}
              >
                查看批次状态
              </button>
              <p className="text-[11px] leading-5 text-slate-500">
                C16 没有「列出全部批次」接口，因此这里只记录本浏览器最近发起的批次；跨设备请粘贴 batch_id。
              </p>
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">本机最近批次</h2>
              <span className="text-[11px] text-slate-400">{recent.length} 条</span>
            </div>
            {recent.length === 0 ? (
              <p className="px-3 py-6 text-center text-[12px] text-slate-500">还没有发起过对账。</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {recent.map((item) => (
                  <li key={item.batch_id} className="min-w-0 px-3 py-2">
                    <Link to={`/recon/${item.batch_id}`} className="block min-w-0 rounded-lg px-1 py-1 hover:bg-slate-50">
                      <div className="break-all font-mono text-[11px] text-slate-600">{shortId(item.batch_id)}</div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                        <span className="min-w-0 truncate">{item.suppliers.join('、') || '—'}</span>
                        <span className="ml-auto shrink-0">{dateTime(item.created_at)}</span>
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
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
