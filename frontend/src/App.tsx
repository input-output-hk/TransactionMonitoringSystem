import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Alerts from './pages/Alerts'
import Transactions from './pages/Transactions'
import TransactionDetail from './pages/TransactionDetail'
import Detection from './pages/Detection'
import Configuration from './pages/Configuration'
import Lifecycle from './pages/Lifecycle'

export default function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="alerts" element={<Alerts />} />
          <Route path="transactions" element={<Transactions />} />
          <Route path="transactions/:hash" element={<TransactionDetail />} />
          <Route path="lifecycle" element={<Lifecycle />} />
          <Route path="detection" element={<Detection />} />
          <Route path="configuration" element={<Configuration />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
