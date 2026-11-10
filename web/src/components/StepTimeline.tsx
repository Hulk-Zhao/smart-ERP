import type { AuditImageOut, TicketStepOut } from '../api/types';
import { dateTime, shortHash, text } from '../lib/format';
import { LevelBadge, StepStateBadge } from './Badges';
import { EmptyState } from './EmptyState';
import { AuditFieldDiff } from './FieldDiffTable';

const COMPENSATION_STATES = new Set(['compensating', 'compensated', 'unresolved', 'rolled_back']);

function isCompensation(step: TicketStepOut, images: AuditImageOut[]): boolean {
  return COMPENSATION_STATES.has(step.state) || images.some((image) => /rollback|compensat|manual_task/i.test(image.object_type));
}

/**
 * 工单执行时间线：每一步都展示前像/后像、字段差异与补偿记录。
 * 前像/后像来自审计影像（AuditImageOut.before_ref / after_ref），缺失时明确标注，不做猜测。
 */
export function StepTimeline({ steps, images }: { steps: TicketStepOut[]; images: AuditImageOut[] }) {
  if (steps.length === 0) {
    return (
      <EmptyState
        variant="no-data"
        compact
        title="该工单还没有执行步骤"
        description="本期确实无数据：审批通过后，网关会逐步落库；每一步的前像/后像由审计影像回传。"
      />
    );
  }

  const sorted = [...steps].sort((left, right) => left.step_no - right.step_no);

  return (
    <ol className="min-w-0 space-y-3 px-3 py-3">
      {sorted.map((step) => {
        const stepImages = images.filter((image) => image.step_id === step.step_id);
        const compensation = isCompensation(step, stepImages);
        return (
          <li key={step.step_id} className="min-w-0 rounded-xl border border-slate-200 bg-white">
            <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-3 py-2">
              <span className="font-mono text-[11px] text-slate-400">#{String(step.step_no).padStart(2, '0')}</span>
              <span className="text-[13px] font-semibold text-slate-900">{step.action_key}</span>
              <StepStateBadge state={step.state} />
              <LevelBadge level={step.level} />
              {compensation ? (
                <span className="badge bg-orange-50 text-orange-700 ring-orange-200">补偿记录</span>
              ) : null}
              <span className="ml-auto text-[11px] text-slate-500">{dateTime(step.executed_at)}</span>
            </div>

            <div className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-1 px-3 py-2 text-[11px] text-slate-500 sm:grid-cols-2">
              <div className="min-w-0 break-all">系统 / 对象：{step.system} · {step.object_type}:{step.object_id}</div>
              <div className="min-w-0 break-all">幂等键：<span className="font-mono">{step.idem_key}</span></div>
              <div className="min-w-0 break-all">步骤 ID：<span className="font-mono">{step.step_id}</span></div>
              <div className="min-w-0 break-all">执行时刻：{dateTime(step.executed_at)}</div>
            </div>

            {stepImages.length === 0 ? (
              <div className="mx-3 mb-3 rounded-lg border border-dashed border-slate-200 px-3 py-2 text-[11px] text-slate-500">
                审计影像未回传（该步骤没有前像/后像记录）。
              </div>
            ) : (
              <div className="space-y-2 px-3 pb-3">
                {stepImages.map((image) => (
                  <div key={image.image_id} className="min-w-0 rounded-lg border border-slate-100 bg-slate-50/60">
                    <div className="grid min-w-0 grid-cols-1 gap-2 px-3 py-2 sm:grid-cols-2">
                      <div className="min-w-0">
                        <div className="label">前像 before</div>
                        <div className="break-all font-mono text-[11px] text-slate-700">{image.before_ref ?? '缺失'}</div>
                      </div>
                      <div className="min-w-0">
                        <div className="label">后像 after</div>
                        <div className="break-all font-mono text-[11px] text-slate-700">{image.after_ref ?? '缺失'}</div>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 px-3 py-1.5 text-[10px] text-slate-500">
                      <span className="font-mono">{image.system} · {image.object_type}:{image.object_id}</span>
                      <span>hash: <span className="font-mono">{shortHash(image.hash, 10)}</span></span>
                      <span>prev: <span className="font-mono">{shortHash(image.prev_hash, 10)}</span></span>
                      {image.diff_missing ? (
                        <span className="badge bg-amber-50 text-amber-700 ring-amber-200">field_diff 缺失</span>
                      ) : null}
                    </div>

                    {image.field_diff && image.field_diff.length > 0 ? (
                      <div className="border-t border-slate-100">
                        <AuditFieldDiff entries={image.field_diff} />
                      </div>
                    ) : (
                      <div className="border-t border-slate-100 px-3 py-2 text-[11px] text-slate-500">
                        {image.diff_missing ? '原系统未返回字段差异，需人工核对前后影像。' : '该步骤没有字段变化。'}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {compensation ? (
              <div className="mx-3 mb-3 rounded-lg border border-orange-200 bg-orange-50 px-3 py-2 text-[11px] text-orange-800">
                补偿/回滚记录：该步骤已进入补偿链路，回滚结果见审计影像与服务端编排日志；未确认前不要重复发起。
              </div>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

export function EvidenceRef({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="min-w-0">
      <div className="label">{label}</div>
      <div className="break-all font-mono text-[11px] text-slate-700">{text(value, '缺失')}</div>
    </div>
  );
}
