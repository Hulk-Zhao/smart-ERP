import { useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { configureApi, useApiConfig, type ScopeSource } from '../api/config';
import { setBusinessQueriesPaused } from '../api/query-pause';
import { useSession, type SessionState } from '../api/session';
import type { SessionScopeOut } from '../api/generated/contract';
import { CONTRACT_PATH_COUNT, CONTRACT_VERSION } from '../api/types';
import { OPEN_SETTINGS_EVENT } from '../lib/settings';
import { shortId } from '../lib/format';
import { AssistantPanel } from './AssistantPanel';
import { CommandPalette } from './CommandPalette';
import { DemoIdentitySwitcher } from './DemoIdentitySwitcher';
import { SessionNotice } from './SessionNotice';
import {
  IconBell,
  IconCheck,
  IconClose,
  IconHome,
  IconMenu,
  IconScale,
  IconSearch,
  IconSettings,
  IconSparkles,
  IconTicket,
} from './icons';

const NAV = [
  { to: '/', label: '工作台', end: true, hint: '今日待办与范围概览', icon: IconHome },
  { to: '/tickets', label: '工单', hint: '列表 / 12 状态 / 时间线', icon: IconTicket },
  { to: '/approvals', label: '审批', hint: '队列 / 双人复核 / 决策', icon: IconCheck },
  { to: '/recon', label: '对账', hint: '发起 / 差异 / 证据 / 计量', icon: IconScale },
];

const SIDEBAR_KEY = 'smart-erp.web.sidebar.collapsed';

function SourceBadge({ compact = false }: { compact?: boolean }) {
  const config = useApiConfig();
  return config.mock ? (
    <span
      className="badge bg-amber-50 text-amber-700 ring-amber-200"
      title="VITE_USE_MOCK=true，仅离线开发/演示使用"
    >
      {compact ? 'MOCK' : 'MOCK 数据'}
    </span>
  ) : (
    <span className="badge bg-emerald-50 text-emerald-700 ring-emerald-200" title="真实 /v1 契约调用">
      {compact ? 'API' : '真实 API'}
    </span>
  );
}

function pageMeta(pathname: string): { title: string; crumbs: string[] } {
  if (pathname === '/') return { title: '工作台', crumbs: ['工作台'] };
  if (pathname.startsWith('/tickets/')) return { title: '工单详情', crumbs: ['工单', '详情'] };
  if (pathname === '/tickets') return { title: '工单', crumbs: ['工单'] };
  if (pathname === '/approvals') return { title: '审批', crumbs: ['审批'] };
  if (pathname.includes('/metering')) return { title: '计量表', crumbs: ['对账', '计量表'] };
  if (pathname.includes('/diffs/')) return { title: '差异详情', crumbs: ['对账', '差异详情'] };
  if (pathname.startsWith('/recon/')) return { title: '对账批次', crumbs: ['对账', '批次'] };
  if (pathname === '/recon') return { title: '对账', crumbs: ['对账'] };
  return { title: '工作台', crumbs: ['工作台'] };
}

type ScopePanelState =
  | 'loading'
  | 'ready'
  | 'manual'
  | 'multi-org'
  | 'missing-period'
  | 'missing'
  | 'unavailable'
  | 'error';

const SCOPE_BADGE: Record<ScopePanelState, string> = {
  loading: '获取中',
  ready: '已从会话带出',
  manual: '手动填写（优先）',
  'multi-org': '待选择组织',
  'missing-period': '待补全账期',
  missing: '当前无法获取默认范围',
  unavailable: '当前无法获取默认范围',
  error: '当前无法获取默认范围',
};

const SCOPE_HINT: Record<ScopePanelState, string> = {
  loading: '正在用当前令牌获取会话默认范围…',
  ready: '三项已由 GET /v1/auth/session 的 default_scope 带出，直接保存即可；需要时仍可修改。',
  manual: '当前三项是手动填写的范围，优先于会话默认范围；改成与会话一致的值保存即恢复自动跟随。',
  'multi-org':
    '该令牌授权了多个组织，服务端不替你选择。请填写要使用的 org_id，保存后才会加载数据。',
  'missing-period': '该令牌的授权期内没有可用账期（period 为 null），请手动填写 period。',
  missing: '当前无法获取默认范围：会话未返回 default_scope（端点可能未启用）。下面三项是未确认的旧值/环境默认值，核对后保存才会加载数据。',
  unavailable:
    '当前无法获取默认范围：会话端点返回 401（演示会话未启用或令牌无效）。下面三项未经确认，先核对保存或更换令牌。',
  error: '当前无法获取默认范围：会话端点异常。请手动填写下面三项。',
};

function SettingsPanel({ onClose }: { onClose: () => void }) {
  const config = useApiConfig();
  const queryClient = useQueryClient();
  const [baseUrl, setBaseUrl] = useState(config.baseUrl);
  const [token, setToken] = useState(config.token);
  const [orgId, setOrgId] = useState(config.scope.org_id);
  const [period, setPeriod] = useState(config.scope.period);
  const [systems, setSystems] = useState(config.scope.system_ids.join(', '));
  const [viewerName, setViewerName] = useState(config.viewerName);
  // 用户动过 scope 三项后就不再被会话默认范围覆盖，避免手打一半被重置。
  const [scopeEdited, setScopeEdited] = useState(false);
  const [saving, setSaving] = useState(false);
  const session = useSession();

  // 面板打开期间暂停页面业务查询：会话一返回也不许页面先拿还没确认的范围取数。
  useEffect(() => {
    setBusinessQueriesPaused(true);
    return () => setBusinessQueriesPaused(false);
  }, []);

  const defaultScope = session.session?.default_scope ?? null;
  const scopeState: ScopePanelState =
    session.status === 'ready'
      ? !defaultScope
        ? 'missing'
        : !defaultScope.org_id
          ? 'multi-org'
          : !defaultScope.period
            ? 'missing-period'
            : config.scopeSource === 'manual'
              ? 'manual'
              : 'ready'
      : session.status === 'loading'
        ? 'loading'
        : session.unavailable
          ? 'unavailable'
          : 'error';

  // 会话把 default_scope 写进配置后同步到输入框（用户已手改则不覆盖）。
  const systemsText = config.scope.system_ids.join(', ');
  useEffect(() => {
    if (scopeEdited) return;
    setOrgId(config.scope.org_id);
    setPeriod(config.scope.period);
    setSystems(systemsText);
  }, [config.scope.org_id, config.scope.period, systemsText, scopeEdited]);

  /** 用给定令牌取一次会话（命中同一 queryKey 缓存就不重复请求），返回 default_scope。 */
  const resolveDefaultScope = async (nextToken: string): Promise<SessionScopeOut | null> => {
    configureApi({ token: nextToken, scopeSource: 'pending' });
    try {
      const data = await queryClient.fetchQuery({
        queryKey: ['auth', 'session', nextToken],
        queryFn: () => api.getSession(),
        staleTime: Infinity,
        retry: false,
      });
      return data.default_scope ?? null;
    } catch {
      return null;
    }
  };

  /** 输入即去抖探测（不依赖 blur）；保存时还会再取一次，正确性不靠时序。 */
  useEffect(() => {
    const next = token.trim();
    if (next === config.token) return;
    const timer = window.setTimeout(() => {
      configureApi({ token: next, scopeSource: 'pending' });
      void queryClient.invalidateQueries({ queryKey: ['auth', 'session'] });
    }, 400);
    return () => window.clearTimeout(timer);
  }, [token, config.token, queryClient]);

  /** 失焦立即探测，省去等去抖的 400ms。 */
  const probeSession = () => {
    const next = token.trim();
    if (next === config.token) return;
    configureApi({ token: next, scopeSource: 'pending' });
    void queryClient.invalidateQueries({ queryKey: ['auth', 'session'] });
  };

  const org = orgId.trim();
  const periodValue = period.trim();
  const systemIds = systems
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
  // org_id 为空时禁止保存：多组织是服务端明确交给用户的选择，不能用默认值替代。
  const canSave = org !== '' && periodValue !== '';

  const save = async () => {
    if (!canSave || saving) return;
    setSaving(true);
    const nextToken = token.trim();
    const tokenChanged = nextToken !== config.token;
    let scope = {
      org_id: org,
      period: periodValue,
      system_ids: systemIds.length > 0 ? systemIds : config.scope.system_ids,
    };
    let scopeSource: ScopeSource = 'manual';

    // 没手改且来源不是「用户已确认的手动范围」时，保存前先用输入框里的令牌取一次会话，
    // 再决定用哪套范围：这样「粘完直接点保存」不依赖 blur/请求返回的先后顺序。
    if (!scopeEdited && (config.scopeSource !== 'manual' || tokenChanged)) {
      const resolved = await resolveDefaultScope(nextToken);
      if (resolved) {
        if (!resolved.org_id || !resolved.period) {
          // 多组织 / 无账期：切到选择态，面板保持打开等用户决定，不替用户猜。
          setSaving(false);
          return;
        }
        scope = {
          org_id: resolved.org_id,
          period: resolved.period,
          system_ids: resolved.system_ids.length > 0 ? resolved.system_ids : scope.system_ids,
        };
        scopeSource = 'session';
      }
      // resolved 为 null（端点未启用 / 401 / 异常）：按当前字段保存为 manual，由用户核对确认。
    }

    configureApi({
      baseUrl: baseUrl.trim() || '/erp/api',
      token: nextToken,
      viewerName: viewerName.trim() || '当前用户',
      scope,
      scopeSource,
    });
    queryClient.invalidateQueries();
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-end justify-center bg-slate-900/40 p-0 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-label="连接设置"
    >
      <div
        className="max-h-[92vh] w-full overflow-y-auto rounded-t-2xl bg-white p-4 shadow-xl sm:max-w-lg sm:rounded-2xl"
        data-testid="connection-settings"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-[15px] font-semibold text-slate-900">连接设置</h2>
            <p className="mt-1 text-[11px] text-slate-500">
              配置只保存在本机浏览器。契约 {CONTRACT_VERSION} · {CONTRACT_PATH_COUNT} 条路径。
            </p>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} aria-label="关闭设置">
            <IconClose />
          </button>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-3">
          <label className="block min-w-0">
            <span className="label">API baseUrl</span>
            <input className="input mt-1" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="/erp/api" />
          </label>

          <label className="block min-w-0">
            <span className="label">会话令牌 X-Session-Token</span>
            <input
              className="input mt-1"
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              onBlur={probeSession}
              onKeyDown={(event) => {
                if (event.key === 'Enter') probeSession();
              }}
              placeholder="真实环境必填；mock 可留空"
              autoComplete="off"
              data-testid="session-token"
            />
          </label>

          <div
            className={`min-w-0 rounded-lg border px-3 py-2.5 ${
              scopeState === 'ready'
                ? 'border-emerald-200 bg-emerald-50/70'
                : scopeState === 'loading' || scopeState === 'manual'
                  ? 'border-slate-200 bg-slate-50'
                  : 'border-amber-200 bg-amber-50/70'
            }`}
            data-testid="scope-source"
            data-state={scopeState}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="label">数据范围 org_id / period / system_ids</span>
              <span
                className={`badge ${
                  scopeState === 'ready'
                    ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                    : scopeState === 'loading' || scopeState === 'manual'
                      ? 'bg-slate-100 text-slate-600 ring-slate-200'
                      : 'bg-amber-50 text-amber-700 ring-amber-200'
                }`}
              >
                {SCOPE_BADGE[scopeState]}
              </span>
            </div>
            <p className="mt-1 text-[11px] leading-5 text-slate-600">{SCOPE_HINT[scopeState]}</p>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block min-w-0">
              <span className="label">
                org_id {org === '' ? <span className="text-amber-700">（必填）</span> : null}
              </span>
              <input
                className={`input mt-1 ${org === '' ? 'ring-1 ring-amber-300' : ''}`}
                value={orgId}
                onChange={(event) => {
                  setScopeEdited(true);
                  setOrgId(event.target.value);
                }}
                placeholder={scopeState === 'multi-org' ? '请选择 / 填写要使用的 org_id' : 'ORG-1'}
                data-testid="scope-org"
              />
            </label>
            <label className="block min-w-0">
              <span className="label">period</span>
              <input
                className="input mt-1"
                value={period}
                onChange={(event) => {
                  setScopeEdited(true);
                  setPeriod(event.target.value);
                }}
                data-testid="scope-period"
              />
            </label>
          </div>

          <label className="block min-w-0">
            <span className="label">system_ids（逗号分隔）</span>
            <input
              className="input mt-1"
              value={systems}
              onChange={(event) => {
                setScopeEdited(true);
                setSystems(event.target.value);
              }}
              data-testid="scope-systems"
            />
          </label>

          <label className="block min-w-0">
            <span className="label">当前身份显示名</span>
            <input className="input mt-1" value={viewerName} onChange={(event) => setViewerName(event.target.value)} />
          </label>

          <div className="min-w-0 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5" data-testid="session-facts">
            <div className="flex items-center justify-between gap-2">
              <span className="label">会话身份（GET /v1/auth/session）</span>
              <span
                className={`badge ${
                  session.status === 'ready'
                    ? 'bg-emerald-50 text-emerald-700 ring-emerald-200'
                    : session.status === 'loading'
                      ? 'bg-slate-100 text-slate-600 ring-slate-200'
                      : 'bg-amber-50 text-amber-700 ring-amber-200'
                }`}
              >
                {session.status === 'ready'
                  ? '已连接'
                  : session.status === 'loading'
                    ? '获取中'
                    : session.unavailable
                      ? '未启用演示会话'
                      : '获取失败'}
              </span>
            </div>
            {session.session ? (
              <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[11px]">
                <dt className="text-slate-500">user_id</dt>
                <dd className="truncate font-mono text-slate-700" title={session.session.user_id}>
                  {session.session.user_id}
                </dd>
                <dt className="text-slate-500">tenant_id</dt>
                <dd className="truncate font-mono text-slate-700" title={session.session.tenant_id}>
                  {session.session.tenant_id}
                </dd>
                <dt className="text-slate-500">roles</dt>
                <dd className="font-mono text-slate-700">
                  {session.session.roles.length > 0 ? session.session.roles.join(' / ') : '（空）'}
                </dd>
                <dt className="text-slate-500">default_scope</dt>
                <dd className="font-mono text-slate-700">
                  {session.session.default_scope
                    ? `${session.session.default_scope.org_id ?? 'null（多组织，待选择）'} · ${
                        session.session.default_scope.period ?? 'null'
                      } · ${session.session.default_scope.system_ids.join(',') || '（空）'}`
                    : '（未返回）'}
                </dd>
              </dl>
            ) : (
              <p className="mt-1 text-[11px] leading-5 text-slate-500">
                身份和角色只来自会话端点，不再用 <span className="font-mono">VITE_SESSION_ROLES</span> /
                <span className="font-mono"> VITE_VIEWER_ID</span> 猜。
                {session.unavailable
                  ? '当前返回 401：服务端演示会话未启用或令牌无效。'
                  : session.status === 'error'
                    ? '当前端点异常，详情见页面提示。'
                    : ''}
              </p>
            )}
          </div>

          <p className="rounded-lg bg-slate-50 px-3 py-2 text-[11px] leading-5 text-slate-500">
            离线 mock 只在构建/启动时通过 <span className="font-mono">VITE_USE_MOCK=true</span> 显式开启，默认关闭；
            为避免误连假数据，界面不提供运行期切换。
          </p>

          <div className="flex flex-wrap items-center justify-end gap-2">
            {canSave ? null : (
              <span className="mr-auto text-[11px] leading-5 text-amber-700" data-testid="save-blocked">
                org_id 与 period 不能为空；补齐后保存会立即按该范围加载数据。
              </span>
            )}
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={save}
              disabled={!canSave || saving}
              title={canSave ? undefined : 'org_id 与 period 不能为空'}
              data-testid="save-settings"
            >
              {saving ? '正在获取默认范围…' : '保存并刷新'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const SCOPE_GATE: Record<NonNullable<SessionState['scopeIssue']>, { title: string; detail: string }> = {
  'multi-org': {
    title: '请选择要使用的组织',
    detail: '该会话令牌授权了多个组织，服务端不会替用户挑选。填写 org_id 后才会加载数据。',
  },
  'missing-period': {
    title: '请补全账期',
    detail: '该令牌的授权期内没有可用账期（period 为 null），需要手动填写后才能加载数据。',
  },
  'missing-scope': {
    title: '请补全数据范围',
    detail: '会话没有返回 default_scope，需要手动填写 org_id / period / system_ids。',
  },
  incomplete: {
    title: '请补全数据范围',
    detail: 'org_id 或 period 为空，补齐后才会加载数据。',
  },
};

/** scope 未就绪时挡在页面之前：宁可空着让用户选，也不拿占位 org 去打后端。 */
function ScopeGate({ session, onOpenSettings }: { session: SessionState; onOpenSettings: () => void }) {
  const issue = session.scopeIssue ?? 'incomplete';
  const copy = SCOPE_GATE[issue];
  return (
    <div className="mx-auto mt-10 max-w-xl" data-testid="scope-gate" data-state={issue}>
      <section className="card p-6">
        <div className="eyebrow">数据范围未就绪</div>
        <h2 className="mt-1 text-[18px] font-semibold tracking-tight text-slate-900">{copy.title}</h2>
        <p className="mt-2 text-[12px] leading-6 text-slate-600">{copy.detail}</p>
        <button type="button" className="btn btn-primary mt-4" onClick={onOpenSettings}>
          打开连接设置
        </button>
      </section>
    </div>
  );
}

function SidebarNav({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const session = useSession();
  const pending = useQuery({
    queryKey: ['shell', 'pending-approvals-count'],
    queryFn: () => api.listPendingApprovals({ limit: 50 }),
    // 会话身份未就绪时不查待办：否则 401 会被当成「0 条待办」显示在侧栏。
    enabled: session.canQuery,
    staleTime: 20_000,
    retry: 0,
  });
  const pendingCount = pending.data?.items.length ?? 0;

  return (
    <>
      <div className={`flex h-14 shrink-0 items-center gap-2 border-b border-slate-200 ${collapsed ? 'justify-center px-2' : 'px-4'}`}>
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-900 text-[13px] font-bold text-white">
          E
        </span>
        {collapsed ? null : (
          <div className="min-w-0">
            <div className="truncate text-[13px] font-semibold text-slate-900">smart-ERP</div>
            <div className="truncate text-[10px] text-slate-400">聚合执行工作台</div>
          </div>
        )}
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto py-1">
        {collapsed ? null : <div className="nav-section">工作区</div>}
        {NAV.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              title={item.hint}
              onClick={onNavigate}
              className={({ isActive }) =>
                `nav-item ${isActive ? 'nav-item-active' : ''} ${collapsed ? 'justify-center px-0' : ''}`
              }
            >
              <Icon className="shrink-0" />
              {collapsed ? null : <span className="min-w-0 flex-1">{item.label}</span>}
              {collapsed ? null : item.to === '/approvals' && pendingCount > 0 ? (
                <span className="rounded-full bg-white px-1.5 text-[10px] font-semibold text-indigo-600 ring-1 ring-indigo-200">
                  {pendingCount}
                </span>
              ) : null}
            </NavLink>
          );
        })}
      </nav>

      <div className={`border-t border-slate-200 py-2 ${collapsed ? 'px-2' : 'px-3'}`}>
        {collapsed ? null : (
          <div className="mb-2 rounded-lg bg-slate-50 px-2.5 py-2">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-medium text-slate-500">契约</span>
              <span className="font-mono text-[10px] text-slate-400">C16 · {CONTRACT_PATH_COUNT}</span>
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span className="text-[10px] text-slate-400">v{CONTRACT_VERSION}</span>
              <SourceBadge />
            </div>
          </div>
        )}
      </div>
    </>
  );
}

export function AppShell() {
  const config = useApiConfig();
  const session = useSession();
  const location = useLocation();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [notifyOpen, setNotifyOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [compact, setCompact] = useState(() => {
    try {
      return globalThis.localStorage?.getItem('smart-erp.web.density') === 'compact';
    } catch {
      return false;
    }
  });
  useEffect(() => {
    document.documentElement.dataset.density = compact ? 'compact' : 'comfortable';
    try {
      globalThis.localStorage?.setItem('smart-erp.web.density', compact ? 'compact' : 'comfortable');
    } catch {
      // 仅影响持久化，不影响当前会话
    }
  }, [compact]);
  const [assistantOpen, setAssistantOpen] = useState(() => {
    // 窄屏不默认展开助手，避免首屏被抽屉遮住；桌面默认展开。
    const desktop = globalThis.matchMedia?.('(min-width: 1280px)').matches ?? true;
    if (!desktop) return false;
    try {
      return globalThis.localStorage?.getItem('smart-erp.web.assistant') !== 'closed';
    } catch {
      return true;
    }
  });
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return globalThis.localStorage?.getItem(SIDEBAR_KEY) === '1';
    } catch {
      return false;
    }
  });

  const meta = useMemo(() => pageMeta(location.pathname), [location.pathname]);

  useEffect(() => {
    const open = () => setSettingsOpen(true);
    window.addEventListener(OPEN_SETTINGS_EVENT, open);
    return () => window.removeEventListener(OPEN_SETTINGS_EVENT, open);
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === 'INPUT' ||
        target?.tagName === 'TEXTAREA' ||
        target?.tagName === 'SELECT' ||
        target?.isContentEditable;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        setPaletteOpen((current) => !current);
      } else if (event.key === 'Escape') {
        setPaletteOpen(false);
        setNotifyOpen(false);
        setMobileNavOpen(false);
      } else if (!typing && event.key === '?') {
        event.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    try {
      globalThis.localStorage?.setItem('smart-erp.web.assistant', assistantOpen ? 'open' : 'closed');
    } catch {
      // localStorage 不可用时只影响持久化，不影响当前会话
    }
  }, [assistantOpen]);

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current;
      try {
        globalThis.localStorage?.setItem(SIDEBAR_KEY, next ? '1' : '0');
      } catch {
        // 同上
      }
      return next;
    });
  };

  return (
    <div className="app-shell">
      <aside className={`app-sidebar ${collapsed ? 'app-sidebar-collapsed' : ''}`}>
        <SidebarNav collapsed={collapsed} />
        <button
          type="button"
          onClick={toggleCollapsed}
          className="mx-3 mb-2 hidden rounded-lg border border-slate-200 py-1.5 text-[11px] text-slate-500 hover:bg-slate-50 lg:block"
          aria-label={collapsed ? '展开侧栏' : '收起侧栏'}
        >
          {collapsed ? '»' : '« 收起'}
        </button>
      </aside>

      {mobileNavOpen ? (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="导航">
          <div className="drawer-overlay" onClick={() => setMobileNavOpen(false)} />
          <div className="fixed inset-y-0 left-0 z-50 flex w-[260px] flex-col border-r border-slate-200 bg-white shadow-2xl">
            <SidebarNav collapsed={false} onNavigate={() => setMobileNavOpen(false)} />
          </div>
        </div>
      ) : null}

      <div className="app-main">
        <header className="app-topbar">
          <button
            type="button"
            className="btn btn-ghost btn-sm lg:hidden"
            onClick={() => setMobileNavOpen(true)}
            aria-label="打开导航"
          >
            <IconMenu />
          </button>

          <div className="min-w-0 flex-1">
            <div className="breadcrumb">
              <span className="text-slate-400">智能执行</span>
              {meta.crumbs.map((crumb, index) => (
                <span key={`${crumb}-${index}`} className="flex min-w-0 items-center gap-1.5">
                  <span className="text-slate-300">/</span>
                  <span className={index === meta.crumbs.length - 1 ? 'truncate font-medium text-slate-700' : 'truncate'}>
                    {crumb}
                  </span>
                </span>
              ))}
            </div>
            <h1 className="truncate text-[14px] font-semibold leading-5 text-slate-900">{meta.title}</h1>
          </div>

          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="hidden h-8 min-w-[190px] items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-2.5 text-left text-[12px] text-slate-400 hover:border-slate-300 sm:flex"
            aria-label="打开命令面板"
          >
            <IconSearch width={14} height={14} />
            <span className="flex-1">搜索工单、批次、页面</span>
            <kbd className="kbd">⌘K</kbd>
          </button>
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="btn btn-ghost btn-sm sm:hidden"
            aria-label="搜索"
          >
            <IconSearch />
          </button>

          <span
            className="hidden max-w-[260px] items-center gap-1 truncate rounded-lg border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600 md:flex"
            title={`${config.scope.org_id || '未选择组织'} · ${config.scope.period || '—'} · ${config.scope.system_ids.join(',')}`}
            data-testid="scope-chip"
          >
            <span className={`status-dot ${config.scope.org_id ? 'bg-emerald-500' : 'bg-amber-500'}`} />
            <span className="truncate">
              {config.scope.org_id || '未选择组织'} · {config.scope.period || '—'} · {config.scope.system_ids.join(',')}
            </span>
          </span>

          <div className="hidden sm:block">
            <SourceBadge compact />
          </div>

          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setCompact((current) => !current)}
            aria-pressed={compact}
            aria-label={compact ? '表格密度：紧凑，点击切换为舒适' : '表格密度：舒适，点击切换为紧凑'}
            title={compact ? '表格密度：紧凑（点击切换）' : '表格密度：舒适（点击切换）'}
          >
            <span className="text-[11px] font-medium">{compact ? '紧凑' : '舒适'}</span>
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setAssistantOpen((current) => !current)}
            aria-label={assistantOpen ? '收起助手' : '打开助手'}
            title={assistantOpen ? '收起助手' : '打开助手'}
          >
            <IconSparkles className={assistantOpen ? 'text-indigo-600' : 'text-slate-500'} />
            <span className="hidden xl:inline">助手</span>
          </button>
          <div className="relative">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-label="通知"
              title="通知"
              aria-expanded={notifyOpen}
              aria-haspopup="dialog"
              data-testid="notify-button"
              onClick={() => setNotifyOpen((current) => !current)}
            >
              <IconBell />
            </button>
            {notifyOpen ? (
              <div
                role="dialog"
                aria-label="通知"
                data-testid="notification-panel"
                className="absolute right-0 top-full z-50 mt-1 w-[280px] rounded-xl border border-slate-200 bg-white p-3 text-left shadow-xl"
              >
                <div className="text-[12px] font-semibold text-slate-900">通知中心未启用</div>
                <p className="mt-1 text-[11px] leading-5 text-slate-600">
                  本期没有站内通知通道，因此这里不会出现消息。待办看「审批」，执行与风险看「工作台」，
                  不另造通知数据。
                </p>
                <div className="mt-2 flex gap-1.5">
                  <NavLink to="/approvals" className="btn btn-secondary btn-sm" onClick={() => setNotifyOpen(false)}>
                    审批待办
                  </NavLink>
                  <NavLink to="/" className="btn btn-secondary btn-sm" onClick={() => setNotifyOpen(false)}>
                    工作台
                  </NavLink>
                </div>
              </div>
            ) : null}
          </div>
          <DemoIdentitySwitcher />
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setSettingsOpen(true)}
            aria-label="连接设置"
            title="连接设置"
          >
            <IconSettings />
          </button>
          <span
            className="ml-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-900 text-[11px] font-semibold text-white"
            data-testid="identity-avatar"
            title={
              session.session
                ? `user ${session.session.user_id} · tenant ${session.session.tenant_id} · roles ${session.session.roles.join(' / ') || '（空）'}`
                : session.unavailable
                  ? '当前未启用演示会话'
                  : config.viewerName
            }
          >
            {session.session ? shortId(session.session.user_id).slice(0, 1).toUpperCase() : config.viewerName.slice(0, 1)}
          </span>
        </header>

        <main className="app-content">
          {session.unavailable ? (
            // 401 时业务查询一律被 canQuery 拦住；若仍渲染 Outlet，页面会停在骨架屏上，
            // 看起来像「一直在加载」。身份缺失必须整页显式说明，而不是让每页各自猜。
            <div className="mx-auto max-w-2xl px-1 py-4">
              <SessionNotice
                error={session.error}
                onRetry={session.refresh}
                onOpenSettings={() => setSettingsOpen(true)}
              />
            </div>
          ) : session.status === 'ready' && session.scopeIssue !== null ? (
            <ScopeGate session={session} onOpenSettings={() => setSettingsOpen(true)} />
          ) : (
            <Outlet />
          )}
        </main>
      </div>

      {assistantOpen ? <AssistantPanel onClose={() => setAssistantOpen(false)} /> : null}

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onOpenSettings={() => setSettingsOpen(true)}
      />
      {settingsOpen ? <SettingsPanel onClose={() => setSettingsOpen(false)} /> : null}
    </div>
  );
}
