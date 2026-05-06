import { Outlet, NavLink, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, AlertTriangle, ArrowLeftRight, Activity,
  Sliders, Settings, ChevronLeft, ChevronRight, Zap,
} from 'lucide-react'
import { useTmsStore } from '../../store'
import { useWebSocket } from '../../lib/websocket'
import styles from './Layout.module.css'
import LiveDot from '../ui/LiveDot'

const NAV = [
  { to: '/dashboard',     label: 'Dashboard',     Icon: LayoutDashboard },
  { to: '/alerts',        label: 'Alerts',         Icon: AlertTriangle },
  { to: '/transactions',  label: 'Transactions',   Icon: ArrowLeftRight },
  { to: '/lifecycle',     label: 'Lifecycle',      Icon: Activity },
]

const NAV_CONFIG = [
  { to: '/detection',     label: 'Detection',      Icon: Sliders },
  { to: '/configuration', label: 'Configuration',  Icon: Settings },
]

export default function Layout() {
  const { sidebarCollapsed, toggleSidebar, pushLiveEvent } = useTmsStore()
  const { status } = useWebSocket({
    enabled: true,
    onEvent: (ev) => {
      if (ev.type === 'lifecycle') pushLiveEvent(ev.data)
    },
  })
  const location = useLocation()

  const pageTitle = [...NAV, ...NAV_CONFIG].find((n) =>
    location.pathname.startsWith(n.to),
  )?.label ?? 'TMS'

  return (
    <div className={styles.root} data-collapsed={sidebarCollapsed}>
      {/* Sidebar */}
      <aside className={styles.sidebar}>
        {/* Logo */}
        <div className={styles.logo}>
          <div className={styles.logoIcon}>
            <Zap size={18} strokeWidth={2.5} />
          </div>
          {!sidebarCollapsed && (
            <div className={styles.logoText}>
              <span className={styles.logoBrand}>Cardano</span>
              <span className={styles.logoSub}>TMS</span>
            </div>
          )}
        </div>

        {/* Navigation */}
        <nav className={styles.nav}>
          <div className={styles.navSection}>
            {!sidebarCollapsed && <span className={styles.navLabel}>Monitor</span>}
            {NAV.map(({ to, label, Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  `${styles.navItem} ${isActive ? styles.navItemActive : ''}`
                }
              >
                <Icon size={16} />
                {!sidebarCollapsed && <span>{label}</span>}
              </NavLink>
            ))}
          </div>

          <div className={styles.navSection}>
            {!sidebarCollapsed && <span className={styles.navLabel}>Configure</span>}
            {NAV_CONFIG.map(({ to, label, Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  `${styles.navItem} ${isActive ? styles.navItemActive : ''}`
                }
              >
                <Icon size={16} />
                {!sidebarCollapsed && <span>{label}</span>}
              </NavLink>
            ))}
          </div>
        </nav>

        {/* Connection status */}
        <div className={styles.statusBar}>
          <LiveDot status={status} />
          {!sidebarCollapsed && (
            <span className={styles.statusText}>
              {status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting…' : 'Offline'}
            </span>
          )}
        </div>

        {/* Collapse toggle */}
        <button
          className={styles.collapseBtn}
          onClick={toggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {sidebarCollapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
      </aside>

      {/* Main */}
      <div className={styles.main}>
        <header className={styles.topbar}>
          <h1 className={styles.pageTitle}>{pageTitle}</h1>
          <div className={styles.topbarRight}>
            <div className={styles.networkPill}>mainnet</div>
          </div>
        </header>

        <main className={styles.content}>
          <div className="page-enter">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
