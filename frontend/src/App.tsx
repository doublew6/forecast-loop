import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router'

import { AppShell } from './components/AppShell'
import { LoadingPanel } from './components/Common'

const Dashboard = lazy(() => import('./pages/Dashboard').then((module) => ({ default: module.Dashboard })))
const Evaluations = lazy(() => import('./pages/Evaluations').then((module) => ({ default: module.Evaluations })))
const MeetingDetail = lazy(() => import('./pages/MeetingDetail').then((module) => ({ default: module.MeetingDetail })))
const Observability = lazy(() => import('./pages/Observability').then((module) => ({ default: module.Observability })))
const Reflections = lazy(() => import('./pages/Reflections').then((module) => ({ default: module.Reflections })))
const Runs = lazy(() => import('./pages/Runs').then((module) => ({ default: module.Runs })))
const Scorecards = lazy(() => import('./pages/Scorecards').then((module) => ({ default: module.Scorecards })))
const TraceDetail = lazy(() => import('./pages/TraceDetail').then((module) => ({ default: module.TraceDetail })))
const UserJudgments = lazy(() => import('./pages/UserJudgments').then((module) => ({ default: module.UserJudgments })))
const Wiki = lazy(() => import('./pages/Wiki').then((module) => ({ default: module.Wiki })))

export function App() {
  return (
    <Suspense fallback={<LoadingPanel />}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Dashboard />} />
          <Route path="meeting" element={<MeetingDetail />} />
          <Route path="meeting/:runId" element={<MeetingDetail />} />
          <Route path="judgments" element={<UserJudgments />} />
          <Route path="reflections" element={<Reflections />} />
          <Route path="evaluations" element={<Evaluations />} />
          <Route path="observability" element={<Observability />} />
          <Route path="traces/:traceId" element={<TraceDetail />} />
          <Route path="scorecards" element={<Scorecards />} />
          <Route path="wiki" element={<Wiki />} />
          <Route path="runs" element={<Runs />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  )
}
