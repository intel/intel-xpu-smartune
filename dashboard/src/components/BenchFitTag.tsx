// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The verdict the two preflight dialogs end on: does this fit.
//
// Shared so that "Tight" means the same thing in the memory check and in the
// disk check, and so the colours do not drift apart. What counts as tight is
// each check's own business (memory keeps a ratio of the device's free memory,
// disk keeps an absolute margin for logs and results) -- this only renders the
// answer.

import React from 'react'
import { Tag, Tooltip } from 'antd'

import { COLORS } from '../styles/theme'
import type { FitVerdict } from '../utils/benchMemory'

const LABELS: Record<FitVerdict, { text: string; color?: string; hint: string }> = {
  fits: {
    text: 'Fits',
    color: COLORS.green,
    hint: 'Comfortably within what is free now.',
  },
  tight: {
    text: 'Tight',
    color: COLORS.yellow,
    hint: 'Fits, but with little to spare — overhead that these estimates leave out may not.',
  },
  full: {
    text: "Won't fit",
    color: COLORS.red,
    hint: 'Needs more than is free now; it may fail or fall back to swapping.',
  },
  unknown: {
    text: 'Unknown',
    hint: 'Not enough information to judge — a figure on one side of the comparison is missing.',
  },
}

export default function BenchFitTag({ verdict }: { verdict: FitVerdict }) {
  const { text, color, hint } = LABELS[verdict]
  return (
    <Tooltip title={hint}>
      <Tag
        color={color}
        // The yellow is a light one: black text reads on it, the theme's own
        // does not.
        style={{ marginInlineEnd: 0, color: verdict === 'tight' ? '#000' : undefined }}
      >
        {text}
      </Tag>
    </Tooltip>
  )
}
