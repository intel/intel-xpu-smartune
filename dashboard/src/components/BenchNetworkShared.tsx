// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// The benchmark network settings as shown in more than one place: the Settings
// page edits them; the Models tab's Environment drawer only shows them, with a
// way to Settings. One wording, so the two agree.

import React from 'react'
import { Alert, Space, Tag, Typography } from 'antd'

import type { BenchNetSource, BenchNetworkData, BenchNetworkTestResult } from '../api/types'

const { Text } = Typography

const SOURCE_LABEL: Record<BenchNetSource, string> = {
  settings: 'Settings',
  config: 'config.yaml',
  env: 'the service environment',
  none: 'default',
}

function from(entry: { source: BenchNetSource }): string {
  return entry.source === 'none' ? '' : ` (from ${SOURCE_LABEL[entry.source]})`
}

/** The proxy, endpoint and token in force, one phrase each. */
export function networkLines(info: BenchNetworkData): { proxy: string; endpoint: string; token: string } {
  const eff = info.effective
  // HTTPS is what PyPI and the hub are reached over; it follows the HTTP proxy
  // when only that one is set, so this is the proxy actually in use.
  const proxy = eff.https_proxy.value ? eff.https_proxy : eff.http_proxy
  return {
    proxy: eff.http_proxy.direct
      ? 'none, direct connection forced in Settings'
      : proxy.value
        ? `${proxy.value}${from(proxy)}`
        : 'none (direct connection)',
    endpoint: `${eff.hf_endpoint.value}${from(eff.hf_endpoint)}`,
    token: eff.hf_token.set
      ? `${eff.hf_token.preview ?? 'set'}${from(eff.hf_token)}`
      : 'none (public models only)',
  }
}

/**
 * A warning, not an error, on a proxy address that starts with https://.
 *
 * A proxy really reached over TLS exists, but it is rare. The common case is a
 * plain proxy typed as https:// because it is the HTTPS proxy: Python's urllib
 * (the model list) ignores the scheme and works, while uv, pip and model
 * downloads open TLS to the proxy and fail with "tls handshake eof".
 */
export const PROXY_SCHEME_WARNING = {
  warningOnly: true,
  validator: (_: unknown, value?: string) =>
    /^https:\/\//i.test(String(value ?? '').trim())
      ? Promise.reject(new Error(
          'Usually http://, even for HTTPS traffic. With https://, uv and pip use TLS ' +
          'to reach the proxy itself and fail unless it supports that. Test connection checks it.'))
      : Promise.resolve(),
}

const CHECK_LABEL: Record<string, string> = {
  proxy_tls: 'Proxy address (https://)',
  pypi: 'PyPI (environment builds)',
  hf_endpoint: 'HuggingFace endpoint',
  hf_token: 'HuggingFace token',
}

function checkLabel(name: string): string {
  if (CHECK_LABEL[name]) return CHECK_LABEL[name]
  return name.startsWith('proxy_tls') ? CHECK_LABEL.proxy_tls : name
}

/** The outcome of POST /bench/network/test, one line per check. Closable. */
export function NetworkTestAlert({ result, onClose }: {
  result: BenchNetworkTestResult
  onClose: () => void
}) {
  return (
    <Alert
      type={result.ok ? 'success' : 'warning'}
      showIcon
      closable
      onClose={onClose}
      message={result.ok ? 'All checks passed' : 'Some checks failed'}
      description={
        <Space direction="vertical" size={2}>
          {result.checks.map((check) => (
            <Text key={check.name} style={{ fontSize: 12 }}>
              <Tag color={check.ok ? 'success' : check.ok === null ? 'default' : 'error'}>
                {check.ok ? 'OK' : check.ok === null ? 'Skipped' : 'Failed'}
              </Tag>
              <b>{checkLabel(check.name)}</b>
              {check.via && <> via {check.via}</>}
              {check.ok && check.user && <>: signed in as {check.user}</>}
              {check.ok && check.elapsed_ms !== undefined && <> ({check.elapsed_ms} ms)</>}
              {!check.ok && check.detail && <>: {check.detail}</>}
            </Text>
          ))}
        </Space>
      }
    />
  )
}
