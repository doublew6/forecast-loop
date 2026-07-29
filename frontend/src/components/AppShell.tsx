import {
  BarChart3,
  BookOpenText,
  Gauge,
  History,
  Lightbulb,
  Menu,
  Network,
  NotebookPen,
  X,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'

const navigationGroups = [
  {
    label: '预测',
    items: [
      { to: '/', label: '决策总览', icon: Gauge, end: true },
      { to: '/meeting', label: '投委会详情', icon: Network },
    ],
  },
  {
    label: '参与',
    items: [
      { to: '/judgments', label: '我的判断', icon: NotebookPen },
    ],
  },
  {
    label: '验证与学习',
    items: [
      { to: '/scorecards', label: '历史成绩', icon: BarChart3 },
      { to: '/reflections', label: '每日反省', icon: Lightbulb },
      { to: '/wiki', label: '验证知识库', icon: BookOpenText },
    ],
  },
  {
    label: '系统',
    items: [
      { to: '/runs', label: '运行记录', icon: History },
    ],
  },
]

const navigation = navigationGroups.flatMap((group) => group.items)

function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">
          <span>F</span>
        </div>
        <div>
          <div className="brand-name">forecast-loop</div>
          <div className="brand-subtitle">可验证预测 Agent 框架</div>
        </div>
      </div>

      <nav className="primary-nav" aria-label="主要导航">
        {navigationGroups.map((group) => (
          <div className="nav-group" key={group.label}>
            <div className="nav-eyebrow">{group.label}</div>
            {group.items.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                onClick={onNavigate}
                className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
              >
                <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
                <strong>{label}</strong>
              </NavLink>
            ))}
          </div>
        ))}
      </nav>

      <div className="sidebar-foot">
        <div className="loop-signature">
          <div className="loop-signature-title">
            <strong>Forecast loop</strong>
            <span>一条可复验的预测生命线</span>
          </div>
          <ol aria-label="预测闭环">
            {['预测', '解释', '结算', '学习'].map((step, index) => (
              <li key={step}>
                <i aria-hidden="true">{index + 1}</i>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </div>
        <div className="version-row">
          <span><i className="status-dot" /> 本地研究模式</span>
          <span>v0.1</span>
        </div>
      </div>
    </aside>
  )
}

export function AppShell() {
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButtonRef = useRef<HTMLButtonElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const location = useLocation()
  const current = navigation.find((item) =>
    item.end ? location.pathname === item.to : location.pathname.startsWith(item.to),
  )

  useEffect(() => {
    if (!menuOpen) return

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeButtonRef.current?.focus()
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setMenuOpen(false)
      menuButtonRef.current?.focus()
    }
    window.addEventListener('keydown', closeOnEscape)

    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', closeOnEscape)
    }
  }, [menuOpen])

  return (
    <div className="app-shell">
      <div className={`mobile-drawer${menuOpen ? ' open' : ''}`} aria-hidden={!menuOpen}>
        <button className="drawer-backdrop" aria-label="关闭菜单" onClick={() => setMenuOpen(false)} />
        <div className="drawer-panel" role="dialog" aria-modal="true" aria-label="站点导航">
          <button
            ref={closeButtonRef}
            className="drawer-close"
            aria-label="关闭菜单"
            onClick={() => {
              setMenuOpen(false)
              menuButtonRef.current?.focus()
            }}
          >
            <X size={20} />
          </button>
          <Sidebar onNavigate={() => setMenuOpen(false)} />
        </div>
      </div>

      <div className="desktop-sidebar"><Sidebar /></div>
      <div className="content-column">
        <header className="mobile-header">
          <button
            ref={menuButtonRef}
            className="icon-button"
            aria-label="打开菜单"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(true)}
          >
            <Menu size={20} />
          </button>
          <span>{current?.label ?? 'forecast-loop'}</span>
          <div className="mobile-brand-mark">F</div>
        </header>
        <main className="main-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
