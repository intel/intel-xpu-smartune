export interface ApiResponse<T> {
  retcode: number
  retmsg: string
  data: T
}

export interface DiskDeviceData {
  utilization: number
  is_busy: boolean
  read_kb_per_sec: number
  write_kb_per_sec: number
  read_iops?: number
  write_iops?: number
}

export interface DiskData {
  disk_io: Record<string, DiskDeviceData>
  is_stressed?: boolean
  stressed_disks?: string[]
  iowait?: number
  busy_disks?: string[]
  total_disks?: number
  busy_ratio?: number | null
  busy_pct?: number | null
  busy_level?: string
}

export interface PressureData {
  cpu?: number
  memory?: number
  io?: number
  level?: string
  score?: number
  is_disk_io_stressed?: boolean
  network_rx?: number
  network_tx?: number
  network_busy_nics?: string[]
  network_total_nics?: number
  network_busy_ratio?: number | null
  network_busy_pct?: number | null
  network_busy_level?: string
}

export interface AppInfo {
  app_id: string
  app_name: string
  cpu_usage: number
  memory_mb: number
  io_read_rate: number
  score?: number
  priority?: string
  status?: string
  controlled?: boolean
  remark?: string
  cmdline?: string
  cgroup?: string
  process_names?: string[]
  is_running?: boolean
  is_pending?: boolean
}

export interface AppResourceEntry {
  app_id: string
  app_name: string
  pid: number
  process_name: string
  cmdline: string
  cpu_usage: number       // fraction of total CPU capacity (0-1)
  memory_mb: number       // resident memory in MB
  io_read_rate: number    // MB/s
  io_write_rate: number   // MB/s
  score: number
  gpu_util: number        // peak GPU engine utilisation % (0-100); 0 when GPU not in use
  gpu_mem_mb: number      // GPU memory used in MB (drm-memory-* from /proc fdinfo)
}

export interface AppResourceStatsData {
  apps: AppResourceEntry[]
}

export interface AppDiskIoEntry {
  pid: number
  name: string
  app_name: string
  cmdline: string
  io_read_rate: number    // MB/s
  io_write_rate: number   // MB/s
  io_read_iops: number    // ops/s
  io_write_iops: number   // ops/s
  score: number
}

export interface AppDiskIoStatsData {
  apps: AppDiskIoEntry[]
}

export interface ProcessEntry {
  pid: number
  name: string
  username: string
  cpu_percent: number
  memory_percent: number
  mem_rss_kb: number
  status: string
  cmdline: string
}

export interface ProcessListData {
  count: number
  processes: ProcessEntry[]
}

export type AppListData = AppInfo[]

export interface SetControlPayload {
  app_id: string
  app_name: string
  priority: string
  controlled: boolean
  remark: string
  cmdline: string
  cgroup: string
}

export interface AppIdPayload {
  app_id: string
  app_name: string
}

export interface SetPriorityPayload {
  app_id: string
  priority: string
}

// "Add Application" wizard ------------------------------------------------
// Mirrors balancer/monitor/app_discovery.py::Candidate / ExtractResult and
// the /app/discover_search, /app/discover_extract, /app/wizard_commit
// endpoints in BalanceService.py.
export interface DiscoverCandidate {
  pid: number
  comm: string         // /proc/<pid>/comm — same 15-byte truncation BPF reports
  exe: string          // readlink /proc/<pid>/exe (full path, may be empty)
  cmdline: string      // nul-joined cmdline rendered with spaces
  cgroup_unit: string  // systemd unit/scope (or "")
  ppid: number
  score: number        // ranking hint; higher = more likely user-launched
}

export interface DiscoverSearchData {
  count: number
  candidates: DiscoverCandidate[]
}

export interface DiscoverExtractData {
  bpf_name: string[]
  process_names: string[]
  commandline: string[]
  id_suggestion: string
}

export interface WizardCommitPayload {
  name: string
  id: string
  priority: string
  remark: string
  commandline: string
  bpf_name: string[]
  process_names: string[]
}

export interface WizardCommitData {
  name: string
  id: string
}

export interface ResourceLimitPayload {
  app_id: string
  app_name: string
  priority: string
  cgroup_id?: string
  limit_overrides?: {
    cpu?: {
      enabled: boolean
      rate?: number
    }
    memory?: {
      enabled: boolean
      rate?: number
    }
    disk_io?: {
      enabled: boolean
      rate?: {
        write: number
        read: number
        write_iops: number
        read_iops: number
      }
    }
  }
}

export interface ResourceLimitProfileData {
  cpu: {
    enabled: boolean
    value: number
    min: number
    max: number
    options?: number[]
  }
  memory: {
    enabled: boolean
    value: number
    min: number
    max: number
    options?: number[]
  }
  disk_io: {
    enabled: boolean
    is_io_limit?: boolean
    write: { value: number; min: number; max: number }
    read: { value: number; min: number; max: number }
    write_iops: { value: number; min: number; max: number }
    read_iops: { value: number; min: number; max: number }
  }
  process_names?: string[]
  cgroup_ids?: string[]
}

export interface PackageInfo {
  installed: boolean
  version: string | null
  raw: string | null
}

export interface StaticInfoData {
  collected_at: string
  bios: {
    version: string | null
  }
  os: {
    version: string | null
  }
  driver: {
    kernel_version: string | null
    kernel_cmdline: string | null
    guc_fw?: string[]
    huc_fw?: string[]
    mesa: PackageInfo
    opencl: PackageInfo
    level_zero: PackageInfo
    media: PackageInfo
    npu_fw: string | null
  }
  cpu: {
    model_name: string | null
    core_count: {
      logical: number | null
      physical: number | null
    }
    freq_mhz: {
      min_mhz: number | null
      max_mhz: number | null
      base_mhz?: number | null
      per_core_mhz: Array<number | null>
      p_core_freq_mhz?: { min_mhz: number | null; max_mhz: number | null } | null
      e_core_freq_mhz?: { min_mhz: number | null; max_mhz: number | null } | null
      lpe_core_freq_mhz?: { min_mhz: number | null; max_mhz: number | null } | null
    }
  }
  memory: {
    ddr_speeds: string[]
    total_gb: number | null
    swap_total_gb?: number | null
    devices?: {
      total_slots: number | null
      populated: number
      channels?: number | null
      devices: Array<{
        locator: string | null
        bank_locator?: string | null
        size_gb: number | null
        type: string | null
        speed: string | null
        configured_speed: string | null
        form_factor: string | null
        manufacturer: string | null
        part_number: string | null
      }>
    }
  }
  io: {
    nic_count: number
    network_speeds_mbps: Record<string, number>
    network_peak_mbps: number | null
    primary_interface: string
    valid_nics: Array<{ name: string; speed_mbps: number; ipv4?: string[]; ipv6?: string[] }>
  }
  disk: {
    device_count: number
    total_size_bytes: number | null
    total_size_gb: number | null
    devices: Array<{
      name: string
      size_bytes: number | null
      size_gb: number | null
    }>
  }
  gpu: {
    names: string[]
    count: number
    engines: Record<string, string[]>
    freq_bounds_mhz: Record<string, { min_mhz: number | null; max_mhz: number | null }>
    gt_freq_bounds_mhz?: Record<string, {
      gt0?: { min_mhz: number | null; max_mhz: number | null }
      gt1?: { min_mhz: number | null; max_mhz: number | null }
    }>
    vram: Record<string, { total_bytes: number | null; used_bytes: number | null; usage_percent: number | null }>
    pcie: Record<string, { current_speed: string | null; current_width: string | null; max_speed: string | null; max_width: string | null }>
    eu_count?: Record<string, number | null>
    pci_addresses?: Record<string, string>
    driver_names?: Record<string, string | null>
  }
  npu: {
    names: string[]
    freq_bounds_mhz: Record<string, { min_mhz?: number | null; max_mhz: number | null }>
    pciid?: string | null
    driver_version?: string | null
  }
}

export interface ToolOutput {
  available: boolean
  raw: string | null
  error: string | null
}

export interface GpuUsageFreq {
  name: string
  min_mhz: number | null
  cur_mhz: number | null
  act_mhz: number | null
  max_mhz: number | null
  rc6_pct: number | null
  throttled: boolean
  throttle_reasons: string[]
}

export interface GpuUsageDevice {
  pci_dev: string | null
  dev_type: string | null
  drv_name: string | null
  engines: string[]
  freqs: GpuUsageFreq[]
  power_w: {
    gpu: number | null
    pkg: number | null
    card?: number | null
  }
  engine_util: Record<string, number | null>
  utilization?: number | null
}

export interface GpuUsageParsed {
  timestamp: number | null
  version: string | null
  devices: GpuUsageDevice[]
}

export interface GpuUsageOutput {
  available: boolean
  raw: string | null
  error: string | null
  parsed: GpuUsageParsed | null
}

export interface DynamicInfoData {
  collected_at: string
  cpu: {
    usage_total: number | null
    per_core_usage: number[]
    per_core_freq_mhz: Array<number | null>
    p_core_usage: number | null
    e_core_usage: number | null
    lpe_core_usage: number | null
    p_core_freq_mhz: number | null
    e_core_freq_mhz: number | null
    lpe_core_freq_mhz: number | null
    p_core_indices: number[]
    e_core_indices: number[]
    lpe_core_indices: number[]
    core_type_source: string
    temperature_c: number | null
    per_core_temperature_c?: Array<number | null>
  }
  memory: {
    usage_percent: number | null
    total_gb: number | null
    available_gb: number | null
    swap_total_gb: number | null
    swap_used_gb: number | null
    swap_usage_percent: number | null
  }
  pressure: PressureData
  network: {
    interfaces: Record<string, { rx_bytes_per_sec: number; tx_bytes_per_sec: number }>
    total: { rx_bytes_per_sec: number; tx_bytes_per_sec: number }
  }
  disk: DiskData
  gpu: {
    vram: Record<string, { total_bytes: number | null; used_bytes: number | null; usage_percent: number | null }>
    gpu_usage: GpuUsageOutput
  }
  npu: {
    npu_smi: ToolOutput
  }
}

export type HistorySnapshotType = 'static' | 'dynamic' | 'all'

export interface HistorySnapshotItem {
  id: number
  snapshot_type: 'static' | 'dynamic'
  source: string
  collected_at: string | null
  create_time: number
  update_time: number
  create_date: string | null
  update_date: string | null
  data: StaticInfoData | DynamicInfoData | Record<string, unknown> | string | null
}

export interface HistoryData {
  snapshot_type: HistorySnapshotType
  limit: number
  start_time?: number | null
  end_time?: number | null
  // Server-side "now" at the moment of this query, in unix seconds.
  // The UI uses this to detect client/server clock skew rather than trusting
  // Date.now() on a possibly-misconfigured client.
  server_time?: number
  count: number
  items: HistorySnapshotItem[]
}

export interface HistoryQueryOptions {
  snapshotType?: HistorySnapshotType
  limit?: number
  startTime?: number | null
  endTime?: number | null
  // Preset window length in seconds.  When set (and startTime/endTime are
  // omitted) the server anchors the window to its own clock, immune to a
  // skewed client wall clock.  Per-client value: a tab choosing 1 h does not
  // affect another tab still on 15 min.
  rangeSeconds?: number | null
}

export interface HistoryRetentionData {
  retention_days: number
  default_days: number
  min_days: number
  max_days: number
  updated_at?: number
}

export interface WeightsTopData {
  cpu: number
  memory: number
  gpu: number
  updated_at?: number
}

export interface PassiveControlData {
  enabled: boolean
  updated_at?: number
}

export type SaveResult<TOk> =
  | { status: 'ok'; data: TOk }
  | { status: 'conflict'; current: any; message: string }
