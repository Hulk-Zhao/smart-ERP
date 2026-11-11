import { useMemo, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { useSession } from '../api/session';
import { IconAlert, IconArrowRight, IconClose, IconSparkles } from './icons';

interface AssistantPanelProps {
  onClose: () => void;
}

interface Suggestion {
  title: string;
  detail: string;
  to: string;
  cta: string;
}

/**
 * 右侧助手栏（交互壳）。
 *
 * 数据来源只有两处：
 * 1) `GET /v1/approvals/pending` 的真实返回（与审批页同一接口）；
 * 2) 契约里已写死的口径说明（如计费行/有效对账行的关系）。
 * 没有生成式模型调用，也不伪造「AI 结论」；问答入口会明确说明 C16 缺口。
 */
export function AssistantPanel({ onClose }: AssistantPanelProps) {
  const location = useLocation();
  const session = useSession();
  const [question, setQuestion] = useState('');
  const [asked, setAsked] = useState<string | null>(null);

  const approvals = useQuery({
    queryKey: ['assistant', 'pending-approvals'],
    queryFn: () => api.listPendingApprovals({ limit: 50 }),
    // 与其它页面同一条规矩：身份/范围没就绪前不发请求，避免把占位 org 打到后端。
    enabled: session.canQuery,
    staleTime: 10_000,
  });

  const stats = useMemo(() => {
    const items = approvals.data?.items ?? [];
    return {
      total: items.length,
      touchesFund: items.filter((item) => item.touches_fund).length,
      w3: items.filter((item) => item.write_level === 'W3').length,
      missingRole: items.filter((item) => item.missing_roles.length > 0).length,
    };
  }, [approvals.data]);

  const page = location.pathname;
  const suggestions = useMemo<Suggestion[]>(() => {
    const list: Suggestion[] = [];
    if (stats.w3 > 0) {
      list.push({
        title: `${stats.w3} 张 W3 工单不可自动执行`,
        detail: 'W3 只能人工处置或终止。先到审批队列确认等级与缺失角色，再决定动作。',
        to: '/approvals',
        cta: '查看队列',
      });
    }
    if (stats.missingRole > 0) {
      list.push({
        title: `${stats.missingRole} 张审批仍缺角色`,
        detail: '缺角色时通过不会推进到执行；队列已按 required_roles 标出缺口。',
        to: '/approvals',
        cta: '核对角色',
      });
    }
    if (stats.touchesFund > 0) {
      list.push({
        title: `${stats.touchesFund} 张涉及资金`,
        detail: '金额与 W 等级已并列显示，建议逐张核对后再做决定。',
        to: '/approvals',
        cta: '逐张核对',
      });
    }
    if (page.includes('/metering')) {
      list.push({
        title: '有效对账行 59 与计费行 59 相等',
        detail: '本期没有已确认错放与客户原因扣减，所以两者相等；发生扣减后计费行会小于有效对账行。',
        to: location.pathname,
        cta: '回看口径',
      });
    }
    if (page.includes('/recon')) {
      list.push({
        title: '差异先看证据链，再看建议',
        detail: '字段 diff 与证据（单据号 / 字段 / 页码 / bbox）在同一页，建议只是待确认草稿。',
        to: page.includes('/diffs/') ? page : '/recon',
        cta: '打开差异',
      });
    }
    if (page.includes('/tickets')) {
      list.push({
        title: '工单详情右侧看审批进度',
        detail: '缺哪个角色、审计链是否完整、动作是否可用，都在右侧决策栏，不用回列表。',
        to: page,
        cta: '停留在本页',
      });
    }
    if (list.length === 0) {
      list.push({
        title: '从待决策开始，而不是从菜单开始',
        detail: '工作台按 W3 > W2 > 涉及资金 > 等待时长排序，先处理最贵的阻塞。',
        to: '/',
        cta: '去工作台',
      });
    }
    return list.slice(0, 3);
  }, [location.pathname, page, stats]);

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
            来源：审批队列接口 + 契约口径；不调用生成式模型。
          </p>
        </div>
        <button type="button" className="btn btn-ghost btn-sm shrink-0" onClick={onClose} aria-label="收起助手">
          <IconClose />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        <div className="rounded-xl border border-slate-200 bg-slate-50/60 px-3 py-2.5">
          <div className="flex items-center justify-between text-[11px] text-slate-500">
            <span>待审批</span>
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
                <span className="badge bg-rose-50 text-rose-700 ring-rose-200">W3 {stats.w3}</span>
                <span className="badge bg-amber-50 text-amber-700 ring-amber-200">涉资金 {stats.touchesFund}</span>
                <span className="badge bg-slate-100 text-slate-600 ring-slate-200">缺角色 {stats.missingRole}</span>
              </span>
            </div>
          )}
        </div>

        <div className="mt-3">
          <div className="flex items-center justify-between px-1">
            <span className="eyebrow">下一步</span>
            <span className="text-[10px] text-slate-400">按风险排序</span>
          </div>
          <div className="mt-1.5 space-y-1.5">
            {suggestions.map((item) => (
              <div key={item.title} className="rounded-xl border border-slate-200 bg-white p-2.5 transition-colors hover:border-indigo-200">
                <div className="text-[12px] font-medium leading-5 text-slate-900">{item.title}</div>
                <p className="mt-0.5 text-[11px] leading-[17px] text-slate-500">{item.detail}</p>
                <Link to={item.to} className="mt-1.5 inline-flex items-center gap-1 text-[11px] font-medium text-indigo-600 hover:text-indigo-700">
                  {item.cta}
                  <IconArrowRight width={12} height={12} />
                </Link>
              </div>
            ))}
          </div>
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
        演示模式：建议由页面数据与契约口径生成，不含模型输出。
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
