# Event Schemas

领域事件 JSON Schema（语言中立）。事件必须包裹在 `envelope.json` 定义的
信封中：`event_type` 决定 `payload` 应校验的 schema。

## 事件目录

| event_type | payload schema |
|---|---|
| order/intent.created | order/intent.created.json |
| order/submitted | order/submitted.json |
| order/filled | order/filled.json |
| order/cancelled | order/cancelled.json |
| order/acknowledged | order/acknowledged.json |
| order/partially_filled | order/partially_filled.json |
| order/rejected | order/rejected.json |
| approval/requested | approval/requested.json |
| approval/approved | approval/approved.json |
| approval/rejected | approval/rejected.json |
| account/reconciled | （payload 自由，按 envelope 校验） |
| account/mismatch | account/mismatch.json |
| bot/task.created | bot/task.created.json |
| bot/task.transitioned | bot/task.transitioned.json |
| market/chief.summary | market/chief.summary.json |

兼容性规则：字段只允许新增（向后兼容），不允许删除或收窄类型；
新增事件类型必须同步登记到 envelope.json 的 enum，CI 会校验目录与 enum 一致。
