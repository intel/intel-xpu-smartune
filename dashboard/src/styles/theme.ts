// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { theme } from 'antd'
import type { ThemeConfig } from 'antd'

export type ColorMode = 'light' | 'dark'

const LIGHT_COLORS = {
  bg: '#f4f7fb',
  panelBg: '#ffffff',
  border: '#d9e1ec',
  green: '#3f8600',
  yellow: '#ad6800',
  orange: '#d46b08',
  red: '#cf1322',
  accent: '#1677ff',
  tabAccent: '#0068b5',
  text: '#111827',
  textMuted: '#4b5563',
  textTertiary: '#667085',
  textPlaceholder: '#6b7280',
  headerBg: '#edf2f7',
  rowAlt: '#f0f5ff',
  canvas: '#f8fafc',
  surfaceSubtle: '#edf2f7',
  borderStrong: '#cbd5e1',
  accentSoft: '#e6f4ff',
  accentGrid: 'rgba(22, 119, 255, 0.06)',
  shadow: 'rgba(15, 23, 42, 0.08)',
  shadowStrong: 'rgba(15, 23, 42, 0.14)',
  overlay: 'rgba(248, 250, 252, 0.92)',
  gaugeLow: '#0068b5',
  gaugeNeedle: '#1f4b7a',
  gaugeTrack: '#dbe7f3',
  gaugeSurface: '#f1f6fb',
  gaugeBorder: '#c9d9eb',
  chartContrast: '#1f4b7a',
}

const DARK_COLORS = {
  bg: '#0f1117',
  panelBg: '#1a1d2e',
  border: '#2d3149',
  green: '#73bf69',
  yellow: '#fade2a',
  orange: '#f2495c',
  red: '#c4162a',
  accent: '#5794f2',
  tabAccent: '#5794f2',
  text: '#d9d9d9',
  textMuted: '#8e9ab3',
  textTertiary: '#8e9ab3',
  textPlaceholder: '#8e9ab3',
  headerBg: '#141720',
  rowAlt: '#1e2235',
  canvas: '#0b0f14',
  surfaceSubtle: '#141720',
  borderStrong: '#3a4260',
  accentSoft: 'rgba(87, 148, 242, 0.12)',
  accentGrid: 'rgba(120, 176, 255, 0.08)',
  shadow: 'rgba(5, 10, 20, 0.7)',
  shadowStrong: 'rgba(0, 0, 0, 0.38)',
  overlay: 'rgba(9, 12, 20, 0.88)',
  gaugeLow: '#73bf69',
  gaugeNeedle: '#d9d9d9',
  gaugeTrack: '#2d3149',
  gaugeSurface: '#1a1d2e',
  gaugeBorder: '#2d3149',
  chartContrast: '#ffffff',
}

type ColorPalette = typeof LIGHT_COLORS

export const COLOR_MODES: Record<ColorMode, ColorPalette> = {
  light: LIGHT_COLORS,
  dark: DARK_COLORS,
}

export const COLORS = { ...LIGHT_COLORS }

function createTheme(colors: typeof COLORS, algorithm: ThemeConfig['algorithm']): ThemeConfig {
  return {
    algorithm,
  token: {
    colorBgBase: colors.bg,
    colorBgContainer: colors.panelBg,
    colorBgElevated: colors.panelBg,
    colorBorderSecondary: colors.border,
    colorBorder: colors.border,
    colorPrimary: colors.accent,
    colorText: colors.text,
    colorTextSecondary: colors.textMuted,
    colorTextTertiary: colors.textTertiary,
    colorTextQuaternary: colors.textTertiary,
    colorTextPlaceholder: colors.textPlaceholder,
    borderRadius: 4,
    fontFamily: "'Sora', 'Space Grotesk', 'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
  },
  components: {
    Tabs: {
      inkBarColor: colors.tabAccent,
      itemActiveColor: colors.tabAccent,
      itemSelectedColor: colors.tabAccent,
      itemColor: colors.textMuted,
      cardBg: colors.panelBg,
    },
    Table: {
      headerBg: colors.headerBg,
      headerColor: colors.textMuted,
      rowHoverBg: colors.rowAlt,
      borderColor: colors.border,
    },
    Card: {
      colorBgContainer: colors.panelBg,
    },
    Select: {
      colorBgContainer: colors.panelBg,
    },
    Input: {
      colorBgContainer: colors.panelBg,
    },
    Button: {
      colorPrimary: colors.accent,
    },
  },
}
}

export const lightTheme = createTheme(COLOR_MODES.light, theme.defaultAlgorithm)
export const darkTheme = createTheme(COLOR_MODES.dark, theme.darkAlgorithm)

export function setColorMode(mode: ColorMode): void {
  const colors = COLOR_MODES[mode]
  Object.assign(COLORS, colors)
  for (const [name, value] of Object.entries(colors)) {
    document.documentElement.style.setProperty(`--color-${name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`, value)
  }
}

export function getPressureColor(value: number): string {
  if (value < 0.6) return COLORS.gaugeLow
  if (value < 0.8) return COLORS.yellow
  if (value < 1.0) return COLORS.orange
  return COLORS.red
}

export function getPressureLabel(value: number): string {
  if (value < 0.6) return 'LOW'
  if (value < 0.8) return 'MEDIUM'
  if (value < 1.0) return 'HIGH'
  return 'CRITICAL'
}
