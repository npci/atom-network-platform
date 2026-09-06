// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ROUTER_BASENAME } from './utils/basePath'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from './context/ThemeContext'
// R-2 — global resume-progress jobs registry (loaded inside QueryClientProvider
// so it can use api.get for the periodic /jobs/active reconcile).
import { JobsProvider } from './context/JobsContext'
import AppLayout from './components/layout/AppLayout'
import ProtectedRoute from './components/common/ProtectedRoute'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import NewChange from './pages/ChangeRequest/NewChange'
import ChangeDetail from './pages/ChangeRequest/ChangeDetail'
import StepView from './pages/ChangeRequest/StepView'
import ProductKnowledge from './pages/Admin/ProductKnowledge'
import CodeKnowledge from './pages/Admin/CodeKnowledge'
import CodeIndexing from './pages/Admin/CodeIndexing'
import ApiRegistry from './pages/Admin/ApiRegistry'
import AgenticCodegen from './pages/Admin/AgenticCodegen'
import Configuration from './pages/Admin/Configuration'
import BuildHost from './pages/Admin/BuildHost'
import AuthorityPolicyPage from './pages/Admin/AuthorityPolicy'
import GovernanceSkills from './pages/Admin/GovernanceSkills'
import EvalPolicy from './pages/Admin/EvalPolicy'
import EvalLogs from './pages/Admin/EvalLogs'
import EvalMetrics from './pages/Admin/EvalMetrics'
import EvalCompare from './pages/Admin/EvalCompare'
import Partners from './pages/Admin/Partners'
import A2ALogs from './pages/Admin/A2ALogs'
import CertA2ATrigger from './pages/Admin/CertA2ATrigger'
import Logs from './pages/Admin/Logs'
import UserManagement from './pages/Admin/UserManagement'
import PromptEnhancement from './pages/ChangeRequest/PromptEnhancement'
import Research from './pages/ChangeRequest/Research'
import Canvas from './pages/ChangeRequest/Canvas'
import Clarification from './pages/ChangeRequest/Clarification'
import BRD from './pages/ChangeRequest/BRD'
import TechSpec from './pages/ChangeRequest/TechSpec'
import XSD from './pages/ChangeRequest/XSD'
import ProductKit from './pages/ChangeRequest/ProductKit'
import ProductKitManager from './pages/ProductKitManager'
import PhaseB from './pages/ChangeRequest/PhaseB'
import PhaseC from './pages/ChangeRequest/PhaseC'
import Approvals from './pages/Approvals'
import Usage from './pages/Usage'
import TeamInbox from './pages/TeamInbox'
import AgentMessaging from './pages/Certification/AgentMessaging'
import CertificationStatus from './pages/Certification/CertificationStatus'
import CertDashboard from './pages/Certification/CertDashboard'
import CertChangeDetail from './pages/Certification/CertChangeDetail'
import CertPartnerEntries from './pages/Certification/CertPartnerEntries'
import CertConversation from './pages/Certification/CertConversation'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,               // no retries — a 401 retry just fires the redirect twice
      staleTime: 30_000,
      refetchOnWindowFocus: false, // switching tabs must NOT trigger API calls with old tokens
      refetchOnReconnect: false,
    },
  },
})

export default function App() {
  return (
    <ThemeProvider>
    <QueryClientProvider client={queryClient}>
    <JobsProvider>
      <BrowserRouter basename={ROUTER_BASENAME}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="changes/new" element={<NewChange />} />
            <Route path="changes/:id" element={<ChangeDetail />} />
            <Route path="changes/:id/view/:stepKey" element={<StepView />} />
            <Route path="changes/:id/prompt_enhancement" element={<PromptEnhancement />} />
            <Route path="changes/:id/research" element={<Research />} />
            <Route path="changes/:id/canvas" element={<Canvas />} />
            <Route path="changes/:id/clarify" element={<Clarification />} />
            <Route path="changes/:id/brd" element={<BRD />} />
            <Route path="changes/:id/tech_spec" element={<TechSpec />} />
            <Route path="changes/:id/xsd" element={<XSD />} />
            <Route path="changes/:id/product_kit" element={<ProductKit />} />
            <Route path="product-kit" element={<ProductKitManager />} />
            <Route path="product-kit/:id" element={<ProductKitManager />} />
            <Route path="changes/:id/phase-b" element={<PhaseB />} />
            <Route path="changes/:id/phase-c" element={<PhaseC />} />
            <Route path="approvals" element={<Approvals />} />
            <Route path="usage" element={<Usage />} />
            <Route path="escalations" element={<TeamInbox />} />
            <Route path="certification/agent-messaging" element={<AgentMessaging />} />
            <Route path="certification/status"          element={<CertificationStatus />} />
            <Route path="certification/dashboard"       element={<CertDashboard />} />
            <Route path="certification/changes/:crId"   element={<CertChangeDetail />} />
            <Route path="certification/partners"        element={<CertPartnerEntries />} />
            <Route path="certification/changes/:crId/conversation/:partnerId" element={<CertConversation />} />
            <Route path="admin/partners" element={<Partners />} />
            <Route path="admin/users" element={<UserManagement />} />
            <Route path="admin/product-knowledge" element={<ProductKnowledge />} />
            <Route path="admin/code-knowledge" element={<CodeKnowledge />} />
            <Route path="admin/code-indexing" element={<CodeIndexing />} />
            <Route path="admin/api-registry" element={<ApiRegistry />} />
            <Route path="admin/agentic" element={<ProtectedRoute requiredRole="tech_lead"><AgenticCodegen /></ProtectedRoute>} />
            <Route path="admin/configuration" element={<Configuration />} />
            <Route path="admin/build-host" element={<BuildHost />} />
            <Route path="admin/authority-policy" element={<AuthorityPolicyPage />} />
            <Route path="admin/governance-skills" element={<GovernanceSkills />} />
            <Route path="admin/eval-policy" element={<EvalPolicy />} />
            <Route path="admin/eval-logs" element={<EvalLogs />} />
            <Route path="admin/eval-metrics" element={<EvalMetrics />} />
            <Route path="admin/eval-compare" element={<EvalCompare />} />
            <Route path="admin/a2a-logs" element={<A2ALogs />} />
            <Route path="admin/cert-a2a" element={<CertA2ATrigger />} />
            <Route path="admin/logs" element={<Logs />} />
          </Route>
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
    </JobsProvider>
    </QueryClientProvider>
    </ThemeProvider>
  )
}
