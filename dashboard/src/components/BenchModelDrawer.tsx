// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// One model's full detail and settings, over the page rather than in it.
//
// A tile carries what can be read at a glance -- precision, device, runtime,
// whether the weights are here. Everything else a model has (its OpenVINO
// variants, popularity, when the conversion was last updated) and every control
// that changes its settings lives here, in BenchModelDetail, unchanged: it was
// already exactly this pane, and used to be shown inline under a strip of tabs.
//
// A drawer and not a modal because what it edits is on the page behind it. The
// tiles stay visible at the left edge while a model's precisions are being
// picked, so "is this the one I meant" needs no dismissing to answer.

import React from 'react'
import { Drawer } from 'antd'

import type { BenchModel, BenchPrecision } from '../api/types'
import BenchModelDetail, { type ModelParams } from './BenchModelDetail'

interface Props {
  open: boolean
  /** null while the drawer animates shut, or for a ticked id the list no longer holds. */
  model: BenchModel | null
  params: ModelParams
  onParamsChange: (next: ModelParams) => void
  onClose: () => void
  ovChoices: string[]
  installedOvs: string[]
  onInstallOv: (version: string) => void
  ready: boolean
  busy: boolean
  probing: boolean
  onOpenEnv: () => void
  onDeleteLocal: (model: BenchModel, precisions: BenchPrecision[]) => void
}

export default function BenchModelDrawer({
  open,
  model,
  params,
  onParamsChange,
  onClose,
  ...rest
}: Props) {
  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={620}
      // The model's own id is the heading; BenchModelDetail's card title carries
      // it too, and repeating it in the frame would be the only thing the frame
      // said.
      title={model ? (model.id.split('/').pop() ?? model.id) : 'Model'}
      styles={{ body: { padding: 12 } }}
    >
      <BenchModelDetail
        model={model}
        params={params}
        onParamsChange={onParamsChange}
        {...rest}
      />
    </Drawer>
  )
}
