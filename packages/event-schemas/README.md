# event-schemas

领域事件 JSON Schema（语言中立），事件清单见 PRD 第 12.2 节。

命名规范：`<domain>/<event>.json`，事件类型字符串形如 `order/intent.created`。

所有事件必须包含 `envelope.json` 定义的公共信封字段：稳定 ID、时间戳、策略版本、数据版本和权限上下文。
