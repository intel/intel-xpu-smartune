// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

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
  busy_disks?: string[]
  total_disks?: number
  busy_ratio?: number | null
  busy_pct?: number | null
  busy_level?: string
  // PSI-gated disk-IO pressure severity (0-100), separate from busy_pct (breadth).
  pressure_pct?: number | null
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
  network_pressure_level?: string
  network_pressure_pct?: number | null
  network_worst_nic?: string | null
  network_worst_direction?: string | null
  // Per-NIC, per-direction pressure diagnostics (why a direction is under pressure).
  network_interfaces?: Record<string, NetworkInterfacePressure>
}

// One direction's (rx or tx) pressure breakdown, all percent-scaled. Fields specific to
// a direction (hw_overflow/softnet on rx, fifo on tx) are optional so the other omits them.
export interface NetworkDirectionPressure {
  util_pct?: number
  distress_pct?: number
  score_pct?: number
  drop_ratio_pct?: number
  hw_overflow_ratio_pct?: number
  softnet_squeeze_ratio_pct?: number
  softnet_drop_ratio_pct?: number
  fifo_ratio_pct?: number
  collective_harm_pct?: number
  level?: string
  reason?: string | null
}

export interface NetworkInterfacePressure {
  rx?: NetworkDirectionPressure
  tx?: NetworkDirectionPressure
}

// Coarse control state that drives the unified management table's interaction
// (tag colour + button gating). The "partially restored" middle state is NOT an
// enum value -- it rides along under EffectiveControl / auto_detail for display
// only, since a half-relaxed auto limit is still auto-owned and stays locked.
export type ControlStatus = 'NORMAL' | 'MANUAL_LIMITED' | 'AUTO_LIMITED'

// The limit kept multi-dimensional on purpose (never collapsed to one percent):
// CPU/memory travel together; disk-IO is its own channel with the exact disk set
// it was written to (empty = every disk).
export interface EffectiveControl {
  cpu_mem: { limited: boolean; cpu_rate?: number | null; mem_rate?: number | null }
  disk_io: {
    limited: boolean
    disks: string[]
    read_mb_s?: number | null
    write_mb_s?: number | null
    read_iops?: number | null
    write_iops?: number | null
  }
}

// Pressure detail attached only to AUTO_LIMITED rows, for the drawer.
export interface AutoControlDetail {
  limit_reason: AutoLimitReason
  pressure_level: string
  // Which channel already had its staged relaxation. sys=CPU/mem, disk_io=IO.
  partial_parts: { sys?: boolean; disk_io?: boolean }
}

export interface AppInfo {
  app_id: string
  app_name: string
  cpu_usage: number
  memory_mb: number
  io_read_rate: number
  score?: number
  priority?: string
  network_priority?: string
  status?: string
  controlled?: boolean
  remark?: string
  cmdline?: string
  cgroup?: string
  process_names?: string[]
  bpf_name?: string[]
  is_running?: boolean
  is_pending?: boolean
  // Known to the database but no longer listed in config.yaml's controlled_apps
  // (its entry was deleted by hand). Selectable in "Option 2", which restores
  // the config entry from the snapshot stored on the row.
  previously_managed?: boolean
  app_summary_status?: 'Limited' | 'Partial Limited' | 'Not Limited' | 'No Running Process'
  runtime_hint?: 'Running' | 'Stopped' | 'Pending'
  process_status_rows?: ProcessStatusRow[]
  // Unified control contract (see backend get_controlled_app / _entry_control_view).
  control_status?: ControlStatus
  effective?: EffectiveControl | null
  auto_detail?: AutoControlDetail | null
  // Cgroups recorded when the active limit was applied. Unlike process_status_rows,
  // this remains available when a live process scan cannot find the process yet.
  limited_scopes?: string[]
}

export interface ProcessStatusRow {
  key: string
  pid?: number | null
  process_name: string
  cmdline?: string
  scope_processes?: ScopeProcess[]
  cgroup?: string
  runtime_status: 'Running' | 'Stopped' | 'Pending'
  limit_status: 'Limited' | 'Not Limited' | 'N/A'
  applied_at?: number | null
  note?: string
}

export interface ScopeProcess {
  pid: number
  process_name: string
  cmdline?: string
}

export interface AppResourceEntry {
  app_id: string
  app_name: string
  pid: number
  pids?: number[]         // all PIDs of the app; kill/suspend act on the whole set
  process_name: string
  cmdline: string
  status?: string         // representative status; 'stopped' when any PID is suspended
  is_self?: boolean       // any PID belongs to SmartTune itself — never signal
  balancer_candidate?: boolean  // false for shells / self — hides "Add to balancer"
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
  pids?: number[]         // all PIDs of the app; kill/suspend act on the whole set
  name: string
  app_name: string
  cmdline: string
  status?: string         // representative status; 'stopped' when any PID is suspended
  is_self?: boolean       // any PID belongs to SmartTune itself — never signal
  balancer_candidate?: boolean  // false for shells / self — hides "Add to balancer"
  io_read_rate: number    // MB/s
  io_write_rate: number   // MB/s
  io_read_iops: number    // device requests/s (cgroup io.stat rios, not a syscall count)
  io_write_iops: number   // device requests/s (cgroup io.stat wios, not a syscall count)
  io_per_disk?: Record<string, DiskIoRates>  // per whole disk; partitions fold into the parent
  score: number
}

export interface DiskIoRates {
  read_mb_s: number
  write_mb_s: number
  read_iops: number
  write_iops: number
}

export interface AppDiskIoStatsData {
  apps: AppDiskIoEntry[]
}

export interface ProcessEntry {
  pid: number
  name: string
  username: string
  uid: number | null
  cpu_percent: number
  memory_percent: number
  mem_rss_kb: number
  mem_shared_kb: number
  status: string
  create_time: number | null
  cgroup: string
  cmdline: string
  // Present only when fetched with gpu=1 and the PID holds a GPU fd.
  // Keyed by PCI address (drm-pdev); mapped to igpu/dgpu labels client-side.
  gpu_devices?: Record<string, { gpu_util: number; gpu_mem_mb: number }>
  // Present only when fetched with io=1; bytes/s over the polling interval.
  io_read_rate?: number
  io_write_rate?: number
  // SmartTune's own processes — never offered for balancer management or kill.
  is_self?: boolean
  // False for shells / blacklisted daemons / self — hides "Add to balancer".
  balancer_candidate?: boolean
}

export interface ProcessListData {
  count: number
  processes: ProcessEntry[]
}

export interface ProcessDetailData {
  pid: number
  name: string
  exe: string
  cwd: string
  username: string
  status: string
  ppid: number | null
  num_threads: number | null
  num_fds: number | null
  nice: number | null
  create_time: number | null
  cmdline: string
}

export type AppListData = AppInfo[]

// Which channel drove an auto limit. The combined policy caps both off one signal,
// so everything it limits reports 'system_pressure'.
export type AutoLimitReason = 'system_pressure' | 'disk_pressure'

// One app the balancer is auto-limiting. No restore deadline: recovery waits for
// pressure to ease and then runs in stages.
export interface AutoLimitedApp {
  app_id: string
  effective_app_id: string
  app_name: string
  priority: string
  is_controlled: boolean
  status: 'limited' | 'partially_restored'
  limit_reason: AutoLimitReason
  // The channel's level when the limit landed. The UI shows the current level instead
  // (pushed over SSE) and falls back to this before the first push.
  pressure_level: string
  limited_at: number | null
  limit_parts: { cpu_mem_limited?: boolean; io_limited?: boolean }
  cgroups: string[]
  pids: number[]
  representative_pid?: number | null
  // Unified control contract, aligned with AppInfo so the merged table renders
  // both sources through one code path. Always 'AUTO_LIMITED' for these rows.
  control_status?: ControlStatus
  effective?: EffectiveControl | null
  auto_detail?: AutoControlDetail | null
}

// List + current levels in one response, so the tab needs a single request on open.
export interface AutoLimitedAppsData {
  apps: AutoLimitedApp[]
  sys_pressure_level: string
  disk_pressure_level: string
}

// An app the user restored by hand and thereby opted out of auto-limiting. 'app' covers
// every instance of a controlled app; 'instance' covers one cgroup, so siblings of the
// same name stay throttleable. Cleared when the service restarts.
export interface AutoLimitExclusion {
  key: string
  kind: 'app' | 'instance'
  // Why the app is exempt: hand-restored from an auto limit ("user_restore"), or
  // claimed by a manual limit ("manual_limit"). The Excluded tab shows only the former;
  // manual-limit exemptions are represented by their row under Manual Control.
  reason: 'user_restore' | 'manual_limit'
  app_id: string
  app_name: string
  priority: string
  cgroups: string[]
  excluded_at: number
}

export type AutoLimitExclusionsData = AutoLimitExclusion[]

export interface SetControlPayload {
  app_id: string
  app_name: string
  priority: string
  network_priority?: string
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

export interface SetNetworkPriorityPayload {
  app_id: string
  network_priority: string
}

// "Add Application" wizard ------------------------------------------------
// Mirrors balancer/monitor/app_discovery.py::Candidate / ExtractResult and
// the /app/discover_search, /app/discover_extract, /app/wizard_commit
// endpoints in BalanceService.py.
export interface DiscoverCandidate {
  pid: number
  comm: string         // /proc/<pid>/comm — same 15-byte truncation BPF reports
  process_name?: string
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
  cgroup_ids?: string[]
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
  target_cgroups?: string[]
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
  target_processes?: Array<{ pid: number; name?: string }>
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
    guc_fw?: { driver: string; firmware: string; version: string; status: string | null }[]
    huc_fw?: { driver: string; firmware: string; version: string; status: string | null }[]
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
  network: {
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
  monitored_sections_updated_at?: number
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

export interface MonitoredSectionsData {
  sections: string[]
  configured_sections: string[] | null
  all_sections: string[]
  updated_at?: number
}

export interface CollectionData {
  regular_update_sys_pressure_time: number
  updated_at?: number
}

// System-pressure tuning grouped as one settings card: level cut-offs, resource
// weights, and the memory-discount gate steepness.
export interface SystemPressureData {
  thresholds: { low: number; medium: number; high: number; critical: number }
  weights: { cpu: number; memory: number; io: number }
  mem_gate_steepness: number
  memory_busy_threshold: number
  cpu_busy_threshold: number
  updated_at?: number
}

export type LimitPriority = 'high' | 'medium' | 'low' | 'undefined'

export interface LimitRates {
  high?: number
  medium?: number
  low?: number
  undefined?: number
}

export interface DiskIoRateFields {
  write?: number
  read?: number
  write_iops?: number
  read_iops?: number
}

// Media classes the backend recognises (monitor/disk_pressure.py MEDIA_CLASSES).
export type DiskMedia = 'nvme' | 'sata_ssd' | 'mmc' | 'hdd' | 'usb' | 'unknown'

export interface DiskCandidateFloor {
  mb_s?: number
  iops?: number
}

export interface LimitPolicyData {
  policy: string
  cpu: { enabled: boolean; rate: LimitRates }
  memory: { enabled: boolean; rate: LimitRates }
  disk_io: {
    enabled: boolean
    rate: Partial<Record<LimitPriority, DiskIoRateFields>>
    // `rate` above is calibrated for NVMe; these two re-express it per media class --
    // media_scale shrinks the cap, candidate_floor is the "heavy enough to be worth
    // capping" bar an app has to clear on that disk.
    media_scale?: Partial<Record<DiskMedia, number>>
    candidate_floor?: Partial<Record<DiskMedia, DiskCandidateFloor>>
  }
  updated_at?: number
}

export type SaveResult<TOk> =
  | { status: 'ok'; data: TOk }
  | { status: 'conflict'; current: any; message: string }
