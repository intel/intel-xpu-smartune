import { Space, Tag, Typography } from 'antd'
import { COLORS } from '../styles/theme'
import { useAlertPolicies } from '../utils/alertPolicy'

const { Text } = Typography

// Explains which events create alerts and how each alert ends; shown in
// floating panels next to alert views. The policies are the server's
// (GET /diag/alert-policies), so this list cannot drift from what alerts.
export default function AlertPolicyList() {
  const { policies, cooldownSeconds, loaded } = useAlertPolicies()
  const cooldownMinutes = cooldownSeconds ? Math.round(cooldownSeconds / 60) : null
  return (
    <Space direction='vertical' size={8} style={{ width: 380 }}>
      <Text type='secondary'>Only these event types, and custom rules whose Alert setting is on, create Alerts. Every other event remains diagnostic evidence, even when its Severity is error or critical.</Text>
      {!loaded ? <Text type='secondary'>Loading the alert policy…</Text> : null}
      {policies.map((policy) => (
        <div key={policy.event_type}>
          <Space size={6} wrap>
            <Tag color={policy.severity === 'critical' ? COLORS.red : COLORS.orange}>{policy.severity.toUpperCase()}</Tag>
            <Text code>{policy.event_type}</Text>
          </Space>
          <Text type='secondary' style={{ display: 'block', marginTop: 4 }}>{policy.description}</Text>
        </div>
      ))}
      <Text type='secondary'>
        Repeated observations with the same event type and scope are merged into one Alert.
        {cooldownMinutes ? ` Notifications for the same Alert are cooled down for ${cooldownMinutes} minutes.` : ''}
        {' '}An Alert is Open until what ends it happens (listed above). Acknowledging closes only the one-off faults; for the others it, like silencing, just marks the Alert as being handled.
      </Text>
      <Text type='secondary'>An Alert's severity is its policy's (listed above), and an escalation rule can raise it; the events behind it keep the severity their source reported.</Text>
    </Space>
  )
}
