import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Col, Descriptions, Row, Space, Spin, Typography, Timeline } from 'antd'
import { InfoCircleOutlined, ReloadOutlined, RobotOutlined } from '@ant-design/icons'
import { api } from '../api/client'
import type { PackageInfo, StaticInfoData } from '../api/types'
import { COLORS } from '../styles/theme'

const { Text, Title, Paragraph } = Typography

interface Props {
  active: boolean
}

function formatPackageText(pkg?: PackageInfo | null): string {
  if (!pkg?.installed) return 'Not Installed'
  return pkg.version || pkg.raw || 'Installed'
}

function formatPlain(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'N/A'
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'N/A'
  return `${value}`
}

function formatCompactText(value: unknown, head = 24, tail = 12): string {
  const text = formatPlain(value)
  if (text === 'N/A') return text
  if (text.length <= head + tail + 3) return text
  return `${text.slice(0, head)}...${text.slice(-tail)}`
}

function formatFreqRange(min?: number | null, max?: number | null): string {
  if (typeof min === 'number' && typeof max === 'number') return `${Math.round(min)}-${Math.round(max)} MHz`
  if (typeof min === 'number') return `min ${Math.round(min)} MHz`
  if (typeof max === 'number') return `max ${Math.round(max)} MHz`
  return 'N/A'
}

function summarizeFreqBounds(bounds?: Record<string, { min_mhz: number | null; max_mhz: number | null }>): string {
  const entries = Object.entries(bounds || {})
  if (!entries.length) return 'N/A'
  return entries.map(([name, range]) => `${name}: ${formatFreqRange(range.min_mhz, range.max_mhz)}`).join(' | ')
}

function summarizeNpuFreqBounds(bounds?: Record<string, { min_mhz?: number | null; max_mhz: number | null }>): string {
  const entries = Object.entries(bounds || {})
  if (!entries.length) return 'N/A'
  return entries.map(([, range]) => formatFreqRange(range.min_mhz ?? null, range.max_mhz)).join(' | ')
}

/** Cards with a PCIe entry = dGPU; rest = iGPU. Labels use the real cardKey. */
function buildGpuCardLabels(
  cardKeys: string[],
  pcieKeys: Set<string>,
  pciAddresses: Record<string, string> = {},
): Record<string, string> {
  const labels: Record<string, string> = {}
  for (const k of cardKeys) {
    const pci = (pciAddresses[k] || '').toLowerCase()
    const isIntegrated = pci ? /(^|:)00:02\./.test(pci) : !pcieKeys.has(k)
    labels[k] = `${isIntegrated ? 'iGPU' : 'dGPU'} (${k})`
  }
  return labels
}

function summarizeGpuFreqBounds(
  bounds?: Record<string, { min_mhz: number | null; max_mhz: number | null }>,
  pcie?: Record<string, unknown>,
  pciAddresses?: Record<string, string>,
): string {
  const entries = Object.entries(bounds || {})
  if (!entries.length) return 'N/A'
  const labels = buildGpuCardLabels(entries.map(([k]) => k), new Set(Object.keys(pcie || {})), pciAddresses)
  return entries.map(([name, range]) => `${labels[name] ?? name}: ${formatFreqRange(range.min_mhz, range.max_mhz)}`).join(' | ')
}

function summarizeGpuPcie(
  pcie?: Record<string, { current_speed: string | null; current_width: string | null; max_speed: string | null; max_width: string | null }>,
  bounds?: Record<string, unknown>,
  pciAddresses?: Record<string, string>,
): string {
  const entries = Object.entries(pcie || {})
  if (!entries.length) return 'N/A'
  const allKeys = Object.keys(bounds || pcie || {})
  const labels = buildGpuCardLabels(allKeys, new Set(Object.keys(pcie || {})), pciAddresses)
  return entries.map(([card, info]) => {
    const cur = info.current_speed && info.current_width
      ? `${info.current_speed} x${info.current_width}`
      : (info.current_speed || info.current_width || null)
    const max = info.max_speed && info.max_width ? `max ${info.max_speed} x${info.max_width}` : null
    const link = cur && max ? `${cur} (${max})` : (cur || max || 'N/A')
    return `${labels[card] ?? card}: ${link}`
  }).join(' | ')
}

function summarizeGpuPerDevice(
  staticInfo?: StaticInfoData | null,
): Array<{ label: string; engines: string; gtFreqs: string; driverName: string | null; euCount: number | null }> {
  if (!staticInfo) return []

  const cardKeySet = new Set<string>()
  Object.keys(staticInfo.gpu.engines || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo.gpu.freq_bounds_mhz || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo.gpu.pcie || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo.gpu.pci_addresses || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo.gpu.gt_freq_bounds_mhz || {}).forEach((k) => cardKeySet.add(k))
  Object.keys(staticInfo.gpu.driver_names || {}).forEach((k) => cardKeySet.add(k))

  const pcieKeys = new Set(Object.keys(staticInfo.gpu.pcie || {}))
  const pciAddresses = staticInfo.gpu.pci_addresses || {}
  const isIntegrated = (cardKey: string): boolean => {
    const pci = (pciAddresses[cardKey] || '').toLowerCase()
    if (pci) return /(^|:)00:02\./.test(pci)
    return !pcieKeys.has(cardKey)
  }
  const cardKeys = Array.from(cardKeySet).sort((a, b) => {
    const ai = isIntegrated(a) ? 0 : 1
    const bi = isIntegrated(b) ? 0 : 1
    if (ai !== bi) return ai - bi
    return a.localeCompare(b)
  })
  const labels = buildGpuCardLabels(cardKeys, pcieKeys, pciAddresses)

  return cardKeys.map((cardKey) => {
    const engineInstances = staticInfo.gpu.engines?.[cardKey] || []
    // Group instances by type: ccs0,ccs1,ccs2,ccs3 -> CCS ×4
    const typeOrder = ['bcs', 'ccs', 'rcs', 'vcs', 'vecs']
    const typeCounts: Record<string, number> = {}
    for (const name of engineInstances) {
      const lowered = name.toLowerCase()
      const engineType = typeOrder.find((t) => lowered.startsWith(t))
      if (engineType) typeCounts[engineType] = (typeCounts[engineType] || 0) + 1
    }
    const enginesSummary = typeOrder
      .filter((t) => typeCounts[t])
      .map((t) => typeCounts[t] > 1 ? `${t.toUpperCase()} \u00d7${typeCounts[t]}` : t.toUpperCase())
      .join(' | ')

    const gt0 = staticInfo.gpu.gt_freq_bounds_mhz?.[cardKey]?.gt0
    const gt1 = staticInfo.gpu.gt_freq_bounds_mhz?.[cardKey]?.gt1
    const gtFreqs = [
      gt0 ? `gt0 ${formatFreqRange(gt0.min_mhz, gt0.max_mhz)}` : null,
      gt1 ? `gt1 ${formatFreqRange(gt1.min_mhz, gt1.max_mhz)}` : null,
    ].filter(Boolean).join(' | ')

    const driverName = staticInfo.gpu.driver_names?.[cardKey] ?? null
    const euCount = staticInfo.gpu.eu_count?.[cardKey] ?? null

    return {
      label: labels[cardKey] || cardKey,
      engines: enginesSummary || 'N/A',
      gtFreqs: gtFreqs || 'N/A',
      driverName,
      euCount,
    }
  })
}

function summarizeDiskSizes(devices?: Array<{ name: string; size_gb: number | null }>): string {
  if (!devices?.length) return 'N/A'
  return devices
    .map((disk) => `${disk.name}: ${typeof disk.size_gb === 'number' ? `${disk.size_gb.toFixed(2)} GB` : 'N/A'}`)
    .join(' | ')
}

function formatNetworkSpeed(value?: number | null): string {
  if (typeof value !== 'number' || value <= 0) return 'N/A'
  return `${Math.round(value)} Mbps`
}

function summarizeNetworkSpeeds(speeds?: Record<string, number>): string {
  const entries = Object.entries(speeds || {})
  if (!entries.length) return 'N/A'
  return entries.map(([name, speed]) => `${name}: ${formatNetworkSpeed(speed)}`).join(' | ')
}

function summarizeGpuNamesByCard(staticInfo?: StaticInfoData | null): string {
  if (!staticInfo) return 'N/A'

  const names = staticInfo.gpu.names || []
  const pciAddresses = staticInfo.gpu.pci_addresses || {}
  const cardKeys = Object.keys(pciAddresses).sort()
  if (!cardKeys.length) return formatPlain(names)

  const nameByBdf: Record<string, string> = {}
  names.forEach((line) => {
    const match = `${line}`.match(/^([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/i)
    if (match) {
      nameByBdf[match[1].toLowerCase()] = line
    }
  })

  const mapped = cardKeys.map((cardKey) => {
    const pci = pciAddresses[cardKey]
    const shortBdf = typeof pci === 'string' ? pci.replace(/^[0-9a-f]{4}:/i, '').toLowerCase() : ''
    const line = nameByBdf[shortBdf]
    return `${cardKey} -> ${line || 'N/A'}`
  })

  return mapped.join(' | ')
}

function SectionCard({
  title,
  items,
  descColumns = 1,
}: {
  title: string
  items: Array<{ label: string; value: React.ReactNode }>
  descColumns?: number
}) {
  return (
    <Card
      className="perf-card perf-rise"
      bodyStyle={{ padding: 14 }}
      style={{ height: '100%' }}
    >
      <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>{title}</Text>
      <Descriptions
        size="small"
        column={descColumns}
        style={{ marginTop: 8 }}
        labelStyle={{ color: COLORS.textMuted, fontSize: 12, width: 140 }}
        contentStyle={{ color: COLORS.text, fontSize: 12, wordBreak: 'break-word' }}
      >
        {items.map((item) => (
          <Descriptions.Item key={`${title}-${item.label}`} label={item.label}>
            {item.value}
          </Descriptions.Item>
        ))}
      </Descriptions>
    </Card>
  )
}

export default function About({ active }: Props) {
  const [staticInfo, setStaticInfo] = useState<StaticInfoData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchStatic = useCallback(async (forceRefresh: boolean) => {
    if (!active) return
    setLoading(true)
    try {
      const data = forceRefresh ? await api.refreshStaticInfo() : await api.getStaticInfo()
      setStaticInfo(data)
      setError(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch static info')
    } finally {
      setLoading(false)
    }
  }, [active])

  useEffect(() => {
    if (!active) return
    fetchStatic(false)
  }, [active, fetchStatic])

  const softwareSections = useMemo(() => {
    if (!staticInfo) return [] as Array<{ title: string; items: Array<{ label: string; value: string }> }>

    return [
      {
        title: 'Software & Drivers',
        items: [
          { label: 'OS version', value: formatPlain(staticInfo.os.version) },
          { label: 'BIOS version', value: formatPlain(staticInfo.bios.version) },
          { label: 'Kernel version', value: formatPlain(staticInfo.driver.kernel_version) },
          { label: 'GuC FW', value: staticInfo.driver.guc_fw?.map(f => `${f.firmware} ${f.version}`).join(', ') || 'N/A' },
          { label: 'HuC FW', value: staticInfo.driver.huc_fw?.map(f => `${f.firmware} ${f.version}`).join(', ') || 'N/A' },
          { label: 'NPU FW', value: formatCompactText(staticInfo.driver.npu_fw) },
          { label: 'Mesa', value: formatPackageText(staticInfo.driver.mesa) },
          { label: 'OpenCL', value: formatPackageText(staticInfo.driver.opencl) },
          { label: 'Level Zero', value: formatPackageText(staticInfo.driver.level_zero) },
          { label: 'Media', value: formatPackageText(staticInfo.driver.media) },
        ],
      },
    ]
  }, [staticInfo])

  const hardwareSections = useMemo(() => {
    if (!staticInfo) return [] as Array<{ title: string; items: Array<{ label: string; value: React.ReactNode }> }>

    const gpuPerDevice = summarizeGpuPerDevice(staticInfo)
    const pcieEntries = Object.entries(staticInfo.gpu.pcie || {})
    const pcieKeys = new Set(Object.keys(staticInfo.gpu.pcie || {}))
    const pciAddresses = staticInfo.gpu.pci_addresses || {}
    const gpuNames = staticInfo.gpu.names || []
    const nameByBdf: Record<string, string> = {}
    gpuNames.forEach((line) => {
      const match = `${line}`.match(/^([0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])/i)
      if (match) nameByBdf[match[1].toLowerCase()] = line
    })

    const gpuCards: Array<{ title: string; items: Array<{ label: string; value: React.ReactNode }> }> = gpuPerDevice.map((gpu) => {
      // Find the card key from the label, e.g. "dGPU(card0)" -> "card0"
      const cardKeyMatch = gpu.label.match(/\(([^)]+)\)/)
      const cardKey = cardKeyMatch ? cardKeyMatch[1] : ''
      const pci = pciAddresses[cardKey]
      const shortBdf = typeof pci === 'string' ? pci.replace(/^[0-9a-f]{4}:/i, '').toLowerCase() : ''
      const nameLine = nameByBdf[shortBdf] || 'N/A'
      const pcieInfo = pcieEntries.find(([k]) => k === cardKey)
      let pcieText = 'N/A'
      if (pcieInfo) {
        const info = pcieInfo[1]
        const cur = info.current_speed && info.current_width
          ? `${info.current_speed} x${info.current_width}` : (info.current_speed || info.current_width || null)
        const max = info.max_speed && info.max_width ? `max ${info.max_speed} x${info.max_width}` : null
        pcieText = cur && max ? `${cur} (${max})` : (cur || max || 'N/A')
      }

      const driverLabel = gpu.driverName || 'N/A'

      const items: Array<{ label: string; value: React.ReactNode }> = [
        { label: 'Name', value: nameLine },
        { label: 'Driver', value: driverLabel },
        ...(pcieKeys.has(cardKey) ? [{ label: 'PCIe', value: pcieText }] : []),
        ...(gpu.driverName === 'i915' && typeof gpu.euCount === 'number' ? [{ label: 'EU Count', value: `${gpu.euCount}` }] : []),
        { label: 'Engines', value: gpu.engines },
        { label: 'GT0/GT1 Freq', value: gpu.gtFreqs },
      ]
      return { title: gpu.label, items }
    })

    return [
      {
        title: 'CPU',
        items: [
          { label: 'Model', value: formatPlain(staticInfo.cpu.model_name) },
          { label: 'Logical cores', value: formatPlain(staticInfo.cpu.core_count.logical) },
          { label: 'Physical cores', value: formatPlain(staticInfo.cpu.core_count.physical) },
          ...(staticInfo.cpu.freq_mhz.p_core_freq_mhz
            ? [{ label: 'P-Core freq', value: formatFreqRange(staticInfo.cpu.freq_mhz.p_core_freq_mhz.min_mhz, staticInfo.cpu.freq_mhz.p_core_freq_mhz.max_mhz) }]
            : [{ label: 'Freq range', value: formatFreqRange(staticInfo.cpu.freq_mhz.min_mhz, staticInfo.cpu.freq_mhz.max_mhz) }]),
          ...(staticInfo.cpu.freq_mhz.e_core_freq_mhz
            ? [{ label: 'E-Core freq', value: formatFreqRange(staticInfo.cpu.freq_mhz.e_core_freq_mhz.min_mhz, staticInfo.cpu.freq_mhz.e_core_freq_mhz.max_mhz) }]
            : []),
          ...(staticInfo.cpu.freq_mhz.lpe_core_freq_mhz
            ? [{ label: 'LPE-Core freq', value: formatFreqRange(staticInfo.cpu.freq_mhz.lpe_core_freq_mhz.min_mhz, staticInfo.cpu.freq_mhz.lpe_core_freq_mhz.max_mhz) }]
            : []),
        ],
      },
      {
        title: 'Memory',
        items: (() => {
          const devs = staticInfo.memory.devices?.devices
          const devInfo = staticInfo.memory.devices
          // Type: deduplicate across all populated slots
          const types = devs?.length ? [...new Set(devs.map((d) => d.type).filter(Boolean))] : []
          // Speed: rated speed with actual configured speed in parentheses
          let speedText = formatPlain(staticInfo.memory.ddr_speeds)
          if (devs?.length) {
            const rated = devs[0].speed
            const configured = devs[0].configured_speed
            if (rated && rated !== 'Unknown') {
              const configuredLabel = configured && configured !== 'Unknown' ? configured : null
              speedText = configuredLabel ? `${rated} (Actual: ${configuredLabel})` : rated
            }
          }
          return [
            { label: 'Total', value: typeof staticInfo.memory.total_gb === 'number' ? `${staticInfo.memory.total_gb.toFixed(1)} GB` : 'N/A' },
            ...(types.length ? [{ label: 'Type', value: types.join(', ') }] : []),
            { label: 'Speed', value: speedText },
            ...(devInfo?.total_slots != null
              ? [{ label: 'Slots', value: `${devInfo.populated} of ${devInfo.total_slots} populated` }]
              : []),
            ...(devInfo?.channels != null
              ? [{ label: 'Channel', value: devInfo.channels === 1 ? 'Single' : devInfo.channels === 2 ? 'Dual' : `${devInfo.channels}ch` }]
              : []),
            { label: 'Swap', value: typeof staticInfo.memory.swap_total_gb === 'number' && staticInfo.memory.swap_total_gb > 0
              ? `${staticInfo.memory.swap_total_gb.toFixed(1)} GB`
              : 'Not configured' },
          ]
        })(),
      },
      ...(staticInfo.disk.devices?.length
        ? staticInfo.disk.devices.map((dev) => ({
            title: `Disk: ${dev.name}`,
            items: [
              { label: 'Size', value: typeof dev.size_gb === 'number' ? `${dev.size_gb.toFixed(2)} GB` : 'N/A' },
              { label: 'Type', value: dev.name.startsWith('nvme') ? 'NVMe SSD' : 'Disk' },
            ],
          }))
        : [{
            title: 'Disk',
            items: [
              { label: 'Total size', value: typeof staticInfo.disk.total_size_gb === 'number' ? `${staticInfo.disk.total_size_gb.toFixed(2)} GB` : 'N/A' },
            ],
          }]
      ),
      ...(staticInfo.io.valid_nics?.length
        ? staticInfo.io.valid_nics.map((nic) => ({
            title: `NIC: ${nic.name}`,
            items: [
              { label: 'Speed', value: formatNetworkSpeed(nic.speed_mbps) },
              { label: 'Primary', value: nic.name === staticInfo.io.primary_interface ? 'Yes' : 'No' },
              ...(nic.ipv4?.length ? [{ label: 'IPv4', value: nic.ipv4.join(', ') }] : []),
              ...(nic.ipv6?.length ? [{ label: 'IPv6', value: nic.ipv6.join(', ') }] : []),
            ],
          }))
        : [{
            title: 'Network',
            items: [
              { label: 'NIC count', value: formatPlain(staticInfo.io.nic_count) },
              { label: 'Primary NIC', value: formatPlain(staticInfo.io.primary_interface) },
              { label: 'Peak speed', value: formatNetworkSpeed(staticInfo.io.network_peak_mbps) },
            ],
          }]
      ),
      {
        title: 'NPU',
        items: [
          { label: 'Name', value: formatPlain(staticInfo.npu.names) },
          { label: 'Device', value: formatPlain(staticInfo.npu.pciid) },
          { label: 'Driver', value: formatPlain(staticInfo.npu.driver_version) },
          { label: 'Freq bounds', value: summarizeNpuFreqBounds(staticInfo.npu.freq_bounds_mhz) },
        ],
      },
      ...gpuCards,
    ]
  }, [staticInfo])

  return (
    <div style={{ padding: '16px 0' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <Space size={8}>
          <InfoCircleOutlined style={{ color: COLORS.accent }} />
          <Title level={4} style={{ color: COLORS.text, margin: 0 }}>
            About
          </Title>
          {staticInfo?.collected_at && (
            <Text style={{ color: COLORS.textMuted, fontSize: 12 }}>
              Updated: {staticInfo.collected_at}
            </Text>
          )}
        </Space>

        <Button
          size="small"
          icon={<ReloadOutlined />}
          onClick={() => fetchStatic(true)}
          loading={loading}
        >
          Refresh
        </Button>
      </div>

      {error && (
        <Alert
          message="About Data Error"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
        />
      )}

      {loading && !staticInfo ? (
        <Card className="perf-card" bodyStyle={{ padding: 18 }}>
          <Spin size="small" />
        </Card>
      ) : (
        <>
          <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 8 }}>
            Features
          </Text>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            <Col span={24}>
              <Card
                className="perf-card perf-rise"
                bodyStyle={{ padding: 16 }}
              >
                <Space align="center" style={{ marginBottom: 12 }}>
                  <RobotOutlined style={{ color: COLORS.accent, fontSize: 16 }} />
                  <Text style={{ color: COLORS.text, fontSize: 13, fontWeight: 600 }}>
                    SmarTune 功能说明
                  </Text>
                </Space>
                <Timeline
                  style={{ marginTop: 8 }}
                  items={[
                    {
                      color: COLORS.green,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>资源管控</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            在系统资源紧张时，通过 cgroups v2 对占用最多的应用动态限制 CPU、内存和磁盘 I/O 的资源使用，同时按压力等级切换功耗模式，并在压力缓解后逐步恢复资源配额。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.accent,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>压力监测</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            基于 Linux PSI（Pressure Stall Information）实时采集 CPU、内存和 I/O 压力数据，计算综合评分并划分低/中/高/临界四个压力等级；同时通过 eBPF 拦截 execve 系统调用实时感知受控应用的启动与退出，并独立监控磁盘 I/O 利用率和系统 iowait。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.orange,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>优先级队列</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            当系统达到临界压力或磁盘 I/O 繁忙时，新应用的启动请求被暂停并写入最大优先级队列；待资源恢复后按优先级顺序自动拉起等待中的应用，支持手动取消队列中的待启动请求。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.green,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>应用保活</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            对 Critical 优先级的受控应用，减小其被系统 OOM Killer 回收的概率；同时持续监控关键应用的运行进程，确保其稳定运行。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.orange,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>磁盘I/O控制</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            通过 cgroups v2 限制占用磁盘 I/O 资源过多的应用，按优先级分配读写带宽与 IOPS 配额，在磁盘 I/O 压力消退后逐步恢复限额。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.accent,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>网络I/O控制</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            通过 cgroup + iptables + tc/HTB 对受控应用实施出/入方向流量管控，按 Critical/High/Low/System 四级优先级分配带宽；基于滑动平均窗口实时计算网络压力等级，达到临界时依次限制低/高优先级类的带宽上限，压力下降后逐步回调恢复。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.green,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>GPU 实时监测</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            对每块 GPU 卡（iGPU/dGPU 自动区分）实时采集 gt0/gt1 频率（当前/实际/最大）、GPU 及封装功耗、各引擎（Render/Compute/Video Encode/Decode/Copy）使用率、VRAM 用量及频率降级原因；静态信息涵盖 GPU 名称、引擎列表、EU 数量、PCIe 速度/宽度及 PCI 地址。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.orange,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>NPU 实时监测</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            实时读取 NPU 利用率（%）、功耗（W）、温度（°C）、运行频率（MHz）、NoC 带宽（MiB/s）和内存占用；同时通过 /proc/[pid]/fdinfo 解析各进程的 NPU 内存占用，实现进程级 NPU 使用追踪。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.accent,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>系统信息采集</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            静态采集完整的硬件与软件环境信息：CPU 型号、P/E 核拓扑及频率范围；内存总量与 DDR 速率；磁盘设备列表与容量；网卡数量、主网卡及峰值带宽；每块 GPU 的名称、引擎、PCIe、频率范围及 EU 数量；NPU 的 PCI ID、驱动版本、固件版本及频率范围；操作系统版本、BIOS、内核及 GuC/HuC/NPU 固件、Mesa/OpenCL/Level Zero/Media 驱动版本。
                          </Paragraph>
                        </div>
                      ),
                    },
                    {
                      color: COLORS.textMuted,
                      children: (
                        <div>
                          <Text style={{ color: COLORS.text, fontWeight: 500 }}>手动控制</Text>
                          <Paragraph style={{ color: COLORS.textMuted, margin: '4px 0 0', fontSize: 12 }}>
                            支持用户手动操作受控应用，包括调整优先级、取消排队启动、设置资源限额（CPU/内存/I/O）、恢复正常配额、保活以及删除应用等。
                          </Paragraph>
                        </div>
                      ),
                    },
                  ]}
                />
              </Card>
            </Col>
          </Row>

          <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 8 }}>
            Software Configuration
          </Text>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            {softwareSections.map((section) => (
              <Col key={`soft-${section.title}`} xs={24}>
                <SectionCard title={section.title} items={section.items} descColumns={2} />
              </Col>
            ))}
          </Row>

          <Text style={{ color: COLORS.textMuted, fontSize: 12, display: 'block', marginBottom: 8 }}>
            Hardware Configuration
          </Text>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
            {hardwareSections.map((section) => (
              <Col key={`hw-${section.title}`} xs={24} md={12} xl={6}>
                <SectionCard title={section.title} items={section.items} />
              </Col>
            ))}
          </Row>
        </>
      )}
    </div>
  )
}
