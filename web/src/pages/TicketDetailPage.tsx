import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { api, downloadText, type TicketAction, type TicketActionPayload } from '../api/client';
import { useApiConfig, type ApprovalRole } from '../api/config';
import { useSession } from '../api/session';
import { LevelBadge, StateBadge, StepStateBadge } from '../components/Badges';
import { ApprovalOpinions } from '../components/ApprovalOpinions';
import { ErrorPanel } from '../components/ErrorPanel';
import { AuditFieldDiff } from '../components/FieldDiffTable';
import { StateRail } from '../components/StateRail';
import { StepTimeline } from '../components/StepTimeline';
import { IconAlert, IconArrowRight, IconCheck } from '../components/icons';
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

// 关闭同步回滚时给用户的解释：不能只把按钮灰掉，否则「为什么点不了」会变成新的误导。
const ROLLBACK_DISABLED_REASON =
  '同步回滚入口已关闭（本期只读）：待补偿执行器路径上线后开放；点击不会产生任何外部写入。';

// W3 是系统级硬禁止，不是「暂时失败」：既然服务端一定整单拒绝，界面就不给可执行按钮。
const W3_DISABLED_REASON =
  'W3 系统级硬禁止：审批通过并执行会被服务端整单拒绝（403 E-POLICY-W3FORBIDDEN），连接器零写入；请在外部系统完成操作后走人工留痕。驳回 / 终止不触发外部写入，仍可使用。';

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
  const session = useSession();
  const queryClient = useQueryClient();
  const [activeAction, setActiveAction] = useState<TicketAction | null>(null);
  const [comment, setComment] = useState('');
  const [role, setRole] = useState<ApprovalRole>(config.roles[0] ?? 'reviewer');
  // 角色只来自会话端点；会话到达后若当前选择不在可用角色里，落到第一个可用角色。
  useEffect(() => {
    setRole((current) => (config.roles.includes(current) ? current : (config.roles[0] ?? 'reviewer')));
  }, [config.roles.join(',')]);
  const [failCode, setFailCode] = useState<ActionFailCode>('F2');
  const [confirmedManual, setConfirmedManual] = useState(false);
  const [expandedImage, setExpandedImage] = useState<string | null>(null);
  const [exportState, setExportState] = useState<{ status: 'idle' | 'loading' | 'ok' | 'error'; message?: string; error?: unknown }>({ status: 'idle' });

  const ticket = useQuery({
    queryKey: ['ticket', ticketId],
    queryFn: () => api.getTicket(ticketId),
    // 与设置面板同一套规则：范围未确认前不发业务请求，避免用占位 org 拿 403。
    enabled: session.canQuery && Boolean(ticketId),
  });
  const images = useQuery({
    queryKey: ['ticket', ticketId, 'images'],
    queryFn: () => api.listAuditImages(ticketId, { limit: 100 }),
    enabled: session.canQuery && Boolean(ticketId),
  });
  const chain = useQuery({
    queryKey: ['ticket', ticketId, 'chain'],
    queryFn: () => api.getAuditChain(ticketId),
    enabled: session.canQuery && Boolean(ticketId),
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

  if (ticket.isPending) {
    return (
      <div className="space-y-3">
        <div className="skeleton h-20 w-full" />
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_320px]">
          <div className="space-y-3">
            <div className="skeleton h-32 w-full" />
            <div className="skeleton h-56 w-full" />
          </div>
          <div className="skeleton h-64 w-full" />
        </div>
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
  // 动作回执里的写入步骤：审批/执行刚完成时详情可能还没刷新，先按回执如实展示。
  const executedSteps = [...(mutation.data?.executed_steps ?? [])].sort((left, right) => left.step_no - right.step_no);
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
      <section className="card min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
          <div className="min-w-0">
            <Link to="/tickets" className="link text-[11.5px]">
              ← 返回工单列表
            </Link>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <h1 className="font-mono text-[19px] font-semibold tracking-tight text-slate-900">{detail.ticket_no}</h1>
              <StateBadge state={detail.state} />
              <LevelBadge level={detail.write_level} />
              {detail.touches_fund ? (
                <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉及资金</span>
              ) : null}
            </div>
            <p className="mt-1 break-words text-[12px] text-slate-500">
              {detail.business_key} · {detail.scene_code} · v{detail.version}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden text-[10.5px] text-slate-400 sm:inline">更新 {dateTime(detail.updated_at)}</span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                void ticket.refetch();
                void images.refetch();
                void chain.refetch();
              }}
            >
              刷新
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 divide-x divide-y divide-slate-100 border-t border-slate-100 text-[12px] sm:grid-cols-4 sm:divide-y-0">
          {[
            { label: '金额', value: amount(detail.amount), mono: true, tone: '' },
            {
              label: '缺角色',
              value: `${detail.approval_missing_roles.length} 个`,
              mono: false,
              tone: detail.approval_missing_roles.length > 0 ? 'text-amber-700' : 'text-emerald-700',
            },
            { label: '审计影像', value: `${detail.audit_image_count} 张`, mono: false, tone: '' },
            { label: '系统', value: detail.systems.join(' / '), mono: false, tone: '' },
          ].map((item) => (
            <div key={item.label} className="min-w-0 px-4 py-2.5">
              <div className="label">{item.label}</div>
              <div className={`mt-0.5 truncate text-[13px] font-medium ${item.tone ?? 'text-slate-900'} ${item.mono ? 'font-mono num' : ''}`} title={item.value}>
                {item.value}
              </div>
            </div>
          ))}
        </div>
      </section>

      {detail.write_level === 'W3' ? (
        <div className="flex items-start gap-2 rounded-xl border border-rose-300 bg-rose-50 px-3 py-2.5 text-[12px] leading-5 text-rose-800">
          <IconAlert className="mt-0.5 shrink-0" />
          <span>
            <strong className="font-semibold">W3 工单</strong>：网关禁止自动执行，「审批通过并执行」会被整单拒绝（403），连接器零写入。
            驳回 / 终止不触发外部写入，仍可操作；外部操作请在目标系统完成后走人工留痕。
          </span>
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_330px]">
        <div className="min-w-0 space-y-4 xl:order-1">
          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">工单信息</h2>
              <span className="text-[10.5px] text-slate-400">创建 {dateTime(detail.created_at)}</span>
            </div>
            <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 px-4 py-3 text-[12px] sm:grid-cols-2">
              <div className="min-w-0">
                <dt className="label">金额</dt>
                <dd className="mt-0.5 font-mono text-[15px] font-medium num">{amount(detail.amount)}</dd>
              </div>
              <div className="min-w-0">
                <dt className="label">发起人</dt>
                <dd className="mt-0.5 break-all">{shortId(detail.initiator_id)}</dd>
              </div>
              <div className="min-w-0">
                <dt className="label">系统</dt>
                <dd className="mt-0.5 break-words">{detail.systems.join(' / ')}</dd>
              </div>
              <div className="min-w-0">
                <dt className="label">scope</dt>
                <dd className="mt-0.5 break-all">{detail.org_id} · {detail.period}</dd>
              </div>
              <div className="min-w-0">
                <dt className="label">审计影像</dt>
                <dd className="mt-0.5">{detail.audit_image_count} 张</dd>
              </div>
              <div className="min-w-0">
                <dt className="label">更新时间</dt>
                <dd className="mt-0.5">{dateTime(detail.updated_at)}</dd>
              </div>
            </dl>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">状态机与时间线</h2>
              <span className="text-[10.5px] text-slate-400">前像 / 后像 / 补偿记录</span>
            </div>
            <div className="space-y-4 px-4 py-3">
              <StateRail state={detail.state} />
              <div className="min-w-0">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <h3 className="section-title">执行步骤</h3>
                  {images.error ? <span className="text-[11px] text-rose-600">审计影像加载失败</span> : null}
                </div>
                {images.error ? (
                  <ErrorPanel error={images.error} compact onRetry={() => void images.refetch()} onOpenSettings={openSettingsPanel} />
                ) : images.isPending ? (
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
              <span className="text-[10.5px] text-slate-400">
                当前身份：
                {session.session
                  ? `${shortId(session.session.user_id)} · ${session.session.roles.join(' / ') || '无角色'}`
                  : config.viewerName}
              </span>
            </div>
            <div className="card-body">
              <ApprovalOpinions
                approvals={detail.approvals}
                requiredRoles={detail.approval_required_roles}
                viewerRole={config.roles[0] ?? null}
                viewerId={config.viewerId}
                initiatorId={detail.initiator_id}
              />
            </div>
          </section>

          <section className="card min-w-0" id="audit-evidence">
            <div className="card-head">
              <h2 className="section-title">审计证据</h2>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => void exportAudit()}
                disabled={exportState.status === 'loading'}
              >
                {exportState.status === 'loading' ? '导出中…' : '导出 CSV'}
              </button>
            </div>
            <div className="card-body space-y-3">
              {exportState.status === 'ok' ? (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-[11px] text-emerald-800">
                  {exportState.message}
                </div>
              ) : null}
              {exportState.status === 'error' ? <ErrorPanel error={exportState.error} compact /> : null}

              {chain.error ? <ErrorPanel error={chain.error} compact onRetry={() => void chain.refetch()} onOpenSettings={openSettingsPanel} /> : null}
              {auditChain && !auditChain.ok ? (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] text-rose-800">
                  审计链校验失败：{auditChain.detail || '断点未说明'}（broken_at: {auditChain.broken_at ?? '—'}）
                </div>
              ) : null}

              <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-2 text-[11px]">
                <span className={`status-dot ${auditChain?.ok ? 'bg-emerald-500' : 'bg-rose-500'}`} />
                <span className="text-slate-600" data-testid="audit-image-summary">
                  影像 {imagesList.length} 张 ·{' '}
                  {imagesList.length === 0
                    ? '本单没有外部写入，因此没有字段差异影像'
                    : dirtyDiffMissing
                      ? '有字段差异缺失'
                      : 'field_diff 齐全'}
                </span>
                <span className="ml-auto break-all font-mono text-[10px] text-slate-400">
                  head {imagesList.length === 0 ? '—（无影像，链头为空）' : shortHash(auditChain?.head_hash, 8)}
                </span>
              </div>

              {imagesList.length > 0 ? (
                <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
                  {imagesList.map((image) => (
                    <li key={image.image_id} className="min-w-0 px-3 py-2 text-[11px]" data-testid="audit-image-row">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-slate-600">
                          {image.system} · {image.object_type}:{image.object_id}
                        </span>
                        <span className="text-slate-400">{dateTime(image.created_at)}</span>
                        <span className="ml-auto break-all font-mono text-slate-500">hash {shortHash(image.hash, 8)}</span>
                        <button
                          type="button"
                          className="link text-[11px]"
                          aria-expanded={expandedImage === image.image_id}
                          onClick={() => setExpandedImage((current) => (current === image.image_id ? null : image.image_id))}
                        >
                          {expandedImage === image.image_id ? '收起' : '查看'}
                        </button>
                      </div>
                      <div className="mt-1 grid grid-cols-1 gap-1 text-slate-500 sm:grid-cols-2">
                        <span className="break-all">前像：{image.before_ref ?? '缺失'}</span>
                        <span className="break-all">后像：{image.after_ref ?? '缺失'}</span>
                      </div>
                      {expandedImage === image.image_id ? (
                        <div className="mt-2 space-y-2 rounded-lg border border-slate-200 bg-slate-50/70 p-2" data-testid="audit-image-detail">
                          <div className="grid grid-cols-1 gap-1 break-all font-mono text-[10px] sm:grid-cols-2">
                            <span>image_id：{image.image_id}</span>
                            <span>step_id：{image.step_id}</span>
                            <span className="sm:col-span-2">hash：{image.hash}</span>
                            <span className="sm:col-span-2">prev_hash：{image.prev_hash}</span>
                          </div>
                          {(image.field_diff ?? []).length > 0 ? (
                            <AuditFieldDiff entries={image.field_diff ?? []} />
                          ) : (
                            <p className="text-[10.5px] text-amber-700">
                              该影像没有 field_diff（契约返回空）；不猜测填充。
                            </p>
                          )}
                        </div>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          </section>
        </div>

        <aside className="min-w-0 space-y-3 xl:order-2 xl:sticky xl:top-[72px] xl:self-start">
          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">审批进度</h2>
              <span className={`badge ${detail.approval_missing_roles.length ? 'bg-amber-50 text-amber-700 ring-amber-200' : 'bg-emerald-50 text-emerald-700 ring-emerald-200'}`}>
                缺 {detail.approval_missing_roles.length} 个角色
              </span>
            </div>
            <div className="card-body space-y-2.5">
              <div className="space-y-1.5">
                {detail.approval_required_roles.length === 0 ? (
                  <span className="text-[12px] text-slate-500">无需审批角色</span>
                ) : null}
                {detail.write_level === 'W3' ? (
                  <p className="rounded-lg bg-amber-50 px-2.5 py-2 text-[10.5px] leading-4 text-amber-800">
                    W3 不适用审批角色，所以不会进入审批待办；本单只能转人工或终止，审批通过不会发生。
                  </p>
                ) : null}
                {detail.approval_required_roles.map((item) => {
                  const missing = detail.approval_missing_roles.includes(item);
                  return (
                    <div key={item} className="flex items-center gap-2 text-[12px]">
                      <span
                        className={`flex h-4 w-4 items-center justify-center rounded-full ${
                          missing ? 'border border-amber-300 bg-amber-50 text-amber-600' : 'bg-emerald-500 text-white'
                        }`}
                      >
                        {missing ? <span className="h-1.5 w-1.5 rounded-full bg-amber-400" /> : <IconCheck width={10} height={10} />}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-slate-700">{item}</span>
                      <span className={`text-[10.5px] ${missing ? 'text-amber-700' : 'text-emerald-700'}`}>
                        {missing ? '待提交' : '已满足'}
                      </span>
                    </div>
                  );
                })}
              </div>
              {detail.execution_required_roles.length > 0 ? (
                <p className="rounded-lg bg-sky-50 px-2.5 py-2 text-[10.5px] leading-4 text-sky-800">
                  执行要求：{detail.execution_required_roles.join('、')}
                  {detail.execution_missing_roles.length > 0
                    ? `；仍缺 ${detail.execution_missing_roles.join('、')}（B 级连接器强制双签，审批一票 ≠ 执行一票）。`
                    : '；已满足，可执行。'}
                </p>
              ) : null}
              <p className="rounded-lg bg-slate-50 px-2.5 py-2 text-[10.5px] leading-4 text-slate-500">
                审批意见按当前身份脱敏展示；发起人看不到审批意见正文。
              </p>
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">可执行动作</h2>
              <span className="text-[10px] text-slate-400">自动带 Idempotency-Key</span>
            </div>
            <div className="card-body space-y-2.5">
              {actions.length === 0 ? (
                <p className="text-[12px] leading-5 text-slate-500">
                  当前状态「{detail.state}」没有可执行动作（终态或需人工处置）。
                </p>
              ) : (
                <div className="grid gap-1.5">
                  {actions.map((action) => {
                    // 执行类动作（approve → WriteGateway）在 W3 上必定被整单拒绝：
                    // 这里禁用并给原因，而不是让用户点了才看到 403。
                    const w3Blocked = action === 'approve' && detail.write_level === 'W3';
                    const disabled = w3Blocked || (action === 'compensate' && !detail.rollback_available);
                    const primary = action === 'approve' && !w3Blocked;
                    return (
                      <button
                        key={action}
                        type="button"
                        className={`btn w-full justify-between ${
                          primary ? 'btn-primary' : action === 'compensate' ? 'btn-danger' : 'btn-secondary'
                        }`}
                        disabled={disabled}
                        title={w3Blocked ? W3_DISABLED_REASON : disabled ? ROLLBACK_DISABLED_REASON : undefined}
                        data-testid={w3Blocked ? 'w3-approve-blocked' : undefined}
                        onClick={() => {
                          setActiveAction(action);
                          setComment('');
                          setConfirmedManual(false);
                          mutation.reset();
                        }}
                      >
                        <span>{ACTION_LABELS[action]}</span>
                        <IconArrowRight width={13} height={13} />
                      </button>
                    );
                  })}
                </div>
              )}

              {detail.write_level === 'W3' && actions.includes('approve') ? (
                <p className="text-[10.5px] leading-4 text-rose-700" data-testid="w3-action-reason">
                  {W3_DISABLED_REASON}
                </p>
              ) : null}

              {actions.includes('compensate') && !detail.rollback_available ? (
                <p className="text-[10.5px] leading-4 text-amber-700">{ROLLBACK_DISABLED_REASON}</p>
              ) : null}

              {activeAction ? (
                <div className="rounded-xl border border-slate-200 bg-slate-50 p-2.5">
                  <h3 className="text-[12.5px] font-semibold text-slate-900">{ACTION_LABELS[activeAction]}</h3>

                  <div className="mt-2 grid grid-cols-1 gap-2">
                    {activeAction === 'reject' || activeAction === 'approve' ? (
                      <label className="block min-w-0">
                        <span className="label">审批角色</span>
                        <select className="input mt-1 h-8 py-1" value={role} onChange={(event) => setRole(event.target.value as ApprovalRole)}>
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
                          <select className="input mt-1 h-8 py-1" value={failCode} onChange={(event) => setFailCode(event.target.value as ActionFailCode)}>
                            {FAIL_CODES.map((item) => <option key={item} value={item}>{item}</option>)}
                          </select>
                        </label>
                        <label className="flex items-center gap-2 text-[11px] text-slate-700">
                          <input type="checkbox" checked={confirmedManual} onChange={(event) => setConfirmedManual(event.target.checked)} />
                          已确认需人工介入并保留证据
                        </label>
                      </>
                    ) : null}
                  </div>

                  <label className="mt-2 block min-w-0">
                    <span className="label">
                      意见 / 原因 {requiresComment ? <span className="text-rose-600">（必填）</span> : '（可选）'}
                    </span>
                    <textarea
                      className="input mt-1 min-h-[64px] resize-y text-[12px]"
                      maxLength={2000}
                      value={comment}
                      onChange={(event) => setComment(event.target.value)}
                      placeholder={activeAction === 'cancel' ? '写清终止原因，便于审计追溯' : '仅对相关审批人本人可见'}
                    />
                  </label>

                  <div className="mt-2 flex flex-wrap justify-end gap-1.5">
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
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-2 text-[11px] leading-4 text-emerald-800">
                  <div>
                    已完成：{mutation.data.action} → {mutation.data.state}（v{mutation.data.version}）。
                    {executedSteps.length > 0 ? ` 步骤 ${executedSteps.length} 个。` : ''}
                    {(mutation.data.manual_tasks?.length ?? 0) > 0 ? ` 人工任务 ${mutation.data.manual_tasks?.length} 个。` : ''}
                  </div>
                  {executedSteps.length > 0 ? (
                    <ol className="mt-2 space-y-1.5" data-testid="executed-steps">
                      {executedSteps.map((step) => (
                        <li
                          key={step.step_id}
                          data-testid="executed-step"
                          className="min-w-0 rounded-lg border border-emerald-200 bg-white px-2 py-1.5"
                        >
                          <div className="flex flex-wrap items-center gap-1.5">
                            <span className="font-mono text-[10px] text-slate-400">#{String(step.step_no).padStart(2, '0')}</span>
                            <span className="font-mono text-[11px] font-semibold text-slate-900">{step.action_key}</span>
                            <StepStateBadge state={step.state} />
                            <LevelBadge level={step.level} />
                          </div>
                          <div className="mt-1 break-all text-slate-600">
                            写入 {step.system} · {step.object_type}:{step.object_id}
                          </div>
                          <div className="break-all text-slate-500">
                            幂等键 <span className="font-mono">{step.idem_key}</span>
                          </div>
                        </li>
                      ))}
                    </ol>
                  ) : null}
                </div>
              ) : null}
            </div>
          </section>

          <section className="card min-w-0">
            <div className="card-head">
              <h2 className="section-title">审计链</h2>
              <span className={`badge ${auditChain?.ok ? 'bg-emerald-50 text-emerald-700 ring-emerald-200' : 'bg-rose-50 text-rose-700 ring-rose-200'}`}>
                {chain.isPending ? '校验中' : auditChain?.ok ? '完整' : '异常'}
              </span>
            </div>
            <div className="card-body space-y-1.5 text-[11px] text-slate-500">
              <div className="flex items-center justify-between gap-2">
                <span>已校验</span>
                <span className="font-mono text-slate-800 num">{auditChain?.checked ?? 0} 条</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span>链头</span>
                <span className="break-all font-mono text-[10px] text-slate-700">{shortHash(auditChain?.head_hash, 8)}</span>
              </div>
              {auditChain && auditChain.image_count === 0 ? (
                <p className="text-[10.5px] leading-4 text-slate-500">
                  本单没有执行影像，链头为空是正确语义，不是校验失败。
                </p>
              ) : null}
              <a
                href="#audit-evidence"
                className="link inline-flex items-center gap-1 pt-1 text-[11px]"
                data-testid="audit-chain-detail-link"
                onClick={(event) => {
                  const target = document.getElementById('audit-evidence');
                  if (target) {
                    event.preventDefault();
                    target.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
                  }
                }}
              >
                查看影像明细（{imagesList.length} 条）→
              </a>
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}
