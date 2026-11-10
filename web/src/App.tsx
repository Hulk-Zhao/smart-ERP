import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { ApprovalsPage } from './pages/ApprovalsPage';
import { MeteringPage } from './pages/MeteringPage';
import { OverviewPage } from './pages/OverviewPage';
import { ReconBatchPage } from './pages/ReconBatchPage';
import { ReconDiffPage } from './pages/ReconDiffPage';
import { ReconPage } from './pages/ReconPage';
import { TicketDetailPage } from './pages/TicketDetailPage';
import { TicketsPage } from './pages/TicketsPage';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route index element={<OverviewPage />} />
            <Route path="tickets" element={<TicketsPage />} />
            <Route path="tickets/:ticketId" element={<TicketDetailPage />} />
            <Route path="approvals" element={<ApprovalsPage />} />
            <Route path="recon" element={<ReconPage />} />
            <Route path="recon/:batchId" element={<ReconBatchPage />} />
            <Route path="recon/:batchId/metering" element={<MeteringPage />} />
            <Route path="recon/:batchId/diffs/:diffId" element={<ReconDiffPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
