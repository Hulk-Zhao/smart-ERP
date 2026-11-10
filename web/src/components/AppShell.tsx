import { useEffect, useState } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { configureApi, useApiConfig, type ApprovalRole } from '../api/config';
import { CONTRACT_PATH_COUNT, CONTRACT_VERSION } from '../api/types';
import { OPEN_SETTINGS_EVENT } from '../lib/settings';

const NAV = [
  { to: '/', label: '工作台', end: true, hint: '今日待办与范围概览' },
  { to: '/tickets', label: '工单', hint: '列表 / 12 状态 / 时间线' },
  { to: '/approvals', label: '审批', hint: '双人复核' },
  { to: '/recon', label: '对账', hint: '发起 / 差异 / 证据' },
];

const ROLE_CHOICES: ApprovalRole[] = ['reviewer', 'approver', 'cfo'];

function SourceBadge() {
  const config = useApiConfig();
  return config.mock ? (
    <span className="badge bg-amber-100 text-amber-800 ring-amber-200" title="VITE_USE_MOCK=true，仅离线开发使用">
      MOCK 数据
    </span>
  ) : (
    <span className="badge bg-emerald-50 text-emerald-700 ring-emerald-200" title="真实 /v1 契约调用">
      真实 API
    </span>
  );
}

function SettingsPanel({ onClose }: { onClose: () => void }) {
  const config = useApiConfig();
  const queryClient = useQueryClient();
  const [baseUrl, setBaseUrl] = useState(config.baseUrl);
  const [token, setToken] = useState(config.token);
  const [orgId, setOrgId] = useState(config.scope.org_id);
  const [period, setPeriod] = useState(config.scope.period);
  const [systems, setSystems] = useState(config.scope.system_ids.join(','));
  const [viewerName, setViewerName] = useState(config.viewerName);
  const [viewerId, setViewerId] = useState(config.viewerId);
  const [roles, setRoles] = useState<ApprovalRole[]>(config.roles);

  const toggleRole = (role: ApprovalRole) => {
    setRoles((current) =>
      current.includes(role) ? current.filter((item) => item !== role) : [...current, role],
    );
  };

  const save = () => {
    const systemIds = systems
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
    configureApi({
      baseUrl: baseUrl.trim() || '/erp/api',
      token: token.trim(),
      viewerName: viewerName.trim() || '当前用户',
      viewerId: viewerId.trim(),
      roles: roles.length > 0 ? roles : ['reviewer'],
      scope: {
        org_id: orgId.trim() || 'ORG-1',
        period: period.trim() || '2026-10',
        system_ids: systemIds.length > 0 ? systemIds : ['u8', 'statement'],
      },
    });
    queryClient.invalidateQueries();
    onClose();
  };

  return (
    <div className="fixed inset-0 z-40 flex items-end justify-center bg-slate-900/40 p-0 sm:items-center sm:p-4" role="dialog" aria-modal="true" aria-label="连接设置">
      <div className="max-h-[92vh] w-full overflow-y-auto rounded-t-2xl bg-white p-4 shadow-xl sm:max-w-lg sm:rounded-2xl">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-[15px] font-semibold text-slate-900">连接设置</h2>
            <p className="mt-1 text-[11px] text-slate-500">
              配置只保存在本机浏览器。契约 {CONTRACT_VERSION} · {CONTRACT_PATH_COUNT} 条路径。
            </p>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" onClick={onClose} aria-label="关闭设置">
            关闭
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
              placeholder="真实环境必填；mock 可留空"
              autoComplete="off"
            />
          </label>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block min-w-0">
              <span className="label">org_id</span>
              <input className="input mt-1" value={orgId} onChange={(event) => setOrgId(event.target.value)} />
            </label>
            <label className="block min-w-0">
              <span className="label">period</span>
              <input className="input mt-1" value={period} onChange={(event) => setPeriod(event.target.value)} />
            </label>
          </div>

          <label className="block min-w-0">
            <span className="label">system_ids（逗号分隔）</span>
            <input className="input mt-1" value={systems} onChange={(event) => setSystems(event.target.value)} />
          </label>

          <label className="block min-w-0">
            <span className="label">当前身份显示名</span>
            <input className="input mt-1" value={viewerName} onChange={(event) => setViewerName(event.target.value)} />
          </label>

          <label className="block min-w-0">
            <span className="label">当前用户 ID（匹配审批人，决定能否看到自己的意见）</span>
            <input
              className="input mt-1"
              value={viewerId}
              onChange={(event) => setViewerId(event.target.value)}
              placeholder="留空则任何审批意见都不展示正文"
            />
          </label>

          <fieldset className="min-w-0">
            <legend className="label">当前可用的审批角色（界面据此做意见脱敏）</legend>
            <div className="mt-1 flex flex-wrap gap-2">
              {ROLE_CHOICES.map((role) => (
                <label key={role} className={`chip cursor-pointer ${roles.includes(role) ? 'chip-active' : ''}`}>
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={roles.includes(role)}
                    onChange={() => toggleRole(role)}
                  />
                  {role}
                </label>
              ))}
            </div>
          </fieldset>

          <p className="rounded-lg bg-slate-50 px-3 py-2 text-[11px] leading-5 text-slate-500">
            离线 mock 只在构建/启动时通过 <span className="font-mono">VITE_USE_MOCK=true</span> 显式开启，默认关闭；
            为避免误连假数据，界面不提供运行期切换。
          </p>

          <div className="flex justify-end gap-2">
            <button type="button" className="btn btn-secondary" onClick={onClose}>
              取消
            </button>
            <button type="button" className="btn btn-primary" onClick={save}>
              保存并刷新
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function AppShell() {
  const config = useApiConfig();
  const [settingsOpen, setSettingsOpen] = useState(false);

  // 403 等错误态里的「打开连接设置」由统一组件触发，避免逐页传参。
  useEffect(() => {
    const open = () => setSettingsOpen(true);
    window.addEventListener(OPEN_SETTINGS_EVENT, open);
    return () => window.removeEventListener(OPEN_SETTINGS_EVENT, open);
  }, []);

  return (
    <div className="flex min-h-full w-full min-w-0 flex-col">
      <header className="sticky top-0 z-20 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex w-full max-w-7xl min-w-0 flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2 sm:px-5">
          <div className="flex min-w-0 shrink-0 items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-slate-900 text-[12px] font-bold text-white">E</span>
            <div className="min-w-0">
              <div className="truncate text-[13px] font-semibold text-slate-900">smart-ERP 工作台</div>
              <div className="truncate text-[10px] text-slate-400">C16 · {CONTRACT_PATH_COUNT} 条 /v1 契约</div>
            </div>
          </div>

          <nav className="order-3 -mx-1 w-full min-w-0 overflow-x-auto sm:order-none sm:mx-0 sm:w-auto sm:flex-1">
            <div className="flex w-max items-center gap-1 px-1 sm:w-auto">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  title={item.hint}
                  className={({ isActive }) => `nav-link whitespace-nowrap ${isActive ? 'nav-link-active' : ''}`}
                >
                  {item.label}
                </NavLink>
              ))}
            </div>
          </nav>

          <div className="ml-auto flex min-w-0 items-center gap-2">
            <SourceBadge />
            <span
              className="hidden max-w-[260px] truncate rounded-lg bg-slate-100 px-2 py-1 text-[11px] text-slate-600 md:inline-block"
              title={`${config.scope.org_id} · ${config.scope.period} · ${config.scope.system_ids.join(',')}`}
            >
              {config.scope.org_id} · {config.scope.period} · {config.scope.system_ids.join(',')}
            </span>
            <span className="hidden text-[11px] text-slate-500 sm:inline">{config.viewerName}</span>
            <button type="button" className="btn btn-secondary btn-sm whitespace-nowrap" onClick={() => setSettingsOpen(true)}>
              设置
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-7xl min-w-0 flex-1 px-3 py-4 sm:px-5">
        <Outlet />
      </main>

      {settingsOpen ? <SettingsPanel onClose={() => setSettingsOpen(false)} /> : null}
    </div>
  );
}
