import React, { useState, useCallback, useEffect } from 'react'
import { Tabs, Layout, Typography, Space, Alert, Button, notification } from 'antd'
import {
  DashboardOutlined,
  AppstoreOutlined,
  NodeIndexOutlined,
  ControlOutlined,
  LineChartOutlined,
  InfoCircleOutlined,
  LogoutOutlined,
  SettingOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import SettingsModal from './components/SettingsModal'
import SystemOverview from './components/SystemOverview'
import AppResources from './components/AppResources'
import Processes from './components/Processes'
import Balance from './components/Balance'
import Benchmark from './components/Benchmark'
import HistoryDashboard from './components/HistoryDashboard'
import About from './components/About'
import LoginGate from './components/LoginGate'
import { COLORS } from './styles/theme'
import { api, getToken, clearToken, setUnauthorizedHandler } from './api/client'
import { GlobalConfigNoticesProvider, useGlobalConfigNotices } from './hooks/useGlobalConfigNotices'
import { useBenchEvent, useBenchStream } from './hooks/useBenchEvents'

const { Header, Content } = Layout

const BENCHMARK_TAB = '7'

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

export default function App() {
  const [activeTab, setActiveTab] = useState('1')
  // 1 = balancer + monitor, 0 = monitor only. Default to enabled so older
  // servers without the /smartune/capabilities endpoint keep full behaviour.
  const [balancerEnabled, setBalancerEnabled] = useState(true)
  // Whether this server mounts the /bench API. Defaults to false (unlike the
  // balancer above): an older server that omits the field has no /bench routes,
  // so showing the tab would give a page that 404s on every request.
  const [benchmarkEnabled, setBenchmarkEnabled] = useState(false)
  // Set from the Processes tab's "Add to balancer" action; consumed by the Balance tab.
  const [registerKeyword, setRegisterKeyword] = useState<string | null>(null)
  // Gate the whole app behind a valid access token. A stored token is assumed
  // valid until the server rejects a request with 401 (handled below).
  const [authed, setAuthed] = useState(() => !!getToken())
  const [settingsOpen, setSettingsOpen] = useState(false)

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
      })
      .catch(() => setBalancerEnabled(true))
  }, [authed])

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

  // Publish the combined height of the sticky header + sticky tab bar as a CSS
  // variable so per-page sticky toolbars can pin themselves *below* the tab bar
  // instead of colliding with it (both would otherwise stick at top:64 and the
  // toolbar, having a lower z-index, would hide behind the tabs).
  useEffect(() => {
    const measure = () => {
      const header = document.querySelector('.ant-layout-header') as HTMLElement | null
      const nav = document.querySelector('.ant-tabs-nav') as HTMLElement | null
      const offset = (header?.offsetHeight ?? 64) + (nav?.offsetHeight ?? 0)
      document.documentElement.style.setProperty('--app-sticky-top', `${offset}px`)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  const tabs = [
    {
      key: '1',
      label: (
        <Space>
          <DashboardOutlined />
          System Overview
        </Space>
      ),
      children: <SystemOverview active={activeTab === '1'} />,
    },
    {
      key: '2',
      label: (
        <Space>
          <AppstoreOutlined />
          App Resources
        </Space>
      ),
      children: (
        <AppResources
          active={activeTab === '2'}
          balancerEnabled={balancerEnabled}
          onRegister={
            balancerEnabled
              ? (name) => {
                  setRegisterKeyword(name)
                  setActiveTab('5')
                }
              : undefined
          }
        />
      ),
    },
    {
      key: '3',
      label: (
        <Space>
          <NodeIndexOutlined />
          Processes
        </Space>
      ),
      children: (
        <Processes
          active={activeTab === '3'}
          balancerEnabled={balancerEnabled}
          onRegister={
            balancerEnabled
              ? (name) => {
                  setRegisterKeyword(name)
                  setActiveTab('5')
                }
              : undefined
          }
        />
      ),
    },
    {
      key: '4',
      label: (
        <Space>
          <LineChartOutlined />
          History
        </Space>
      ),
      children: <HistoryDashboard active={activeTab === '4'} />,
    },
    // Balancer tab is only shown when the server supports balancing; in
    // monitor-only mode it is omitted entirely rather than shown as disabled.
    ...(balancerEnabled
      ? [
          {
            key: '5',
            label: (
              <Space>
                <ControlOutlined />
                Balancer
              </Space>
            ),
            children: (
              <Balance
                active={activeTab === '5'}
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
            children: <Benchmark />,
          },
        ]
      : []),
    {
      key: '6',
      label: (
        <Space>
          <InfoCircleOutlined />
          About
        </Space>
      ),
      children: <About active={activeTab === '6'} />,
    },
  ]

  if (!authed) {
    return <LoginGate onAuthenticated={() => setAuthed(true)} />
  }

  return (
    <GlobalConfigNoticesProvider>
      {notifyHolder}
      <Layout style={{ minHeight: '100vh', background: COLORS.bg }}>
        <Header
          style={{
            background: COLORS.headerBg,
            borderBottom: `1px solid ${COLORS.border}`,
            padding: '0 24px',
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            position: 'sticky',
            top: 0,
            zIndex: 100,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div
              style={{
                width: 32,
                height: 32,
                background: `linear-gradient(135deg, ${COLORS.accent} 0%, #3a6fd8 100%)`,
                borderRadius: 6,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <DashboardOutlined style={{ color: '#fff', fontSize: 16 }} />
            </div>
            <Typography.Title
              level={4}
              style={{ color: COLORS.text, margin: 0, fontWeight: 600 }}
            >
              Intel XPU SmarTune
            </Typography.Title>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
            <Button
              type="text"
              size="small"
              icon={<SettingOutlined />}
              onClick={() => setSettingsOpen(true)}
              style={{ color: COLORS.textMuted }}
            >
              Settings
            </Button>
            <Button
              type="text"
              size="small"
              icon={<LogoutOutlined />}
              onClick={handleLogout}
              style={{ color: COLORS.textMuted }}
            >
              Sign out
            </Button>
          </div>
        </Header>

        <Content style={{ padding: '0 16px 16px', background: COLORS.bg }}>
          <GlobalConfigNoticeBar />
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            items={tabs}
            size="large"
            style={{ color: COLORS.text }}
            tabBarStyle={{
              marginBottom: 0,
              paddingTop: 8,
              background: COLORS.bg,
              borderBottom: `1px solid ${COLORS.border}`,
              position: 'sticky',
              top: 64,
              zIndex: 99,
            }}
          />
        </Content>
        <SettingsModal
          visible={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          balancerEnabled={balancerEnabled}
        />
      </Layout>
    </GlobalConfigNoticesProvider>
  )
}
