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
  // Live process details grouped by the limited cgroup. This is especially
  // important for unregistered apps, which have no controlled-app snapshot.
  scope_processes?: Record<string, ScopeProcess[]>
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
  // Present only while a measured benchmark run holds quiet mode. The payload is
  // then whatever was last cached rather than a fresh collection -- the endpoint
  // refuses to query hardware while the gate is up -- so a reader must say so
  // instead of drawing it as current. `cached_at` is null for a section that was
  // never cached (monitor_api.py _respond_dynamic_* ).
  quiet_mode?: { active: boolean; cached_at: number | null }
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

// --- Benchmark (benchmark/service/) ---------------------------------------------------

export type BenchStage = 'build' | 'benchmark' | 'all'
export type BenchPrecision = 'fp16' | 'int8' | 'int4'
// Lowercase here and in result rows; the API accepts either and the pipeline
// spells them uppercase (benchmark/service/runner.py VALID_DEVICES).
export type BenchDevice = 'cpu' | 'gpu' | 'npu'
export type BenchJobStatus = 'running' | 'done' | 'failed' | 'cancelled'

// A background job: either the environment setup or a pipeline run. Both are
// reported with the same shape (benchmark/service/jobs.py Job.to_dict).
export interface BenchJob {
  id: string
  kind: 'setup' | 'run'
  status: BenchJobStatus
  returncode: number | null
  started_at: number
  finished_at: number | null
  duration: number
  log_path: string
  meta: {
    stage?: BenchStage
    force?: boolean
    models?: { id: string; build?: string }[]
    // Uppercase, as the pipeline spells them. Absent on a build-only job and on
    // runs started before the request could name devices.
    devices?: string[]
    // BENCH_RUN_NAME: what every result directory this job wrote is named after.
    run_name?: string
    // Whether this run held quiet mode from start to finish. False means the
    // user restored full monitoring part-way through, so the results were taken
    // in a dirtied environment and are not comparable with other runs. Absent on
    // build-only jobs (nothing to measure) and on runs from before quiet mode.
    quiet_held?: boolean
  }
  // Present only when the request asked for a log tail (?offset=).
  chunk?: string
  offset?: number
  size?: number
}

export interface BenchEnvData {
  enabled: boolean
  env_root: string
  src_root: string
  venv_dir: string
  venv_exists: boolean
  venv_usable: boolean
  // The active (base) venv can import huggingface_hub -- enough to list and
  // download models. OpenVINO is built per-version on demand at benchmark time.
  hf_ready: boolean
  genai_ready: boolean
  // hf_ready: a run can be started -- the build/download stage needs only the
  // `hf` CLI, and a benchmark builds its OpenVINO column on demand.
  ready: boolean
  // The venv's package list is still being read on a server-side background
  // thread. Until it clears, `versions` is empty and `venv_usable`/`ready` are
  // false because the answer is unknown -- not because anything is wrong.
  probing: boolean
  versions: Record<string, string | null>
  // OpenVINO columns already built on disk, and the one the active venv points
  // at. May be empty / null until a benchmark has built a column; no longer
  // drives a switch UI (kept for diagnostics).
  ov_versions: string[]
  active_ov: string | null
  // Each BUILT OpenVINO version and the exact packages inside its venv. What the
  // Environment drawer's dropdown lists; empty until a version has been built, so
  // the drawer's package list is empty on a fresh environment.
  ov_versions_detail: { version: string; packages: Record<string, string | null> }[]
  // Static reference of known releases -> package versions. Not shown in the
  // drawer; only seeds the Models tab's version suggestions.
  ov_reference: { version: string; packages: Record<string, string> }[]
  models_dir: string
  model_count: number
  setup_script: string
  setup_job: BenchJob | null
  // Whichever job currently holds the single execution slot, if any.
  busy: BenchJob | null
}

/** One OpenVINO conversion of a source model, as cached by search_models.py. */
export interface BenchModelVariant {
  repo: string
  // null when the repo name carries no recognisable weight-format suffix.
  precision: BenchPrecision | null
}

// A benchmarkable model. `task`/`downloads`/`likes` describe the OpenVINO
// conversion rather than the original repo — that is what the cache enumerates,
// and reading the source repo's own stats would cost one HF lookup per model.
export interface BenchModel {
  id: string
  task: string | null
  downloads: number
  likes: number
  last_modified: string | null
  variants: BenchModelVariant[]
  // Precisions this model can be downloaded in, derived from `variants`.
  precisions: BenchPrecision[]
  // Per-precision presence in the runtime IR directory. Only lists precisions
  // that are either offered or already present.
  local: Partial<Record<BenchPrecision, boolean>>
  downloaded: boolean
}

// Cache status without the list, as carried by the `models` SSE event.
export interface BenchModelsState {
  total: number
  // 1 = the original list-of-ids cache, still readable; 2 = objects with variants.
  version: number
  updated_at: string | null
  refreshing: boolean
  last_error: string | null
  cache_file: string
  cached: boolean
}

export interface BenchModelsData extends BenchModelsState {
  models: BenchModel[]
  count: number
  matched: number
}

// --- /bench/events -------------------------------------------------------
// The server pushes these instead of the tab polling for them; see
// benchmark/service/events.py.

/** An incremental slice of a job's log, at absolute byte offsets. */
export interface BenchLogDelta {
  job_id: string
  start: number
  end: number
  chunk: string
  // Snapshot only: the stream joined a job already in progress, so `start` is
  // not the beginning of the log.
  truncated?: boolean
}

export type BenchEvent =
  | {
      type: 'snapshot'
      env: BenchEnvData
      job: BenchJob | null
      log: BenchLogDelta | null
      models: BenchModelsState | null
      results_rev: number
    }
  | { type: 'job'; job: BenchJob }
  | ({ type: 'log' } & BenchLogDelta)
  | { type: 'env'; env: BenchEnvData }
  | { type: 'models'; models: BenchModelsState }
  | { type: 'results'; rev: number }

export interface BenchRunsData {
  current: BenchJob | null
  recent: BenchJob[]
}

/**
 * Quiet mode: the gate that stands SmarTune's own background activity down for
 * the duration of a measured run, so the load a run competes with does not
 * depend on which dashboard page happens to be open.
 */
export interface BenchQuietModeState {
  /** Is the gate in effect right now? False while the user has it dropped. */
  active: boolean
  /** Does a run hold quiet mode at all? False outside a measured run. */
  held: boolean
  /** The run id holding it, for logs/diagnosis. */
  owner: string | null
  /** Epoch seconds the hold started. */
  since: number | null
  /** Has the user dropped the gate during this hold? Latches until the run ends. */
  user_exited: boolean
}

/** One reason a measured run cannot start. */
export interface BenchPreflightBlocker {
  name: string
  reason?: string
  action?: string
  apps?: { app_id: string | null; app_name: string | null }[]
}

export interface BenchPreflightData {
  blocked: boolean
  blockers: BenchPreflightBlocker[]
  quiet_mode: BenchQuietModeState
}

/**
 * The running run's most recent hardware sample, straight off the sampler that
 * is already writing metrics.csv. Column names are that CSV's schema
 * (benchmark/service/sampler.py COLUMNS) -- CPU/memory/GPU/NPU only, since it
 * collects no disk or network counters.
 */
export interface BenchRunSampleData {
  row: Record<string, number | null> | null
  /** False once the run's sampler has been torn down. */
  sampling: boolean
  period_s: number | null
  quiet_mode: BenchQuietModeState
}

export interface BenchResultRow {
  model: string
  quant: string
  status: string
  task?: string
  device?: string
  port?: string
  model_dir?: string
  log_file?: string
  // KPIs scraped from detail.log plus hardware medians over the case's
  // measurement window. Absent for a case the aggregation step could not parse.
  metrics?: Record<string, number>
  // How the aggregator classified the case: device, precision, mode, batch_size.
  // Its spellings, not summary.tsv's -- see BenchMatrixRow.
  dimensions?: Record<string, string>
  // One line from detail.log saying why a failed case failed. Failures differ:
  // "the NPU compiler rejected this quantisation" and "the device fell off the
  // bus" call for different responses, and "failed" alone says neither.
  failure_reason?: string | null
}

export interface BenchResultRun {
  backend: string
  run: string
  dir: string
  updated_at: number
  report: string | null
  rows: BenchResultRow[]
  // Metric keys present on this run's rows, in the order they were first seen.
  metric_columns: string[]
  ok: number
  failed: number
}

export interface BenchResultsData {
  runs: BenchResultRun[]
  count: number
  // Descriptors for every metric key any run produced. Optional so an older
  // server still renders, just with derived column titles.
  metrics?: BenchMetricMeta[]
  primary_metric?: string | null
  benchmarks_dir: string
  metrics_available: boolean
}

// What a metric key means, so a chart can label an axis and know which end of it
// is good without the frontend keeping its own table of every KPI the pipeline
// might emit.
export interface BenchMetricMeta {
  key: string
  label: string
  unit: string | null
  // Three-valued on purpose. null means the metric has no direction -- a clock
  // frequency or an input length is worth showing and meaningless to rank -- so
  // "best in row" is left unmarked rather than picked arbitrarily.
  higher_is_better: boolean | null
  group: string
  group_label: string
  // What the number actually means, where the name does not say. Mostly the
  // derived ratios, whose denominator decides how they read -- a column of
  // 1.00× means "this row is the baseline" for one metric and "this sweep had
  // nothing to compare with" for another. Shown on hover.
  description?: string | null
}

// One case, flattened out of its run directory. A run holds a single device, so
// every cross-device comparison spans several of them; the backend does that
// join and hands back a plain long table.
export interface BenchMatrixRow {
  backend: string
  // The run directory this case was measured in. Benchmarking the same model,
  // precision and device again produces another row with another run, not a
  // replacement -- every repetition is kept.
  run: string
  // The invocation the run directory belongs to. One job sweeps every device it
  // was asked for, so several runs ("<job>_CPU", "<job>_NPU") share one job --
  // that is what the Results tab folds by.
  job: string
  // When the job started, from the timestamp its name carries. null for a tree
  // whose directories were not named by runner.py (the legacy "TEST" ones);
  // read `updated_at` instead.
  job_started_at: number | null
  // When that run last wrote its summary, for ordering repetitions by age.
  updated_at: number
  model: string
  // Lowercase, matching the aggregator: summary.tsv writes "NPU" where the
  // medians CSV writes "npu", and grouping on the raw string would split one
  // device into two.
  device: string
  // "int4" -- the weight format alone, where `quant` is the exported variant
  // ("int4_ov", "int4_cw_ov") that names the repository it came from.
  precision: string
  quant: string
  mode: string
  batch_size: string
  status: string
  task: string
  case_dir: string
  log_file: string
  model_dir: string
  metrics: Record<string, number>
  // Failed cases are returned too, with the reason: a chart that dropped them
  // would show CPU and GPU with no hint that NPU was ever attempted.
  failure_reason?: string | null
}

export interface BenchMatrixData {
  rows: BenchMatrixRow[]
  // Distinct values per axis, sorted -- what the selectors offer.
  dimensions: {
    models: string[]
    devices: string[]
    precisions: string[]
    backends: string[]
    runs: string[]
    // Newest first, unlike the others: a job is a moment, and the one a reader
    // wants at the top is the last one they started.
    jobs: string[]
  }
  metrics: BenchMetricMeta[]
  // The metric the pipeline's report profile calls the headline one, and so the
  // one that decides which repetition of a test was the best. null when no
  // pivot report has been produced yet.
  primary_metric?: string | null
  benchmarks_dir: string
  metrics_available: boolean
}

// The hardware samples taken while one case was being measured -- what the
// medians reported everywhere else were taken over. A median cannot tell a case
// that ramped from 15 W to 40 W from one that sat at 28 W; this can.
export interface BenchTimelineData {
  case_dir: string
  // Epoch seconds bounding the measurement window, as the aggregation step
  // defines it. Shown only as a duration; `t` is what the chart plots.
  start: number
  end: number
  duration_s: number
  count: number
  // Seconds into the window, one per sample.
  t: number[]
  // Keyed by the *median* metric name (`cpu_usage_percent_median`), so the same
  // descriptors that label a table column label this chart's axis. A gap in a
  // series is null -- a collector that missed a tick, never a zero.
  series: Record<string, (number | null)[]>
}

// Bench actions distinguish three outcomes rather than two: the slot being busy
// and "an environment already exists" are both 409s that the UI answers with a
// prompt, not an error toast.
export type BenchActionResult<TOk> =
  | { status: 'ok'; data: TOk }
  | { status: 'conflict'; data: any; message: string }
