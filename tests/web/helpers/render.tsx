import type { ReactElement } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { AppShell } from '../../../web/src/components/AppShell';

function newQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

export function renderPage(
  element: ReactElement,
  options: { route: string; path: string },
): ReturnType<typeof render> {
  return render(
    <QueryClientProvider client={newQueryClient()}>
      <MemoryRouter initialEntries={[options.route]}>
        <Routes>
          <Route path={options.path} element={element} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 带 AppShell（侧栏/顶栏/连接设置/ScopeGate）渲染页面，用于验证「粘令牌 → 可用」的完整路径。 */
export function renderShell(
  element: ReactElement,
  options: { route?: string } = {},
): ReturnType<typeof render> {
  return render(
    <QueryClientProvider client={newQueryClient()}>
      <MemoryRouter initialEntries={[options.route ?? '/']}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="*" element={element} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
