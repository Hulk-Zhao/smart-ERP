import { useMemo, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import {
  isApiError,
  isNetworkFailure,
  isRateLimited,
  isTimeoutFailure,
} from '../api/errors';
import { useSession } from '../api/session';
import { dateTime, moneyText } from '../lib/format';
import { IconAlert, IconClose, IconSparkles } from './icons';

interface AssistantPanelProps {
  onClose: () => void;
}

/** 后端 LLM 硬超时 8s；前端留出网络余量，超时要与「规则降级」分开报。 */
const ASSISTANT_TIMEOUT_MS = 12_000;

const FALLBACK_REASONS: Record<string, string> = {
  llm_not_configured: '未配置模型（缺少 base_url / api_key / model）',
  llm_timeout: '模型调用超时（后端已降级）',
  llm_connect_failed: '无法连接模型服务',
  llm_error: '模型调用异常',
  llm_output_invalid: '模型输出不可解析',
};

function fallbackReasonLabel(reason: string | null | undefined): string {
  if (!reason) return '未说明原因';
  const http = /^llm_http_(\d{3})$/.exec(reason);
  if (http) return `模型服务返回 HTTP ${http[1]}`;
  return FALLBACK_REASONS[reason] ?? reason;
}

interface SuggestionErrorCopy {
  kind: string;
  title: string;
  detail: string;
}

function suggestionErrorCopy(error: unknown): SuggestionErrorCopy {
  if (isRateLimited(error)) {
    return {
      kind: 'rate-limit',
      title: '助手请求过于频繁',
      detail: '每租户每分钟有调用上限。请稍后重试；这不影响页面其它数据。',
    };
  }
  if (isTimeoutFailure(error)) {
    return {
      kind: 'timeout',
      title: '等待助手响应超时',
      detail: `前端等待超过 ${ASSISTANT_TIMEOUT_MS / 1000} 秒。后端模型超时会降级为规则建议并正常返回；这里是请求没有回来。`,
    };
  }
  if (isNetworkFailure(error)) {
    return {
      kind: 'network',
      title: '无法连接助手服务',
      detail: '请检查网络、baseUrl 与后端进程。未拿到建议，不展示推测内容。',
    };
  }
  if (isApiError(error)) {
    return {
      kind: 'api',
      title: `${error.status} · ${error.code}`,
      detail: error.detail,
    };
  }
  return {
    kind: 'unknown',
    title: '助手建议读取失败',
    detail: '请重试；仍失败时查看浏览器控制台。',
  };
}

/**
 * 右侧助手栏。
 *
 * 待审批计数来自 `GET /v1/approvals/pending`（真实接口）；
 * 工单详情页的「下一步」来自 `POST /v1/assistant/suggest`，只展示接口返回的内容：
 * - `source="llm"`：显示模型名与生成时间；
 * - `source="rule"`：明确标注「规则建议（模型未参与）」并给出 fallback_reason，不显示模型名。
 * 降级时接口仍是 200，因此这里只按 `source` 判断，绝不按状态码猜测。
 */
export function AssistantPanel({ onClose }: AssistantPanelProps) {
  const location = useLocation();
  const session = useSession();
  const [question, setQuestion] = useState('');
  const [asked, setAsked] = useState<string | null>(null);

  const ticketMatch = /^\/tickets\/([^/]+)\/?$/.exec(location.pathname);
  const ticketId = ticketMatch ? decodeURIComponent(ticketMatch[1]) : '';

  const approvals = useQuery({
    queryKey: ['assistant', 'pending-approvals'],
    queryFn: () => api.listPendingApprovals({ limit: 50 }),
    // 与其它页面同一条规矩：身份/范围没就绪前不发请求，避免把占位 org 打到后端。
    enabled: session.canQuery,
    staleTime: 10_000,
  });

  const suggestion = useQuery({
    queryKey: ['assistant', 'suggest', ticketId],
    queryFn: () => api.suggestNextAction(ticketId, { timeoutMs: ASSISTANT_TIMEOUT_MS }),
    enabled: session.canQuery && Boolean(ticketId),
    // 限流时自动重试会继续消耗配额，交给用户手动重试。
    retry: false,
    staleTime: 30_000,
  });

  const stats = useMemo(() => {
    const items = approvals.data?.items ?? [];
    return {
      total: items.length,
      touchesFund: items.filter((item) => item.touches_fund).length,
      w3: items.filter((item) => item.write_level === 'W3').length,
      missingRole: items.filter((item) => item.approval_missing_roles.length > 0).length,
    };
  }, [approvals.data]);

  const errorCopy = suggestion.isError ? suggestionErrorCopy(suggestion.error) : null;

  const content = (
    <div className="flex h-full min-h-0 flex-col bg-white">
      <div className="flex items-start justify-between gap-2 border-b border-slate-200 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600">
              <IconSparkles />
            </span>
            <h2 className="text-[13px] font-semibold text-slate-900">助手</h2>
            <span className="badge bg-slate-100 text-slate-500 ring-slate-200">建议</span>
          </div>
          <p className="mt-1 text-[11px] leading-4 text-slate-500">
            来源：/v1/assistant/suggest；规则降级会明确标注，不冒充模型。
          </p>
        </div>
        <button type="button" className="btn btn-ghost btn-sm shrink-0" onClick={onClose} aria-label="收起助手">
          <IconClose />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        <div className="rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2.5">
          <div className="flex items-center justify-between text-[11px] text-slate-500">
            <span title="口径：全局审批待办，不是本单指标">待审批（全局队列）</span>
            <span>{approvals.isFetching ? '读取中…' : approvals.isError ? '不可用' : '实时'}</span>
          </div>
          {approvals.isError ? (
            <div className="mt-2 flex items-start gap-1.5 text-[11px] text-amber-700">
              <IconAlert className="mt-0.5 shrink-0" />
              待审批数量读取失败，助手不展示推测值。
            </div>
          ) : (
            <div className="mt-1 flex items-baseline gap-1.5">
              <span className="font-mono text-[24px] font-semibold leading-7 text-slate-900 num">
                {approvals.isLoading ? '—' : stats.total}
              </span>
              <span className="text-[11px] text-slate-500">条</span>
              <span className="ml-auto flex flex-wrap justify-end gap-1">
                <span
                  className="badge bg-rose-50 text-rose-700 ring-rose-200"
                  title="仅统计待你决策的 W3 工单；工作台「W3 禁止自动执行」是当前页工单口径"
                >
                  W3 {stats.w3}
                </span>
                <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉资金 {stats.touchesFund}</span>
                <span className="badge bg-slate-100 text-slate-600 ring-slate-200">缺角色 {stats.missingRole}</span>
              </span>
            </div>
          )}
        </div>

        <div className="mt-3">
          <div className="flex items-center justify-between px-1">
            <span className="eyebrow">下一步</span>
            <span className="text-[10px] text-slate-400">后端建议接口</span>
          </div>

          {!ticketId ? (
            <div
              className="mt-1.5 rounded-xl border border-slate-200 bg-white p-2.5 text-[11px] leading-[17px] text-slate-500"
              data-testid="assistant-no-ticket"
            >
              打开一张工单详情，助手会调用 <span className="font-mono">/v1/assistant/suggest</span> 生成下一步建议。
              本页不再用规则拼建议。
            </div>
          ) : suggestion.isPending ? (
            <div
              className="mt-1.5 rounded-xl border border-slate-200 bg-white p-2.5 text-[11px] text-slate-500"
              data-testid="assistant-loading"
            >
              正在生成建议…
            </div>
          ) : errorCopy ? (
            <div
              className="mt-1.5 rounded-xl border border-amber-200 bg-amber-50/70 p-2.5 text-[11px] leading-[17px] text-amber-900"
              data-testid="assistant-error"
              data-kind={errorCopy.kind}
            >
              <div className="font-medium">{errorCopy.title}</div>
              <p className="mt-0.5">{errorCopy.detail}</p>
              <button
                type="button"
                className="btn btn-secondary btn-sm mt-1.5"
                onClick={() => suggestion.refetch()}
                data-testid="assistant-retry"
              >
                重试
              </button>
            </div>
          ) : suggestion.data ? (
            <div
              className="mt-1.5 rounded-xl border border-slate-200 bg-white p-2.5"
              data-testid="assistant-suggest"
              data-source={suggestion.data.source}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                {suggestion.data.source === 'llm' ? (
                  <span
                    className="badge bg-indigo-50 text-indigo-700 ring-indigo-200"
                    data-testid="assistant-source"
                    data-source="llm"
                  >
                    模型建议
                  </span>
                ) : (
                  <span
                    className="badge bg-amber-50 text-amber-800 ring-amber-200"
                    data-testid="assistant-source"
                    data-source="rule"
                  >
                    规则建议（模型未参与）
                  </span>
                )}
                <span className="text-[10px] text-slate-400">
                  生成于 <span data-testid="assistant-generated-at">{dateTime(suggestion.data.generated_at)}</span>
                </span>
              </div>

              {suggestion.data.source === 'rule' ? (
                <p className="mt-1.5 text-[11px] leading-[17px] text-amber-800" data-testid="assistant-fallback-reason">
                  降级原因：{fallbackReasonLabel(suggestion.data.fallback_reason)}
                </p>
              ) : (
                <p className="mt-1.5 text-[11px] text-slate-500">
                  模型 <span className="font-mono" data-testid="assistant-model">{suggestion.data.model || '—'}</span>
                </p>
              )}

              <p className="mt-1.5 text-[12px] font-medium leading-5 text-slate-900" data-testid="assistant-suggestion">
                {moneyText(suggestion.data.suggestion)}
              </p>
              {suggestion.data.reasons.length > 0 ? (
                <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[11px] leading-[17px] text-slate-500">
                  {suggestion.data.reasons.map((reason) => (
                    <li key={reason}>
                      {moneyText(reason.replace(/^写等级：/, '等级：'))}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="mt-3">
          <div className="eyebrow px-1">问一句</div>
          <form
            className="mt-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              if (!question.trim()) return;
              setAsked(question.trim());
              setQuestion('');
            }}
          >
            <div className="flex gap-1.5">
              <input
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                className="input h-8 py-1 text-[12px]"
                placeholder="例如：59 为什么等于 59？"
                aria-label="向助手提问"
              />
              <button type="submit" className="btn btn-primary btn-sm shrink-0" disabled={!question.trim()}>
                发送
              </button>
            </div>
          </form>
          {asked ? (
            <div className="mt-2 rounded-xl border border-amber-200 bg-amber-50/70 p-2.5 text-[11px] leading-[17px] text-amber-900">
              <div className="font-medium">「{asked}」</div>
              <p className="mt-1">
                问答服务尚未纳入 C16 契约，本轮不生成业务结论。可以先用下方入口核对；接入后此处会返回带来源的回答。
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <Link to="/approvals" className="btn btn-secondary btn-sm">
                  审批队列
                </Link>
                <Link to="/recon" className="btn btn-secondary btn-sm">
                  对账批次
                </Link>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="border-t border-slate-200 px-4 py-2 text-[10px] leading-4 text-slate-400">
        {ticketId
          ? '建议由后端接口生成；source=rule 时会标注降级原因，不显示模型名。'
          : '助手只在工单详情页生成下一步建议，其它页面不展示推测。'}
      </div>
    </div>
  );

  return (
    <>
      <aside className="sticky top-0 hidden h-screen w-[var(--assistant-w)] shrink-0 border-l border-slate-200 xl:block">
        {content}
      </aside>
      <div className="fixed inset-0 z-50 xl:hidden" role="dialog" aria-modal="true" aria-label="助手">
        <div className="drawer-overlay" onClick={onClose} />
        <div className="drawer-panel h-full max-w-[380px]">{content}</div>
      </div>
    </>
  );
}
