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
| approval/requested | approval/requested.json |
| approval/approved | approval/approved.json |
| approval/rejected | approval/rejected.json |
| risk/evaluated | risk/evaluated.json |

兼容性规则：字段只允许新增（向后兼容），不允许删除或收窄类型；
新增事件类型必须同步登记到 envelope.json 的 enum，CI 会校验目录与 enum 一致。
