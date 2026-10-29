import { Space, Tag, Typography } from 'antd'
import { COLORS } from '../styles/theme'
import { ALERT_POLICY_SUMMARIES } from '../utils/alertPolicy'

const { Text } = Typography

// Explains which events create alerts and how each alert ends; shown in
// floating panels next to alert views.
export default function AlertPolicyList() {
  return (
    <Space direction='vertical' size={8} style={{ width: 380 }}>
      <Text type='secondary'>Only these event types create Alerts. Other events, including custom rules, remain diagnostic evidence even when their Severity is error or critical.</Text>
      {ALERT_POLICY_SUMMARIES.map((policy) => (
        <div key={policy.eventType}>
          <Space size={6} wrap>
            <Tag color={policy.severity === 'critical' ? COLORS.red : COLORS.orange}>{policy.severity.toUpperCase()}</Tag>
            <Text code>{policy.eventType}</Text>
          </Space>
          <Text type='secondary' style={{ display: 'block', marginTop: 4 }}>{policy.behavior}</Text>
        </div>
      ))}
      <Text type='secondary'>Repeated observations with the same event type and scope are merged into one Alert. Notifications for the same Alert are cooled down for 5 minutes. An Alert is Open until what ends it happens (listed above). Acknowledging closes only the one-off faults; for the others it, like silencing, just marks the Alert as being handled.</Text>
    </Space>
  )
}
