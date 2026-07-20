// Copyright (c) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from 'react'
import { Button, Card, Input, Typography, Alert, Space } from 'antd'
import { DashboardOutlined, LockOutlined } from '@ant-design/icons'
import { login } from '../api/client'
import { COLORS } from '../styles/theme'

interface LoginGateProps {
  onAuthenticated: () => void
}

/**
 * Full-screen access gate shown until the user supplies a valid access token.
 * The token is issued by the server (written to key/api_token, or set via the
 * BALANCER_API_TOKEN env var) and handed to the user by the operator. On a
 * successful /auth/login the token is stored and the main app is rendered.
 */
export default function LoginGate({ onAuthenticated }: LoginGateProps) {
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const submit = async () => {
    if (!token.trim()) {
      setError('Please enter your access token.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const ok = await login(token.trim())
      if (ok) {
        onAuthenticated()
      } else {
        setError('Invalid access token. Please check with your administrator.')
      }
    } catch {
      setError('Could not reach the server. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        background: COLORS.bg,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 16,
      }}
    >
      <Card
        style={{ width: 400, maxWidth: '100%', background: COLORS.headerBg, borderColor: COLORS.border }}
      >
        <Space direction="vertical" size="large" style={{ width: '100%' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div
              style={{
                width: 32,
                height: 32,
                background: `linear-gradient(135deg, ${COLORS.accent} 0%, #3a6fd8 100%)`,
                borderRadius: 6,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <DashboardOutlined style={{ color: '#fff', fontSize: 16 }} />
            </div>
            <Typography.Title level={4} style={{ color: COLORS.text, margin: 0, fontWeight: 600 }}>
              Intel XPU SmarTune
            </Typography.Title>
          </div>

          <Typography.Text style={{ color: COLORS.textMuted }}>
            Enter your access token to continue.
          </Typography.Text>

          {error && <Alert type="error" message={error} showIcon />}

          <Input.Password
            size="large"
            placeholder="Access token"
            prefix={<LockOutlined style={{ color: COLORS.textMuted }} />}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            onPressEnter={submit}
            autoFocus
          />

          <Button type="primary" size="large" block loading={loading} onClick={submit}>
            Sign in
          </Button>
        </Space>
      </Card>
    </div>
  )
}
