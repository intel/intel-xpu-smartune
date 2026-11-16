// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useState } from 'react'
import { Tabs } from 'antd'
import Balance from './Balance'
import DeviceControl from './DeviceControl'

type SubTab = 'apps' | 'devices'

interface Props {
  active: boolean
  balancerEnabled: boolean
  registerKeyword: string | null
  onRegisterConsumed: () => void
  // Bumped by links that mean the apps (Home's Resource Controls card, a held benchmark...).
  appControlIntent?: number
}

// The Control Center holds every control SmarTune offers, split by what it acts on:
// App Control works per application (priority, limits, affinity, network), Device
// Control works per device (frequency) and is independent of any app.
export default function ControlCenter({ active, balancerEnabled, registerKeyword, onRegisterConsumed, appControlIntent = 0 }: Props) {
  const [sub, setSub] = useState<SubTab>('apps')

  // "Add to balancer" from the Processes tab opens the wizard, which lives in App Control.
  useEffect(() => {
    if (registerKeyword) setSub('apps')
  }, [registerKeyword])

  useEffect(() => {
    if (appControlIntent > 0) setSub('apps')
  }, [appControlIntent])

  return (
    <Tabs
      activeKey={sub}
      onChange={(key) => setSub(key as SubTab)}
      items={[
        {
          key: 'apps',
          label: 'App Control',
          children: (
            <Balance
              active={active && sub === 'apps'}
              balancerEnabled={balancerEnabled}
              registerKeyword={registerKeyword}
              onRegisterConsumed={onRegisterConsumed}
            />
          ),
        },
        {
          key: 'devices',
          label: 'Device Control',
          children: <DeviceControl active={active && sub === 'devices'} />,
        },
      ]}
    />
  )
}
