import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, downloadText, type TicketAction, type TicketActionPayload } from '../api/client';
import { useApiConfig, type ApprovalRole } from '../api/config';
import { LevelBadge, StateBadge } from '../components/Badges';
import { ApprovalOpinions } from '../components/ApprovalOpinions';
import { ErrorPanel } from '../components/ErrorPanel';
import { StateRail } from '../components/StateRail';
import { StepTimeline } from '../components/StepTimeline';
import { amount, dateTime, shortHash, shortId } from '../lib/format';
import { openSettingsPanel } from '../lib/settings';

const ACTION_LABELS: Record<TicketAction, string> = {
  approve: '审批通过并执行',
  reject: '驳回',
  cancel: '终止工单',
  compensate: '触发补偿 / 回滚',
};

const ACTION_CONFIRM: Record<TicketAction, string> = {
  approve: '确认审批通过',
  reject: '确认驳回',
  cancel: '确认终止',
  compensate: '确认补偿',
};

type ActionFailCode = NonNullable<TicketActionPayload['fail_code']>;

const FAIL_CODES: ActionFailCode[] = ['F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7'];

function availableActions(state: string): TicketAction[] {
  if (state === '待审批') return ['approve', 'reject', 'cancel'];
  if (state === '计划中' || state === '需人工处置') return ['cancel'];
  if (state === '执行中' || state === '补偿中') return ['compensate'];
  return [];
}

export function TicketDetailPage() {
  const { ticketId = '' } = useParams();
  const config = useApiConfig();
  const queryClient = useQueryClient();
  const [activeAction, setActiveAction] = useState<TicketAction | null>(null);
  const [comment, setComment] = useState('');
  const [role, setRole] = useState<ApprovalRole>(config.roles[0] ?? 'reviewer');
  const [failCode, setFailCode] = useState<ActionFailCode>('F2');
  const [confirmedManual, setConfirmedManual] = useState(false);
  const [exportState, setExportState] = useState<{ status: 'idle' | 'loading' | 'ok' | 'error'; message?: string; error?: unknown }>({ status: 'idle' });

  const ticket = useQuery({
    queryKey: ['ticket', ticketId],
    queryFn: () => api.getTicket(ticketId),
    enabled: Boolean(ticketId),
  });
  const images = useQuery({
    queryKey: ['ticket', ticketId, 'images'],
    queryFn: () => api.listAuditImages(ticketId, { limit: 100 }),
    enabled: Boolean(ticketId),
  });
  const chain = useQuery({
    queryKey: ['ticket', ticketId, 'chain'],
    queryFn: () => api.getAuditChain(ticketId),
    enabled: Boolean(ticketId),
  });

  const mutation = useMutation({
    mutationFn: ({ action, payload }: { action: TicketAction; payload: TicketActionPayload }) =>
      api.ticketAction(ticketId, action, payload),
    onSuccess: () => {
      setActiveAction(null);
      setComment('');
      setConfirmedManual(false);
      void queryClient.invalidateQueries({ queryKey: ['ticket', ticketId] });
      void queryClient.invalidateQueries({ queryKey: ['tickets'] });
      void queryClient.invalidateQueries({ queryKey: ['approvals'] });
    },
  });

  const submitAction = () => {
    if (!activeAction) return;
    const payload: TicketActionPayload = {
      comment: comment.trim(),
      role: activeAction === 'reject' || activeAction === 'approve' ? role : null,
      fail_code: activeAction === 'compensate' ? failCode : null,
      confirmed_manual: activeAction === 'compensate' ? confirmedManual : false,
    };
    mutation.mutate({ action: activeAction, payload });
  };

  const exportAudit = async () => {
    setExportState({ status: 'loading' });
    try {
      const result = await api.exportAuditCsv(ticketId);
      downloadText(result.filename, result.csv);
      setExportState({
        status: 'ok',
        message: `已导出 ${result.rowCount} 行 · ${result.filename}${result.sha256 ? ` · ${result.sha256}` : ''}`,
      });
    } catch (error) {
      setExportState({ status: 'error', error });
    }
  };

  if (!ticketId) {
    return <ErrorPanel error={{ status: 422, code: 'E-DATA-MISSING', detail: '缺少工单 ID' }} />;
  }

  if (ticket.isLoading) {
    return (
      <div className="space-y-2">
        {[0, 1, 2].map((key) => <div key={key} className="skeleton h-28 w-full" />)}
      </div>
    );
  }

  if (ticket.error) {
    return (
      <ErrorPanel
        error={ticket.error}
        onRetry={() => void ticket.refetch()}
        onOpenSettings={openSettingsPanel}
        notFoundKind="ticket"
        backTo={{ to: '/tickets', label: '← 返回工单列表' }}
      />
    );
  }

  const detail = ticket.data!;
  const actions = availableActions(detail.state);
  const imagesList = images.data?.items ?? [];
  const auditChain = chain.data;
  const dirtyDiffMissing = imagesList.some((item) => item.diff_missing);
  const requiresComment = activeAction === 'reject' || activeAction === 'cancel';
  const canConfirm =
    Boolean(activeAction) &&
    (!requiresComment || comment.trim().length > 0) &&
    (activeAction !== 'compensate' || confirmedManual);

  return (
    <div className="min-w-0 space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <Link to="/tickets" className="link text-[12px]">← 返回工单列表</Link>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
            <h1 className="text-[18px] font-semibold text-slate-900">{detail.ticket_no}</h1>
            <StateBadge state={detail.state} />
            <LevelBadge level={detail.write_level} />
            {detail.touches_fund ? <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉及资金</span> : null}
          </div>
          <p className="mt-1 break-words text-[12px] text-slate-500">{detail.business_key} · {detail.scene_code} · v{detail.version}</p>
        </div>
        <button type="button" className="btn btn-secondary btn-sm" onClick={() => { void ticket.refetch(); void images.refetch(); void chain.refetch(); }}>
          刷新
        </button>
      </div>

      {detail.write_level === 'W3' ? (
        <div className="rounded-xl border border-rose-300 bg-rose-50 px-3 py-2 text-[12px] text-rose-800">
          <strong className="font-semibold">W3 工单</strong>：网关禁止自动执行，任何审批/执行请求都会被整单拒绝，连接器零写入。请在外部系统完成操作后走人工留痕。
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.6fr_1fr]">
        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">工单信息</h2>
            <span className="text-[11px] text-slate-400">更新 {dateTime(detail.updated_at)}</span>
          </div>
          <dl className="grid grid-cols-1 gap-x-4 gap-y-2 px-3 py-3 text-[12px] sm:grid-cols-2">
            <div className="min-w-0"><dt className="label">金额</dt><dd className="mt-0.5 font-mono">{amount(detail.amount)}</dd></div>
            <div className="min-w-0"><dt className="label">发起人</dt><dd className="mt-0.5 break-all">{shortId(detail.initiator_id)}</dd></div>
            <div className="min-w-0"><dt className="label">系统</dt><dd className="mt-0.5 break-words">{detail.systems.join(' / ')}</dd></div>
            <div className="min-w-0"><dt className="label">scope</dt><dd className="mt-0.5 break-all">{detail.org_id} · {detail.period}</dd></div>
            <div className="min-w-0"><dt className="label">创建时间</dt><dd className="mt-0.5">{dateTime(detail.created_at)}</dd></div>
            <div className="min-w-0"><dt className="label">审计影像</dt><dd className="mt-0.5">{detail.audit_image_count} 张</dd></div>
          </dl>
        </section>

        <section className="card min-w-0">
          <div className="card-head">
            <h2 className="section-title">审批进度</h2>
            <span className="text-[11px] text-slate-400">缺 {detail.missing_roles.length} 个角色</span>
          </div>
          <div className="card-body space-y-2">
            <div className="flex flex-wrap gap-1">
              {detail.required_roles.length === 0 ? <span className="text-[12px] text-slate-500">无需审批角色</span> : null}
              {detail.required_roles.map((item) => (
                <span key={item} className={`badge ${detail.missing_roles.includes(item) ? 'bg-amber-50 text-amber-700 ring-amber-200' : 'bg-emerald-50 text-emerald-700 ring-emerald-200'}`}>
                  {item} · {detail.missing_roles.includes(item) ? '待提交' : '已满足'}
                </span>
              ))}
            </div>
            <p className="text-[11px] leading-5 text-slate-500">
              审批意见在下方按当前身份脱敏展示；发起人看不到审批意见正文。
            </p>
          </div>
        </section>
      </div>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">可执行动作</h2>
          <span className="text-[11px] text-slate-400">写接口自动携带 Idempotency-Key</span>
        </div>
        <div className="card-body space-y-3">
          {actions.length === 0 ? (
            <p className="text-[12px] text-slate-500">当前状态「{detail.state}」没有可执行动作（终态或需人工处置）。</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {actions.map((action) => (
                <button
                  key={action}
                  type="button"
                  className={`btn btn-sm ${action === 'approve' ? 'btn-primary' : action === 'compensate' ? 'btn-danger' : 'btn-secondary'}`}
                  onClick={() => {
                    setActiveAction(action);
                    setComment('');
                    setConfirmedManual(false);
                    mutation.reset();
                  }}
                >
                  {ACTION_LABELS[action]}
                </button>
              ))}
            </div>
          )}

          {activeAction ? (
            <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
              <h3 className="text-[13px] font-semibold text-slate-900">{ACTION_LABELS[activeAction]}</h3>
              {activeAction === 'approve' && detail.write_level === 'W3' ? (
                <p className="mt-1 text-[11px] text-rose-700">该工单为 W3：提交后会收到 403，连接器不会产生任何写入。</p>
              ) : null}

              <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
                {activeAction === 'reject' || activeAction === 'approve' ? (
                  <label className="block min-w-0">
                    <span className="label">审批角色</span>
                    <select className="input mt-1" value={role} onChange={(event) => setRole(event.target.value as ApprovalRole)}>
                      {(config.roles.length > 0 ? config.roles : ['reviewer']).map((item) => (
                        <option key={item} value={item}>{item}</option>
                      ))}
                    </select>
                  </label>
                ) : null}

                {activeAction === 'compensate' ? (
                  <>
                    <label className="block min-w-0">
                      <span className="label">失败分类 fail_code</span>
                      <select className="input mt-1" value={failCode} onChange={(event) => setFailCode(event.target.value as ActionFailCode)}>
                        {FAIL_CODES.map((item) => <option key={item} value={item}>{item}</option>)}
                      </select>
                    </label>
                    <label className="flex items-center gap-2 text-[12px] text-slate-700">
                      <input type="checkbox" checked={confirmedManual} onChange={(event) => setConfirmedManual(event.target.checked)} />
                      我已确认需要人工介入并保留证据
                    </label>
                  </>
                ) : null}
              </div>

              <label className="mt-2 block min-w-0">
                <span className="label">
                  意见 / 原因 {requiresComment ? <span className="text-rose-600">（必填）</span> : '（可选，最多 2000 字）'}
                </span>
                <textarea
                  className="input mt-1 min-h-[72px] resize-y"
                  maxLength={2000}
                  value={comment}
                  onChange={(event) => setComment(event.target.value)}
                  placeholder={activeAction === 'cancel' ? '写清终止原因，便于审计追溯' : '意见仅对与你角色相关的审批人本人可见'}
                />
              </label>

              <div className="mt-2 flex flex-wrap justify-end gap-2">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setActiveAction(null)}>
                  取消
                </button>
                <button
                  type="button"
                  className={`btn btn-sm ${activeAction === 'compensate' ? 'btn-danger' : 'btn-primary'}`}
                  disabled={!canConfirm || mutation.isPending}
                  onClick={submitAction}
                >
                  {mutation.isPending ? '提交中…' : ACTION_CONFIRM[activeAction]}
                </button>
              </div>

              {mutation.error ? (
                <div className="mt-2">
                  <ErrorPanel error={mutation.error} onOpenSettings={openSettingsPanel} compact />
                </div>
              ) : null}
            </div>
          ) : null}

          {mutation.isSuccess ? (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[12px] text-emerald-800">
              动作已完成：{mutation.data.action} → {mutation.data.state}（v{mutation.data.version}）。
              {(mutation.data.executed_steps?.length ?? 0) > 0 ? ` 执行步骤 ${mutation.data.executed_steps?.length} 个。` : ''}
              {(mutation.data.manual_tasks?.length ?? 0) > 0 ? ` 人工任务 ${mutation.data.manual_tasks?.length} 个。` : ''}
            </div>
          ) : null}
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">状态机与时间线</h2>
          <span className="text-[11px] text-slate-400">前像 / 后像 / 补偿记录</span>
        </div>
        <div className="space-y-4 px-3 py-3">
          <StateRail state={detail.state} />
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h3 className="section-title">执行步骤</h3>
              {images.error ? <span className="text-[11px] text-rose-600">审计影像加载失败</span> : null}
            </div>
            {images.error ? (
              <ErrorPanel error={images.error} compact onRetry={() => void images.refetch()} onOpenSettings={openSettingsPanel} />
            ) : images.isLoading ? (
              <div className="space-y-2">{[0, 1].map((key) => <div key={key} className="skeleton h-16 w-full" />)}</div>
            ) : (
              <StepTimeline steps={detail.steps} images={imagesList} />
            )}
          </div>
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">双人复核</h2>
          <span className="text-[11px] text-slate-400">当前身份：{config.viewerName}</span>
        </div>
        <div className="card-body">
          <ApprovalOpinions
            approvals={detail.approvals}
            requiredRoles={detail.required_roles}
            viewerRole={config.roles[0] ?? null}
            viewerId={config.viewerId}
            initiatorId={detail.initiator_id}
          />
        </div>
      </section>

      <section className="card min-w-0">
        <div className="card-head">
          <h2 className="section-title">审计证据</h2>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void exportAudit()} disabled={exportState.status === 'loading'}>
            {exportState.status === 'loading' ? '导出中…' : '导出 CSV'}
          </button>
        </div>
        <div className="card-body space-y-3">
          {exportState.status === 'ok' ? (
            <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-800">{exportState.message}</div>
          ) : null}
          {exportState.status === 'error' ? <ErrorPanel error={exportState.error} compact /> : null}

          <div className="grid grid-cols-1 gap-2 text-[12px] sm:grid-cols-3">
            <div className="min-w-0 rounded-lg border border-slate-200 px-2.5 py-2">
              <div className="label">哈希链</div>
              <div className={`mt-0.5 font-medium ${auditChain?.ok ? 'text-emerald-700' : 'text-rose-700'}`}>
                {chain.isLoading ? '校验中…' : auditChain?.ok ? '完整' : '异常 / 未知'}
              </div>
              <div className="mt-0.5 text-[11px] text-slate-500">已校验 {auditChain?.checked ?? 0} 条</div>
            </div>
            <div className="min-w-0 rounded-lg border border-slate-200 px-2.5 py-2">
              <div className="label">链头 hash</div>
              <div className="mt-0.5 break-all font-mono text-[11px]">{shortHash(auditChain?.head_hash, 12)}</div>
            </div>
            <div className="min-w-0 rounded-lg border border-slate-200 px-2.5 py-2">
              <div className="label">影像 / 差异完整性</div>
              <div className="mt-0.5 font-medium">{imagesList.length} 张{dirtyDiffMissing ? ' · 有字段差异缺失' : ' · field_diff 齐全'}</div>
            </div>
          </div>

          {chain.error ? <ErrorPanel error={chain.error} compact onRetry={() => void chain.refetch()} onOpenSettings={openSettingsPanel} /> : null}
          {auditChain && !auditChain.ok ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-800">
              审计链校验失败：{auditChain.detail || '断点未说明'}（broken_at: {auditChain.broken_at ?? '—'}）
            </div>
          ) : null}

          {imagesList.length > 0 ? (
            <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
              {imagesList.map((image) => (
                <li key={image.image_id} className="min-w-0 px-3 py-2 text-[11px]">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-slate-600">{image.system} · {image.object_type}:{image.object_id}</span>
                    <span className="text-slate-400">{dateTime(image.created_at)}</span>
                    <span className="ml-auto break-all font-mono text-slate-500">hash {shortHash(image.hash, 8)}</span>
                  </div>
                  <div className="mt-1 grid grid-cols-1 gap-1 text-slate-500 sm:grid-cols-2">
                    <span className="break-all">前像：{image.before_ref ?? '缺失'}</span>
                    <span className="break-all">后像：{image.after_ref ?? '缺失'}</span>
                  </div>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </section>
    </div>
  );
}
