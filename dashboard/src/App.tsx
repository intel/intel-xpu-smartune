import React, { useState, useCallback, useEffect } from 'react'
import { Tabs, Layout, Typography, Space, Alert, Badge, Button, Dropdown, Empty, Tag, notification, Tooltip, Menu, Avatar } from 'antd'
import type { MenuProps } from 'antd'
import {
  DashboardOutlined,
  HomeOutlined,
  AlertOutlined,
  BellOutlined,
  AppstoreOutlined,
  NodeIndexOutlined,
  ControlOutlined,
  LineChartOutlined,
  InfoCircleOutlined,
  LogoutOutlined,
  SettingOutlined,
  ExperimentOutlined,
  MoonOutlined,
  SunOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MonitorOutlined,
  SlidersOutlined,
  FundOutlined,
  UserOutlined,
  DownOutlined,
} from '@ant-design/icons'
import SettingsModal from './components/SettingsModal'
import SystemOverview from './components/SystemOverview'
import AppResources from './components/AppResources'
import Processes from './components/Processes'
import Balance from './components/Balance'
import Benchmark from './components/Benchmark'
import HistoryDashboard from './components/HistoryDashboard'
import Diagnostics from './components/Diagnostics'
import About from './components/About'
import LoginGate from './components/LoginGate'
import { COLORS } from './styles/theme'
import type { ColorMode } from './styles/theme'
import { api, getToken, clearToken, setUnauthorizedHandler, consumeUrlToken, login } from './api/client'
import { GlobalConfigNoticesProvider, useGlobalConfigNotices } from './hooks/useGlobalConfigNotices'
import { useUiLease } from './hooks/useUiLease'
import { useBenchEvent, useBenchStream } from './hooks/useBenchEvents'
import type { DiagAlert } from './api/types'

const { Header, Content, Sider } = Layout

const BENCHMARK_TAB = 'models'

function HealthDiagnosticsIcon() {
  return (
    <span className="health-diagnostics-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round">
        <path d="M20.5 8.7c0 5.7-8.5 11.1-8.5 11.1S3.5 14.4 3.5 8.7A4.6 4.6 0 0 1 12 6.2a4.6 4.6 0 0 1 8.5 2.5Z" />
        <path d="M5.5 12h3l1.4-2.4 2.1 5 1.8-3h4" />
      </svg>
    </span>
  )
}

type HistoryRangeIntent = {
  from: number
  to: number
}

// What a finished benchmark job is called in a notification. The tab's own
// wording is "environment setup" / "run"; these are the same two things said in
// a sentence that has to make sense out of context.
const BENCH_JOB_LABEL: Record<string, string> = {
  setup: 'Benchmark environment setup',
  run: 'Benchmark run',
}

function GlobalConfigNoticeBar() {
  const { notices, dismissNotice } = useGlobalConfigNotices()

  if (!notices.length) return null

  return (
    <div style={{ marginTop: 12 }}>
      {notices.map((notice) => (
        <Alert
          key={notice.id}
          message={notice.title}
          description={notice.description}
          type="info"
          showIcon
          closable
          onClose={() => dismissNotice(notice.id)}
          style={{ marginBottom: 8 }}
        />
      ))}
    </div>
  )
}

interface AppProps {
  colorMode: ColorMode
  onColorModeChange: (mode: ColorMode) => void
}

export default function App({ colorMode, onColorModeChange }: AppProps) {
  const [activeTab, setActiveTab] = useState('overview')
  const [workloadView, setWorkloadView] = useState('resources')
  const [settingsSection, setSettingsSection] = useState<string | null>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [historyRangeIntent, setHistoryRangeIntent] = useState<HistoryRangeIntent | null>(null)
  // 1 = balancer + monitor, 0 = monitor only. Default to enabled so older
  // servers without the /smartune/capabilities endpoint keep full behaviour.
  const [balancerEnabled, setBalancerEnabled] = useState(true)
  // Whether this server mounts the /bench API. Defaults to false (unlike the
  // balancer above): an older server that omits the field has no /bench routes,
  // so showing the tab would give a page that 404s on every request.
  const [benchmarkEnabled, setBenchmarkEnabled] = useState(false)
  const [diagnosticsEnabled, setDiagnosticsEnabled] = useState(false)
  // Set from the Processes tab's "Add to balancer" action; consumed by the Balance tab.
  const [registerKeyword, setRegisterKeyword] = useState<string | null>(null)
  // Gate the whole app behind a valid access token. A stored token is assumed
  // valid until the server rejects a request with 401 (handled below).
  const [authed, setAuthed] = useState(() => !!getToken())
  const [activeAlertCount, setActiveAlertCount] = useState(0)
  const [activeAlerts, setActiveAlerts] = useState<DiagAlert[]>([])
  const [diagnosticsOpenAlertsSignal, setDiagnosticsOpenAlertsSignal] = useState(0)
  // While a bootstrap token from the URL hash (desktop launcher) is being
  // validated, hold rendering so the login gate does not flash before we know
  // whether the auto-login succeeds. Only blocks when such a token is present.
  const [booting, setBooting] = useState(() => /[#&]token=/.test(window.location.hash))

  // Consume a one-shot token passed in the URL hash by the desktop launcher and
  // auto-login with it, so clicking the desktop icon lands straight in the app.
  useEffect(() => {
    if (!booting) return
    const urlToken = consumeUrlToken()
    if (!urlToken) {
      setBooting(false)
      return
    }
    login(urlToken)
      .then((ok) => {
        if (ok) setAuthed(true)
      })
      .catch(() => {})
      .finally(() => setBooting(false))
  }, [booting])

  // Any 401 (expired/revoked/invalid token) drops us back to the login gate.
  useEffect(() => {
    setUnauthorizedHandler(() => setAuthed(false))
    return () => setUnauthorizedHandler(null)
  }, [])

  useEffect(() => {
    if (!authed) return
    api
      .getCapabilities()
      .then((c) => {
        setBalancerEnabled(c.capabilities === 1)
        // Absent on older servers, which never served /bench at all.
        setBenchmarkEnabled(c.benchmark === 1)
        setDiagnosticsEnabled(c.diagnostics === 1)
      })
      .catch(() => {
        setBalancerEnabled(true)
        setDiagnosticsEnabled(false)
      })
  }, [authed])

  useEffect(() => {
    if (!authed || !diagnosticsEnabled) return undefined
    let cancelled = false
    const loadAlerts = () => {
      api.getDiagAlerts(true, 100, true)
        .then((data) => {
          if (!cancelled) {
            setActiveAlerts(data.alerts || [])
            setActiveAlertCount(data.alerts?.length || 0)
          }
        })
        .catch(() => {})
    }
    loadAlerts()
    const timer = window.setInterval(loadAlerts, 30000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [authed, diagnosticsEnabled])

  // Hold an open-UI lease while logged in so the packaged monitor can stop
  // itself once the last dashboard tab is closed. No-op against a server that
  // doesn't arm the watchdog (dev / balancer).
  useUiLease(authed)

  // The benchmark event stream is owned here rather than by the tab: a setup
  // takes an hour and a run can take longer, so the whole point is that the user
  // goes elsewhere and is told when it is done. Log deltas are only requested
  // while the tab is actually on screen -- see benchEventsUrl.
  const benchTabActive = activeTab === BENCHMARK_TAB
  useBenchStream(authed && benchmarkEnabled, benchTabActive)

  const [notify, notifyHolder] = notification.useNotification()

  useBenchEvent(
    useCallback(
      (event) => {
        // Only worth interrupting for when they cannot already see it happening.
        if (event.type !== 'job' || event.job.status === 'running' || benchTabActive) return
        const label = BENCH_JOB_LABEL[event.job.kind] ?? 'Benchmark job'
        const done = event.job.status === 'done'
        const key = `bench-job-${event.job.id}`
        notify[done ? 'success' : 'warning']({
          key,
          message: `${label} ${event.job.status}`,
          description: done
            ? 'The results are ready in the Models tab.'
            : `Exit code ${event.job.returncode ?? 'unknown'}. The log is in the Models tab.`,
          duration: 0, // they were not looking; let them find it in their own time
          btn: (
            <Button
              type="primary"
              size="small"
              onClick={() => {
                setActiveTab(BENCHMARK_TAB)
                notify.destroy(key)
              }}
            >
              Open Models
            </Button>
          ),
        })
      },
      [benchTabActive, notify],
    ),
    authed && benchmarkEnabled,
  )

  const handleLogout = useCallback(() => {
    clearToken()
    setAuthed(false)
  }, [])

  const openCurrentAlerts = useCallback(() => {
    setActiveTab('diagnostics')
    setDiagnosticsOpenAlertsSignal((signal) => signal + 1)
  }, [])

  const formatAlertTime = useCallback((value: string) => {
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    })
  }, [])

  const alertColor = useCallback((severity: string) => {
    if (severity === 'critical') return COLORS.red
    if (severity === 'error') return COLORS.orange
    if (severity === 'warning') return COLORS.yellow
    return COLORS.accent
  }, [])

  const alertCenter = (
    <div className="app-alert-center">
      <div className="app-alert-center__header">
        <Typography.Text strong style={{ color: COLORS.text }}>Alerts</Typography.Text>
        <Typography.Text className="app-alert-center__count">{activeAlertCount} need attention</Typography.Text>
      </div>
      {activeAlerts.length ? (
        <div className="app-alert-center__list">
          {activeAlerts.slice(0, 5).map((alert) => (
            <button
              key={alert.dedup_key}
              type="button"
              onClick={openCurrentAlerts}
              className="app-alert-center__item"
            >
              <div className="app-alert-center__item-title">
                <Tag color={alertColor(alert.severity)} style={{ marginInlineEnd: 0, textTransform: 'uppercase' }}>{alert.severity}</Tag>
                <Typography.Text ellipsis style={{ color: COLORS.text, flex: 1, minWidth: 0, fontSize: 13 }}>
                  {alert.summary || alert.event_type}
                </Typography.Text>
              </div>
              <Typography.Text className="app-alert-center__item-meta">
                {alert.scope || 'System'} · Latest {formatAlertTime(alert.last_fired_at)}
              </Typography.Text>
            </button>
          ))}
        </div>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No alerts need attention" style={{ margin: '20px 0' }} />
      )}
      <div className="app-alert-center__footer">
        <Button type="link" size="small" onClick={openCurrentAlerts} style={{ padding: 0 }}>
          Open alert history
        </Button>
      </div>
    </div>
  )

  // Publish the sticky application header height so page-level toolbars can
  // stay below it in the content workspace.
  useEffect(() => {
    const measure = () => {
      const header = document.querySelector('.ant-layout-header') as HTMLElement | null
      const offset = header?.offsetHeight ?? 64
      document.documentElement.style.setProperty('--app-sticky-top', `${offset}px`)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  const tabs = [
    {
      key: 'overview',
      label: 'Home',
      children: <SystemOverview active={activeTab === 'overview'} />,
    },
    {
      key: 'workloads',
      label: 'Applications',
      children: (
        <div className="workload-page">
          <div className="workspace-page-heading">
            <div>
              <Typography.Title level={3}>Applications</Typography.Title>
              <Typography.Text type="secondary">Review application resource use and manage active processes.</Typography.Text>
            </div>
          </div>
          <Tabs
            className="workload-tabs"
            activeKey={workloadView}
            onChange={setWorkloadView}
            items={[
              {
                key: 'resources',
                label: 'Resource consumers',
                children: (
                  <AppResources
                    active={activeTab === 'workloads' && workloadView === 'resources'}
                    balancerEnabled={balancerEnabled}
                    onRegister={balancerEnabled ? (name) => {
                      setRegisterKeyword(name)
                      setActiveTab('optimizer')
                    } : undefined}
                  />
                ),
              },
              {
                key: 'processes',
                label: 'Processes',
                children: (
                  <Processes
                    active={activeTab === 'workloads' && workloadView === 'processes'}
                    balancerEnabled={balancerEnabled}
                    onRegister={balancerEnabled ? (name) => {
                      setRegisterKeyword(name)
                      setActiveTab('optimizer')
                    } : undefined}
                  />
                ),
              },
            ]}
          />
        </div>
      ),
    },
    ...(diagnosticsEnabled
      ? [
          {
            key: 'diagnostics',
            label: (
              <Space>
                <AlertOutlined />
                Diagnostics
              </Space>
            ),
            children: (
              <Diagnostics
                active={activeTab === 'diagnostics'}
                openAlertsSignal={diagnosticsOpenAlertsSignal}
                onOpenHistory={(range) => {
                  setHistoryRangeIntent(range)
                  setActiveTab('history')
                }}
              />
            ),
          },
        ]
      : []),
    {
      key: 'history',
      label: (
        <Space>
          <LineChartOutlined />
          History
        </Space>
      ),
      children: (
        <HistoryDashboard
          active={activeTab === 'history'}
          historyRangeIntent={historyRangeIntent}
          onHistoryRangeIntentConsumed={() => setHistoryRangeIntent(null)}
        />
      ),
    },
    // Balancer tab is only shown when the server supports balancing; in
    // monitor-only mode it is omitted entirely rather than shown as disabled.
    ...(balancerEnabled
      ? [
          {
            key: 'optimizer',
            label: (
              <Space>
                <ControlOutlined />
                Balancer
              </Space>
            ),
            children: (
              <Balance
                active={activeTab === 'optimizer'}
                balancerEnabled={balancerEnabled}
                registerKeyword={registerKeyword}
                onRegisterConsumed={() => setRegisterKeyword(null)}
              />
            ),
          },
        ]
      : []),
    // Same conditional treatment as the Balancer tab: a server without the
    // benchmark feature omits the tab rather than showing a dead one.
    ...(benchmarkEnabled
      ? [
          {
            key: BENCHMARK_TAB,
            label: (
              <Space>
                <ExperimentOutlined />
                Models
              </Space>
            ),
            // No `active` prop: the tab keeps itself current off the event
            // stream owned above, so switching away and back costs nothing.
            //
            // onOpenBalance is how a preflight block becomes actionable: the
            // apps holding a run back are released on the Balancer page, and in
            // monitor-only mode there is no such page (and, equally, no
            // balancer to block a run), so the prop is left off.
            children: (
              <Benchmark
                onOpenBalance={balancerEnabled ? () => setActiveTab('optimizer') : undefined}
              />
            ),
          },
        ]
      : []),
    {
      key: 'about',
      label: (
        <Space>
          <InfoCircleOutlined />
          About
        </Space>
      ),
      children: <About active={activeTab === 'about'} />,
    },
    {
      key: 'settings',
      label: 'Settings',
      children: (
        <SettingsModal
          active={activeTab === 'settings'}
          balancerEnabled={balancerEnabled}
          onActiveTabChange={setSettingsSection}
        />
      ),
    },
  ]

  const navigationItems: MenuProps['items'] = [
    { key: 'overview', icon: <HomeOutlined />, label: 'Home' },
    { key: 'workloads', icon: <AppstoreOutlined />, label: 'Applications' },
    { key: 'history', icon: <LineChartOutlined />, label: 'History' },
    ...(balancerEnabled ? [{ key: 'optimizer', icon: <SlidersOutlined />, label: 'Control center' }] : []),
    ...(benchmarkEnabled ? [{ key: BENCHMARK_TAB, icon: <ExperimentOutlined />, label: 'Models' }] : []),
    ...(diagnosticsEnabled ? [{ key: 'diagnostics', icon: <HealthDiagnosticsIcon />, label: 'System diagnostics' }] : []),
    { key: 'settings', icon: <SettingOutlined />, label: 'Settings' },
    { key: 'about', icon: <InfoCircleOutlined />, label: 'About' },
  ]

  const pageTitles: Record<string, string> = {
    overview: 'Home',
    workloads: 'Applications',
    diagnostics: 'System diagnostics',
    history: 'History',
    optimizer: 'Control center',
    [BENCHMARK_TAB]: 'Models',
    about: 'About',
    settings: 'Settings',
  }

  // Validating a URL-hash bootstrap token — hold off rendering the gate.
  if (booting) {
    return <div style={{ minHeight: '100vh', background: COLORS.bg }} />
  }

  if (!authed) {
    return <LoginGate onAuthenticated={() => setAuthed(true)} />
  }

  return (
    <GlobalConfigNoticesProvider>
      {notifyHolder}
      <Layout className="app-shell" style={{ minHeight: '100vh', background: COLORS.bg }}>
        <Sider
          className="app-sidebar"
          width={264}
          collapsedWidth={64}
          collapsible
          collapsed={sidebarCollapsed}
          trigger={null}
          onCollapse={setSidebarCollapsed}
          theme={colorMode}
          style={{
            background: COLORS.panelBg,
            borderRight: `1px solid ${COLORS.border}`,
            top: 0,
            height: '100vh',
            overflow: 'auto',
          }}
        >
          <div className="app-sidebar__brand">
            <div
              style={{
                width: 28,
                height: 28,
                background: `linear-gradient(135deg, ${COLORS.accent} 0%, #3a6fd8 100%)`,
                borderRadius: 6,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <DashboardOutlined style={{ color: '#fff', fontSize: 14 }} />
            </div>
            <Typography.Title
              level={4}
              style={{ color: COLORS.text, margin: 0, fontWeight: 600 }}
            >
              Intel XPU SmarTune
            </Typography.Title>
          </div>
          <Menu
            mode="inline"
            selectedKeys={[activeTab]}
            items={navigationItems}
            onClick={({ key }) => {
              setActiveTab(key)
            }}
          />
        </Sider>
        <Layout className="app-main" style={{ background: COLORS.bg }}>
          <Header
            className="app-header"
            style={{
              background: COLORS.headerBg,
              borderBottom: `1px solid ${COLORS.border}`,
              position: 'sticky',
              top: 0,
              zIndex: 100,
            }}
          >
            <div className="app-header__breadcrumb">
              <Tooltip title={sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'}>
                <Button
                  type="text"
                  size="small"
                  icon={sidebarCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                  aria-label={sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'}
                  onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
                />
              </Tooltip>
              <DashboardOutlined />
              <Typography.Text>Intel XPU SmarTune</Typography.Text>
              <span>/</span>
              <Typography.Text strong>{pageTitles[activeTab] ?? 'Home'}</Typography.Text>
              {activeTab === 'settings' && settingsSection ? (
                <>
                  <span>/</span>
                  <Typography.Text strong>{settingsSection}</Typography.Text>
                </>
              ) : null}
            </div>
            <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
            <Tooltip title={colorMode === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}>
              <Button
                type="text"
                size="small"
                icon={colorMode === 'light' ? <MoonOutlined /> : <SunOutlined />}
                aria-label={colorMode === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
                onClick={() => onColorModeChange(colorMode === 'light' ? 'dark' : 'light')}
                style={{ color: COLORS.textMuted }}
              />
            </Tooltip>
            {diagnosticsEnabled ? (
              <Dropdown
                trigger={['click']}
                dropdownRender={() => alertCenter}
                overlayStyle={{ paddingTop: 6 }}
              >
                <Button
                  type="text"
                  size="small"
                  aria-label="Open alerts"
                  style={{ color: COLORS.textMuted }}
                >
                  <Badge count={activeAlertCount} size="small" offset={[2, -1]}>
                    <BellOutlined style={{ color: COLORS.textMuted }} />
                  </Badge>
                </Button>
              </Dropdown>
            ) : null}
            <Dropdown
              trigger={['click']}
              menu={{
                items: [
                  {
                    key: 'sign-out',
                    icon: <LogoutOutlined />,
                    label: 'Sign out',
                    onClick: handleLogout,
                  },
                ],
              }}
            >
              <Button type="text" size="small" className="app-account-button" aria-label="Open account menu">
                <Avatar size="small" icon={<UserOutlined />} />
                <DownOutlined />
              </Button>
            </Dropdown>
            </div>
          </Header>

          <Content className="app-workspace" style={{ background: COLORS.bg }}>
            <GlobalConfigNoticeBar />
            <div className="app-content-area">
              <Tabs
                className="app-content-tabs"
                activeKey={activeTab}
                onChange={setActiveTab}
                items={tabs}
                renderTabBar={() => <div />}
              />
            </div>
          </Content>
        </Layout>
      </Layout>
    </GlobalConfigNoticesProvider>
  )
}
