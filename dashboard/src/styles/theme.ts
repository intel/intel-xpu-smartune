// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import { theme } from 'antd'
import type { ThemeConfig } from 'antd'

export const COLORS = {
  bg: '#0f1117',
  panelBg: '#1a1d2e',
  border: '#2d3149',
  green: '#73bf69',
  yellow: '#fade2a',
  orange: '#f2495c',
  red: '#c4162a',
  accent: '#5794f2',
  text: '#d9d9d9',
  textMuted: '#8e9ab3',
  headerBg: '#141720',
  rowAlt: '#1e2235',
}

export const darkTheme: ThemeConfig = {
  algorithm: theme.darkAlgorithm,
  token: {
    colorBgBase: COLORS.bg,
    colorBgContainer: COLORS.panelBg,
    colorBgElevated: COLORS.panelBg,
    colorBorderSecondary: COLORS.border,
    colorBorder: COLORS.border,
    colorPrimary: COLORS.accent,
    colorText: COLORS.text,
    colorTextSecondary: COLORS.textMuted,
    borderRadius: 4,
    fontFamily: "'Sora', 'Space Grotesk', 'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
  },
  components: {
    Tabs: {
      inkBarColor: COLORS.accent,
      itemActiveColor: COLORS.accent,
      itemSelectedColor: COLORS.accent,
      itemColor: COLORS.textMuted,
      cardBg: COLORS.panelBg,
    },
    Table: {
      headerBg: COLORS.headerBg,
      rowHoverBg: COLORS.rowAlt,
      borderColor: COLORS.border,
    },
    Card: {
      colorBgContainer: COLORS.panelBg,
    },
    Select: {
      colorBgContainer: COLORS.panelBg,
    },
    Input: {
      colorBgContainer: COLORS.panelBg,
    },
    Button: {
      colorPrimary: COLORS.accent,
    },
  },
}

export function getPressureColor(value: number): string {
  if (value < 0.6) return COLORS.green
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
