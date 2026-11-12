import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { configureApi, useApiConfig } from '../api/config';
import type { DemoIdentityOut } from '../api/types';

/**
 * 演示身份切换器：调 `GET /v1/auth/demo-identities` 取部署期注入的身份与令牌，
 * 点击只做「换本机 X-Session-Token + 重取数据」，不绕过任何鉴权。
 *
 * 端点非 demo 环境返回 404，这里如实显示「未开启」，不伪造身份列表。
 */
export function DemoIdentitySwitcher() {
  const [open, setOpen] = useState(false);
  const config = useApiConfig();
  const queryClient = useQueryClient();
  const identities = useQuery({
    queryKey: ['auth', 'demo-identities'],
    queryFn: () => api.getDemoIdentities(),
    enabled: open,
    retry: false,
    staleTime: Infinity,
  });

  const switchTo = (identity: DemoIdentityOut) => {
    configureApi({ token: identity.token, viewerName: identity.label });
    // 当前页所有依赖身份的查询重新取；无权查看就照常报 403/空态，不吞错。
    queryClient.invalidateQueries();
    setOpen(false);
  };

  return (
    <div className="relative">
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        aria-label="演示身份切换"
        aria-expanded={open}
        title="演示身份切换"
        onClick={() => setOpen((value) => !value)}
        data-testid="demo-identity-toggle"
      >
        <span className="hidden lg:inline text-[11px] font-medium">演示身份</span>
        <span className="lg:hidden text-[11px] font-medium">身份</span>
      </button>

      {open ? (
        <div
          className="absolute right-0 z-[60] mt-2 w-72 rounded-xl border border-slate-200 bg-white p-2 shadow-xl"
          data-testid="demo-identity-panel"
        >
          <p className="px-2 py-1 text-[10.5px] leading-4 text-amber-700">
            演示身份切换，仅用于演示环境。只更换本机会话令牌，不绕过鉴权；无权访问的页面照常报错。
          </p>

          {identities.isPending ? (
            <p className="px-2 py-1 text-[11px] text-slate-500">读取中…</p>
          ) : null}

          {identities.isError ? (
            <p className="px-2 py-1 text-[11px] leading-4 text-slate-500" data-testid="demo-identity-unavailable">
              当前环境未开启演示身份接口；仍可在「连接设置」手输令牌。
            </p>
          ) : null}

          <div className="mt-1 space-y-0.5">
            {(identities.data?.items ?? []).map((identity) => {
              const current = config.token === identity.token;
              return (
                <button
                  key={identity.user_id}
                  type="button"
                  className={`flex w-full items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left text-[11.5px] ${
                    current ? 'bg-slate-900 text-white' : 'text-slate-700 hover:bg-slate-100'
                  }`}
                  onClick={() => switchTo(identity)}
                  data-testid="demo-identity-option"
                  aria-current={current ? 'true' : undefined}
                >
                  <span className="min-w-0 break-all font-mono">{identity.label}</span>
                  {current ? <span className="shrink-0 text-[10px]">当前</span> : null}
                </button>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
