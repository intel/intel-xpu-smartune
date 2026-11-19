import React, { useState, useCallback, useEffect, useMemo } from 'react'
import { Tabs, Layout, Typography, Space, Alert, Badge, Button, Dropdown, Drawer, Empty, Tag, message, notification, Tooltip, Menu, Avatar, Modal } from 'antd'
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
  ExportOutlined,
  PoweroffOutlined,
} from '@ant-design/icons'
import SettingsModal from './components/SettingsModal'
import SystemOverview from './components/SystemOverview'
import AppResources from './components/AppResources'
import Processes from './components/Processes'
import ControlCenter from './components/ControlCenter'
import Benchmark from './components/Benchmark'
import HistoryDashboard from './components/HistoryDashboard'
import Diagnostics from './components/Diagnostics'
import type { AlertsRequest } from './components/Diagnostics'
import About from './components/About'
import LoginGate from './components/LoginGate'
import { COLORS } from './styles/theme'
import type { ColorMode } from './styles/theme'
import { api, getToken, clearToken, setUnauthorizedHandler, consumeUrlToken, login, haltRequests } from './api/client'
import { suspendStreams } from './events/sseHub'
import { GlobalConfigNoticesProvider, useGlobalConfigNotices } from './hooks/useGlobalConfigNotices'
import { useBenchEvent, useBenchStream } from './hooks/useBenchEvents'
import { useAppEvents } from './hooks/useAppEvents'
import type { DiagAlert } from './api/types'
import { alertHandlingMark } from './utils/alertPolicy'
import type { AlertHandlingMark } from './utils/alertPolicy'
import { useIsNarrowViewport } from './hooks/useIsNarrowViewport'
import { useContentWidth } from './hooks/useContentWidth'
import { WorkspaceWidthContext } from './hooks/useWorkspaceWidth'

const { Header, Content, Sider } = Layout

const BENCHMARK_TAB = 'models'

// Tells every dashboard tab of this origin that the service was stopped.
const SERVICE_CHANNEL = 'smartune-service'

// The header alert center shows only the first few alerts needing attention;
// the rest are one click away in Diagnostics.
const ALERT_CENTER_MAX_ITEMS = 5
const ALERT_SEVERITY_ORDER = ['critical', 'error', 'warning', 'info']

function alertSeverityRank(severity: string): number {
  const index = ALERT_SEVERITY_ORDER.indexOf(severity)
  return index === -1 ? ALERT_SEVERITY_ORDER.length : index
}

function HealthDiagnosticsIcon({ className }: { className?: string }) {
  return (
    <span className={[className, 'health-diagnostics-icon'].filter(Boolean).join(' ')} aria-hidden="true">
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
  const [alertCenterOpen, setAlertCenterOpen] = useState(false)
  const [alertCenterExpanded, setAlertCenterExpanded] = useState(false)
  const [workloadView, setWorkloadView] = useState('resources')
  const [settingsSection, setSettingsSection] = useState<string | null>(null)
  const [settingsTabIntent, setSettingsTabIntent] = useState<{ key: string; requestId: number } | null>(null)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  // A narrow viewport has no room for the sider: navigation moves to a drawer.
  const narrowViewport = useIsNarrowViewport()
  const [navDrawerOpen, setNavDrawerOpen] = useState(false)
  const [workspaceRef, workspaceWidth] = useContentWidth<HTMLDivElement>()
  const [historyRangeIntent, setHistoryRangeIntent] = useState<HistoryRangeIntent | null>(null)
  // 1 = balancer + monitor, 0 = monitor only. Default to enabled so older
  // servers without the /smartune/capabilities endpoint keep full behaviour.
  const [balancerEnabled, setBalancerEnabled] = useState(true)
  // Whether this server mounts the /bench API. Defaults to false (unlike the
  // balancer above): an older server that omits the field has no /bench routes,
  // so showing the tab would give a page that 404s on every request.
  const [benchmarkEnabled, setBenchmarkEnabled] = useState(false)
  const [diagnosticsEnabled, setDiagnosticsEnabled] = useState(false)
  // Whether this server lets the dashboard stop it (packaged deployments only).
  const [shutdownEnabled, setShutdownEnabled] = useState(false)
  // Set from the Processes tab's "Add to balancer" action; consumed by the Balance tab.
  const [registerKeyword, setRegisterKeyword] = useState<string | null>(null)
  // Gate the whole app behind a valid access token. A stored token is assumed
  // valid until the server rejects a request with 401 (handled below).
  const [authed, setAuthed] = useState(() => !!getToken())
  // The header alert center is the one place for current (active) alerts.
  const [currentAlerts, setCurrentAlerts] = useState<DiagAlert[]>([])
  const [diagnosticsAlertsRequest, setDiagnosticsAlertsRequest] = useState<AlertsRequest | null>(null)
  const [alertCenterActionKey, setAlertCenterActionKey] = useState<string | null>(null)
  const [diagnosticsRangeIntent, setDiagnosticsRangeIntent] = useState<{ window: '1h'; requestId: number } | null>(null)
  // While a bootstrap token from the URL hash (desktop launcher) is being
  // validated, hold rendering so the login gate does not flash before we know
  // whether the auto-login succeeds. Only blocks when such a token is present.
  const [booting, setBooting] = useState(() => /[#&]token=/.test(window.location.hash))
  // When the service was stopped from this or another tab (null while running).
  // The page stays on screen, frozen, under a notice: everything that talks to
  // the server keys off `live`, and requests and streams are halted below.
  const [stoppedAt, setStoppedAt] = useState<string | null>(null)
  const serviceStopped = stoppedAt !== null
  const live = authed && !serviceStopped
  const markServiceStopped = useCallback(() => {
    setStoppedAt((prev) => prev ?? new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }))
  }, [])

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
    if (!live) return
    api
      .getCapabilities()
      .then((c) => {
        setBalancerEnabled(c.capabilities === 1)
        // Absent on older servers, which never served /bench at all.
        setBenchmarkEnabled(c.benchmark === 1)
        setDiagnosticsEnabled(c.diagnostics === 1)
        setShutdownEnabled(c.shutdown === 1)
      })
      .catch(() => {
        setBalancerEnabled(true)
        setDiagnosticsEnabled(false)
      })
  }, [live])

  const loadAlerts = useCallback(() => {
    api.getDiagAlerts(true, 200)
      .then((data) => setCurrentAlerts(data.alerts || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!live || !diagnosticsEnabled) return undefined
    loadAlerts()
    const timer = window.setInterval(loadAlerts, 30000)
    return () => window.clearInterval(timer)
  }, [live, diagnosticsEnabled, loadAlerts])

  const alertCenterGroups = useMemo(() => {
    const groups: Record<AlertHandlingMark, DiagAlert[]> = { needs_attention: [], acknowledged: [], silenced: [] }
    for (const alert of currentAlerts) groups[alertHandlingMark(alert)].push(alert)
    // Most severe first; the server order (latest fire first) breaks ties.
    groups.needs_attention.sort((a, b) => alertSeverityRank(a.severity) - alertSeverityRank(b.severity))
    return groups
  }, [currentAlerts])
  const needsAttentionCount = alertCenterGroups.needs_attention.length

  // The benchmark event stream is owned here rather than by the tab: a setup
  // takes an hour and a run can take longer, so the whole point is that the user
  // goes elsewhere and is told when it is done. Log deltas are only requested
  // while the tab is actually on screen -- see benchEventsUrl.
  const benchTabActive = activeTab === BENCHMARK_TAB
  useBenchStream(live && benchmarkEnabled, benchTabActive)

  const [notify, notifyHolder] = notification.useNotification()
  // Hook form so the confirmation follows the app's light/dark theme.
  const [modal, modalHolder] = Modal.useModal()

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
    live && benchmarkEnabled,
  )

  // A freeze the user forgot silently starves the app, so the reminder must reach them
  // on any tab, not only in Control Center (whose own stream is subscribed only while
  // that tab is open).
  useAppEvents(
    useCallback(
      (event) => {
        if (event.purpose !== 'notify' || event.status !== 'app_frozen_long') return
        notify.warning({
          key: `frozen-long-${event.app_id}`,
          message: `${event.app_name} is still frozen`,
          description: 'It has been frozen for a while. Unfreeze it in Control Center if it is no longer needed.',
          duration: 0,
          btn: (
            <Button
              type="primary"
              size="small"
              onClick={() => {
                setActiveTab('optimizer')
                notify.destroy(`frozen-long-${event.app_id}`)
              }}
            >
              Open Control Center
            </Button>
          ),
        })
      },
      [notify],
    ),
    live,
  )

  const handleLogout = useCallback(() => {
    clearToken()
    setAuthed(false)
  }, [])

  // A stop from any tab ends the session in all of them (each tab of this
  // origin listens), so none is left polling a server that is gone.
  useEffect(() => {
    if (typeof BroadcastChannel === 'undefined') return undefined
    const channel = new BroadcastChannel(SERVICE_CHANNEL)
    channel.onmessage = (event: MessageEvent<{ type?: string }>) => {
      if (event.data?.type === 'stopped') markServiceStopped()
    }
    return () => channel.close()
  }, [markServiceStopped])

  // Freeze the page once the service is gone: requests stay pending (pollers
  // stop, pages keep their last data), streams close, and notifications whose
  // buttons would lead nowhere are dismissed.
  useEffect(() => {
    if (!serviceStopped) return
    haltRequests()
    suspendStreams()
    notify.destroy()
  }, [serviceStopped, notify])

  // `inert` (React 18 has no prop for it) keeps keyboard focus out of the
  // frozen page too, not only the mouse.
  const frozenShellRef = useCallback(
    (el: HTMLDivElement | null) => el?.toggleAttribute('inert', serviceStopped),
    [serviceStopped],
  )
  // A mask over the page (like a modal's) rather than dimming the page itself:
  // it also takes every click, and it repaints reliably in a tab that was in
  // the background when the stop arrived.
  const stoppedNotice = serviceStopped ? (
    <>
    <div
      aria-hidden="true"
      style={{ position: 'fixed', inset: 0, zIndex: 1999, background: COLORS.bg, opacity: 0.65 }}
    />
    <div
      role="status"
      style={{
        position: 'fixed',
        top: 16,
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: 2000,
        width: 'min(560px, calc(100vw - 32px))',
      }}
    >
      <Alert
        type="info"
        showIcon
        icon={<PoweroffOutlined />}
        message={`SmarTune service stopped at ${stoppedAt}`}
        description={
          <>
            This page no longer updates.
            <br />
            Start SmarTune again from the desktop icon.
          </>
        }
        style={{ boxShadow: '0 6px 24px rgba(0, 0, 0, 0.35)' }}
      />
    </div>
    </>
  ) : null

  const handleStopService = useCallback(async () => {
    // What a stop would end right now. Best-effort: if this fails, the
    // confirmation still explains what stopping does in general.
    let busy: string[] = []
    try {
      busy = (await api.getShutdownStatus()).busy
    } catch {
      /* offer the stop anyway */
    }
    modal.confirm({
      title: 'Stop the SmarTune service?',
      icon: <PoweroffOutlined />,
      content: (
        <div>
          <p style={{ marginBottom: busy.length > 0 ? 8 : 0 }}>
            The SmarTune service will stop, and the dashboard will close in every open tab.
          </p>
          {/* Only what this stop actually ends; nothing more to say when idle. */}
          {busy.length > 0 ? (
            <>
              <p style={{ marginBottom: 4 }}>This also ends:</p>
              <ul style={{ paddingLeft: 20, marginBottom: 0 }}>
                {busy.map((reason) => (
                  <li key={reason}>{reason.charAt(0).toUpperCase() + reason.slice(1)}</li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ),
      okText: 'Stop service',
      okButtonProps: { danger: true },
      cancelText: 'Cancel',
      onOk: async () => {
        try {
          await api.stopService()
        } catch (err) {
          message.error(`Could not stop the service: ${(err as Error).message}`)
          return
        }
        if (typeof BroadcastChannel !== 'undefined') {
          const channel = new BroadcastChannel(SERVICE_CHANNEL)
          channel.postMessage({ type: 'stopped' })
          channel.close()
        }
        markServiceStopped()
      },
    })
  }, [modal, markServiceStopped])

  // Leaving the header only makes sense to use Diagnostics context: its Alerts
  // section, or one alert's row there.
  const openInDiagnostics = useCallback((request: { kind: 'current' } | { kind: 'locate'; alert: DiagAlert }) => {
    setAlertCenterOpen(false)
    setAlertCenterExpanded(false)
    setActiveTab('diagnostics')
    setDiagnosticsAlertsRequest((previous) => ({ requestId: (previous?.requestId ?? 0) + 1, ...request }))
  }, [])

  // Quick triage from the header without leaving the current page.
  const runAlertCenterAction = useCallback(async <T,>(alert: DiagAlert, action: () => Promise<T>, success: string | ((result: T) => string)) => {
    setAlertCenterActionKey(alert.dedup_key)
    try {
      const result = await action()
      loadAlerts()
      message.success(typeof success === 'function' ? success(result) : success)
    } catch (error) {
      message.error(error instanceof Error ? error.message : 'Unable to update alert')
    } finally {
      setAlertCenterActionKey(null)
    }
  }, [loadAlerts])

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

  // The header only triages what needs attention now; handled (ack / silenced)
  // alerts live in Diagnostics. Long lists start collapsed and expand in place.
  const alertCenterList = alertCenterExpanded
    ? alertCenterGroups.needs_attention
    : alertCenterGroups.needs_attention.slice(0, ALERT_CENTER_MAX_ITEMS)
  const alertCenterOverflow = needsAttentionCount - ALERT_CENTER_MAX_ITEMS
  const handledSummary = (['acknowledged', 'silenced'] as const)
    .filter((mark) => alertCenterGroups[mark].length > 0)
    .map((mark) => `${alertCenterGroups[mark].length} ${mark}`)
    .join(' · ')
  const alertCenter = (
    <div className="app-alert-center">
      <div className="app-alert-center__header">
        <Typography.Text strong style={{ color: COLORS.text }}>Current alerts</Typography.Text>
        <Typography.Text className="app-alert-center__count">{needsAttentionCount} need attention</Typography.Text>
      </div>
      {alertCenterList.length ? (
        <div className="app-alert-center__list">
          {alertCenterList.map((alert) => (
            <div key={alert.dedup_key} className="app-alert-center__item">
              <Tooltip title="Show in Diagnostics" placement="left" mouseEnterDelay={0.5}>
                <button
                  type="button"
                  onClick={() => openInDiagnostics({ kind: 'locate', alert })}
                  className="app-alert-center__item-main"
                >
                  <div className="app-alert-center__item-title">
                    <Tag color={alertColor(alert.severity)} style={{ marginInlineEnd: 0, textTransform: 'uppercase' }}>{alert.severity}</Tag>
                    <Typography.Text ellipsis style={{ color: COLORS.text, flex: 1, minWidth: 0, fontSize: 13 }}>
                      {alert.summary || alert.event_type}
                    </Typography.Text>
                    {/* Same "leaves this view" marker as the Home cockpit cards. */}
                    <ExportOutlined className="app-alert-center__nav-icon" aria-hidden="true" />
                  </div>
                  <Typography.Text className="app-alert-center__item-meta">
                    {alert.scope || 'System'} · Latest {formatAlertTime(alert.last_fired_at)} · Detected {alert.fire_count}×
                  </Typography.Text>
                </button>
              </Tooltip>
              <div className="app-alert-center__item-actions">
                {/* Both actions share one filled, neutral style: the fill keeps
                    them from reading as tags, and matching styles keep the pair
                    from reading as a toggle with one option selected. No icons:
                    a check mark reads as "already acknowledged". */}
                <Button
                  size="small"
                  color="default"
                  variant="filled"
                  loading={alertCenterActionKey === alert.dedup_key}
                  onClick={() => void runAlertCenterAction(alert, () => api.acknowledgeDiagAlert(alert.dedup_key), (result) => (result.closed ? 'Alert acknowledged and closed' : 'Alert acknowledged'))}
                >
                  Acknowledge
                </Button>
                <Dropdown
                  trigger={['click']}
                  disabled={alertCenterActionKey === alert.dedup_key}
                  menu={{
                    items: [
                      { key: '30', label: '30 minutes' },
                      { key: '120', label: '2 hours' },
                    ],
                    onClick: ({ key }) => {
                      const minutes = Number(key) as 30 | 120
                      void runAlertCenterAction(
                        alert,
                        () => api.silenceDiagAlert(alert.dedup_key, minutes),
                        `Alert silenced for ${minutes === 30 ? '30 minutes' : '2 hours'}`,
                      )
                    },
                  }}
                >
                  <Button size="small" color="default" variant="filled">
                    Silence <DownOutlined className="app-alert-center__caret" />
                  </Button>
                </Dropdown>
              </div>
            </div>
          ))}
          {alertCenterOverflow > 0 ? (
            <button
              type="button"
              className="app-alert-center__more"
              onClick={() => setAlertCenterExpanded((expanded) => !expanded)}
            >
              {alertCenterExpanded ? 'Show less' : `Show ${alertCenterOverflow} more`}
            </button>
          ) : null}
        </div>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="No alerts need attention"
          style={{ margin: '20px 0' }}
        />
      )}
      <div className="app-alert-center__footer">
        <Typography.Text className="app-alert-center__count">{handledSummary}</Typography.Text>
        <Button type="link" size="small" onClick={() => openInDiagnostics({ kind: 'current' })} style={{ padding: 0 }}>
          View all alerts <ExportOutlined />
        </Button>
      </div>
    </div>
  )

  // Publish the sticky application header height so page-level toolbars can
  // stay below it in the content workspace.
  // The header only exists once signed in, and its height changes with the
  // narrow-viewport layout, so observe the element itself rather than window resizes.
  useEffect(() => {
    if (booting || !authed) return
    const header = document.querySelector('.ant-layout-header') as HTMLElement | null
    if (!header) return
    const publish = () => {
      document.documentElement.style.setProperty('--app-sticky-top', `${header.offsetHeight}px`)
    }
    publish()
    const observer = new ResizeObserver(publish)
    observer.observe(header)
    return () => observer.disconnect()
  }, [booting, authed])

  const tabs = [
    {
      key: 'overview',
      label: 'Home',
      children: (
        <SystemOverview
          active={activeTab === 'overview'}
          colorMode={colorMode}
          onNavigate={setActiveTab}
          onOpenDiagnostics={(window) => {
            setDiagnosticsRangeIntent((current) => ({
              window,
              requestId: (current?.requestId ?? 0) + 1,
            }))
            setActiveTab('diagnostics')
          }}
          onOpenSettings={(section) => {
            setSettingsTabIntent((current) => ({
              key: section,
              requestId: (current?.requestId ?? 0) + 1,
            }))
            setActiveTab('settings')
          }}
          diagnosticsEnabled={diagnosticsEnabled}
          balancerEnabled={balancerEnabled}
        />
      ),
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
                alertsRequest={diagnosticsAlertsRequest}
                currentAlerts={currentAlerts}
                rangeIntent={diagnosticsRangeIntent}
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
              <ControlCenter
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
                onOpenNetworkSettings={() => {
                  setSettingsTabIntent((current) => ({
                    key: 'benchmark_network',
                    requestId: (current?.requestId ?? 0) + 1,
                  }))
                  setActiveTab('settings')
                }}
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
          benchmarkEnabled={benchmarkEnabled}
          onActiveTabChange={setSettingsSection}
          tabIntent={settingsTabIntent}
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

  const navigation = (
    <>
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
          setNavDrawerOpen(false)
        }}
      />
    </>
  )

  const navToggleLabel = narrowViewport
    ? 'Open navigation'
    : sidebarCollapsed ? 'Expand navigation' : 'Collapse navigation'

  // Validating a URL-hash bootstrap token — hold off rendering the gate.
  if (booting) {
    return <div style={{ minHeight: '100vh', background: COLORS.bg }} />
  }

  if (!authed) {
    return (
      <>
        {stoppedNotice}
        <div ref={frozenShellRef}>
          <LoginGate onAuthenticated={() => setAuthed(true)} />
        </div>
      </>
    )
  }

  return (
    <GlobalConfigNoticesProvider>
      {notifyHolder}
      {modalHolder}
      {stoppedNotice}
      <div ref={frozenShellRef}>
      <Layout className="app-shell" style={{ minHeight: '100vh', background: COLORS.bg }}>
        {narrowViewport ? (
          <Drawer
            className="app-nav app-nav-drawer"
            placement="left"
            width={264}
            closable={false}
            open={navDrawerOpen}
            onClose={() => setNavDrawerOpen(false)}
            styles={{ body: { padding: '0 12px 20px', background: COLORS.panelBg } }}
          >
            {navigation}
          </Drawer>
        ) : (
          <Sider
            className="app-nav app-sidebar"
            width={264}
            collapsedWidth={64}
            collapsible
            collapsed={sidebarCollapsed}
            trigger={null}
            onCollapse={setSidebarCollapsed}
            // Tablet widths (768-991px): start as the icon rail so the page
            // keeps ~640px instead of ~450px; the toggle still expands it.
            breakpoint="lg"
            onBreakpoint={setSidebarCollapsed}
            theme={colorMode}
            style={{
              background: COLORS.panelBg,
              borderRight: `1px solid ${COLORS.border}`,
              top: 0,
              height: '100vh',
              overflow: 'auto',
            }}
          >
            {navigation}
          </Sider>
        )}
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
              {/* A tap would leave the hover tooltip stuck on touch screens. */}
              <Tooltip title={narrowViewport ? undefined : navToggleLabel}>
                <Button
                  type="text"
                  size="small"
                  icon={narrowViewport || sidebarCollapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
                  aria-label={navToggleLabel}
                  onClick={() => {
                    if (narrowViewport) setNavDrawerOpen(true)
                    else setSidebarCollapsed((collapsed) => !collapsed)
                  }}
                />
              </Tooltip>
              {narrowViewport ? null : (
                <>
                  <DashboardOutlined />
                  <Typography.Text>Intel XPU SmarTune</Typography.Text>
                  <span>/</span>
                </>
              )}
              <Typography.Text strong ellipsis>{pageTitles[activeTab] ?? 'Home'}</Typography.Text>
              {activeTab === 'settings' && settingsSection ? (
                <>
                  <span>/</span>
                  <Typography.Text strong ellipsis>{settingsSection}</Typography.Text>
                </>
              ) : null}
            </div>
            <div className="app-header__actions">
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
                open={alertCenterOpen}
                onOpenChange={(open) => {
                  setAlertCenterOpen(open)
                  // Reopening starts from the short list again.
                  if (!open) setAlertCenterExpanded(false)
                }}
                dropdownRender={() => alertCenter}
                overlayStyle={{ paddingTop: 6 }}
              >
                <Button
                  type="text"
                  size="small"
                  aria-label={`Open alerts, ${needsAttentionCount} need attention`}
                  style={{ color: COLORS.textMuted }}
                >
                  <Badge count={needsAttentionCount} size="small" offset={[2, -1]}>
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
                  ...(shutdownEnabled
                    ? [
                        { type: 'divider' as const },
                        {
                          key: 'stop-service',
                          icon: <PoweroffOutlined />,
                          label: 'Stop service',
                          danger: true,
                          onClick: () => void handleStopService(),
                        },
                      ]
                    : []),
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
            <div className="app-content-area" ref={workspaceRef}>
              <WorkspaceWidthContext.Provider value={workspaceWidth}>
                <Tabs
                  className="app-content-tabs"
                  activeKey={activeTab}
                  onChange={setActiveTab}
                  items={tabs}
                  renderTabBar={() => <div />}
                />
              </WorkspaceWidthContext.Provider>
            </div>
          </Content>
        </Layout>
      </Layout>
      </div>
    </GlobalConfigNoticesProvider>
  )
}
