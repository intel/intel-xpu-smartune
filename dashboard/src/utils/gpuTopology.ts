import type { GpuTopology } from '../api/types'

export interface GpuComputeUnits {
  label: 'Xe-cores' | 'EU Count'
  count: number
  short: string
}

const isPositive = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v > 0

export function getGpuComputeUnits(
  driver: string | null | undefined,
  topology?: GpuTopology | null,
  euCount?: number | null,
): GpuComputeUnits | null {
  if (driver === 'xe') {
    const cores = topology?.xe_core_count
    return isPositive(cores) ? { label: 'Xe-cores', count: cores, short: `${cores} Xe-cores` } : null
  }
  const eus = topology?.eu_count ?? euCount
  return isPositive(eus) ? { label: 'EU Count', count: eus, short: `${eus} EU` } : null
}
