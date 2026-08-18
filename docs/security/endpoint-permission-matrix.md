# 端点权限矩阵

配合 `docs/security/identity-trust-model.md`。
Identity/IAP 里程碑**保持开启**；三项传输 ADR 仍为 `PROPOSED/UNDECIDED`。
浏览器只打 IAP 与 Next BFF。Gateway / Projection / Incident / Evolution / 量化系统对浏览器不可达。

列含义：

| 列 | 含义 |
| --- | --- |
| 调用方 | 谁发起。人只出现在 BFF 行 |
| 服务 scope | 下游服务身份需要的 scope；`—` 表示不调该服务或只读无 Key |
| 用户 role | 人必须具备的角色；服务间调用为人 `—` |
| CSRF | 浏览器写操作必须有 |
| 审计 | 是否必须留下 `service_principal` + `actor_principal`（服务间调用至少有 service） |
| 失败 | 缺身份 / 缺角色 / 校验失败时的状态码 |

未登录或 Token 无效：BFF **401**。
已登录但角色不够：BFF **403**。
生产未配置 OIDC/IAP：写 BFF **503** 或拒启。
`decided_by` 伪造：仍 **200/业务码**，但主体被覆盖为 `actor_principal`（不得采用客户端值）。

`LIVE` 模式选择器：无此端点。

## Next BFF（人 → 浏览器）

现有路由先按今日代码列出；实现 PR 只改鉴权，不借机加交易能力。

| HTTP | 端点 | 调用方 | 服务 scope | 用户 role | CSRF | 审计 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/` 总控制台（三 Bot 卡片） | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/chief` 或 `/chat` Chief 只读 | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/crypto` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/a-share` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/approvals` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/tasks` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/incidents` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/portfolio` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/strategy-lab` 或 `/strategies` | 浏览器 | Projection 只读 | Viewer | 否 | 否 | 401 |
| GET | `/system` | 浏览器 | 各服务 health 只读 | Viewer | 否 | 否 | 401 |
| GET | `/api/csrf` | 浏览器 | — | Viewer | 否 | 否 | 401 |
| GET | `/api/projection/*` | 浏览器 | —（BFF→Projection） | Viewer | 否 | 否 | 401 |
| POST | `/api/projection/*` | 浏览器 | 禁止写 Projection | — | 是 | 否 | 405/403 |
| POST | `/api/chief/query` | 浏览器 | —（BFF→Projection） | Viewer | 否 | 否 | 401 |
| POST | `/api/approvals/:id/decide` | 浏览器 | Gateway `write` | Approver | 是 | 是 | 401/403/503 |
| POST | `/api/control/emergency-stop` | 浏览器 | Gateway `write` | RiskOperator | 是 | 是 | 401/403/503 |
| POST | `/api/control/kill-switch/resume`（待加） | 浏览器 | Gateway `write` | RiskOperator | 是 | 是 | 401/403/503 |
| POST | `/api/incidents/:id/mitigate`（待加） | 浏览器 | Incident 服务身份 | RiskOperator | 是 | 是 | 401/403/503 |
| POST | `/api/incidents/:id/resolve`（待加） | 浏览器 | Incident 服务身份 | RiskOperator | 是 | 是 | 401/403/503 |
| POST | `/api/strategies/:id/promote-decide`（待加） | 浏览器 | Gateway/Evolution `write` | StrategyReviewer | 是 | 是 | 401/403/503 |
| POST | `/api/identity/mappings`（待加） | 浏览器 | 本项目映射（仅 ADR-3=C） | IdentityAdmin | 是 | 是 | 401/403/503 |
| POST | Outbox `replay/skip/terminate`（待加） | 浏览器 | 对应服务本地 | IdentityAdmin 不足；需另定运维角色或 RiskOperator+审计。**第一版：不开放给人，只跑服务端工具** | 是 | 是 | 403 |

说明：

- Approver **不能**调用 emergency-stop / resume。
- IdentityAdmin **不能**调用 decide。
- StrategyReviewer **不能**调用交易 decide。
- 待加行是权限预留，不是本设计 PR 的实现范围。

## Quant Gateway（仅服务）

人不到达。`服务 scope` 是调用方 API Key。`用户 role` 对人写操作为 BFF 已检过的角色；Gateway 仍必须有用户 actor。

| HTTP | 端点 | 调用方 | 服务 scope | 用户 role | CSRF | 审计 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/healthz` | 探活 | — | — | 否 | 否 | — |
| GET | `/v1/markets/{m}/health` | BFF、Chief、市场 Bot | `read` | — 或 Viewer（经 BFF） | 否 | 否 | 401/403 |
| GET | `/v1/markets/{m}/positions` | 同上 | `read` | 同上 | 否 | 否 | 401/403 |
| GET | `/v1/markets/{m}/accounts` | 同上 | `read` | 同上 | 否 | 否 | 401/403 |
| GET | `/v1/markets/{m}/signals` | 同上 | `read` | 同上 | 否 | 否 | 401/403 |
| GET | `/v1/markets/{m}/orders/{id}` | 同上 | `read` | 同上 | 否 | 否 | 401/403 |
| GET | `/v1/idempotency-keys/{key}` | 市场 Bot | `read` | — | 否 | 否 | 401/403 |
| GET | `/v1/approvals` | BFF、市场 Bot | `read` | Viewer（经 BFF） | 否 | 否 | 401/403 |
| GET | `/v1/approvals/{id}` | 同上 | `read` | 同上 | 否 | 否 | 401/403 |
| GET | `/v1/audit` | BFF、审计服务 | `read` | Viewer（经 BFF） | 否 | 否 | 401/403 |
| POST | `/v1/markets/{m}/orders/preview` | 市场 Bot、BFF | `read` | — | 否 | 否 | 401/403 |
| POST | `/v1/approvals` | 市场 Bot | `write` | —（Bot 发起，actor=bot） | 否 | 是 | 401/403 |
| POST | `/v1/approvals/{id}/decide` | **仅写 BFF** | `write` | Approver | 否* | 是 | 401/403；无 actor 403 |
| POST | `/v1/markets/{m}/risk-snapshots` | 市场 Bot | `write` | — | 否 | 是 | 401/403 |
| POST | `/v1/markets/{m}/orders` | **仅该市场 Runtime** | `write` | —（须已批准） | 否 | 是 | 401/403 |
| POST | `/v1/markets/{m}/orders/{id}/cancel` | 市场 Bot 或写 BFF | `write` | RiskOperator（若经 BFF） | 否* | 是 | 401/403 |
| POST | `/v1/markets/{m}/emergency-stop` | **仅写 BFF** | `write` | RiskOperator | 否* | 是 | 401/403 |
| POST | `/v1/markets/{m}/kill-switch/resume` | **仅写 BFF** | `write` | RiskOperator | 否* | 是 | 401/403 |
| POST | `/v1/markets/{m}/strategies/{id}/pause` | 写 BFF | `write` | StrategyReviewer 或 RiskOperator | 否* | 是 | 401/403 |
| POST | `/v1/markets/{m}/strategies/{id}/resume` | 写 BFF | `write` | StrategyReviewer 或 RiskOperator | 否* | 是 | 401/403 |

`*` CSRF 在 BFF 终止。Gateway 不收浏览器，故自身无 CSRF。

Decide 必须同时具备：BFF `write` Key **和** `actor_principal`。缺一即失败。
请求体 `decided_by` 不作为主体。

## Projection（仅 BFF）

| HTTP | 端点 | 调用方 | 服务 scope | 用户 role | CSRF | 审计 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/healthz` | 探活 | — | — | 否 | 否 | — |
| GET | `/v1/markets/{m}/health\|positions\|accounts\|signals` | Next 投影 BFF | 服务身份（待加） | Viewer | 否 | 否 | 401 |
| GET | `/v1/markets/{m}/orders/{id}` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| GET | `/v1/approvals` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| GET | `/v1/bot-tasks` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| GET | `/v1/incidents` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| GET | `/v1/experiments` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| GET | `/v1/candidates` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| POST | `/v1/chief/query` | Next Chief BFF | 同上 | Viewer | 否 | 否 | 401 |

Projection **无写资金接口**。BFF 不得把 POST `/api/projection/*` 转成写 Gateway。

## Incident Center（仅服务 / 经 BFF）

| HTTP | 端点 | 调用方 | 服务 scope | 用户 role | CSRF | 审计 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | `/healthz` | 探活 | — | — | 否 | 否 | — |
| GET | `/v1/incidents` | BFF、Chief | 服务身份 | Viewer | 否 | 否 | 401 |
| GET | `/v1/incidents/{id}` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| GET | `/v1/incidents/{id}/timeline` | 同上 | 同上 | Viewer | 否 | 否 | 401 |
| POST | `/v1/incidents` | Runtime / Gateway（开事故） | 服务身份 | — | 否 | 是 | 401/403 |
| POST | `/v1/incidents/{id}/mitigate` | 写 BFF | 服务身份 | RiskOperator | 否* | 是 | 401/403 |
| POST | `/v1/incidents/{id}/resolve` | 写 BFF | 服务身份 | RiskOperator | 否* | 是 | 401/403 |

## Strategy Evolution / Risk Auditor（仅服务 / 经 BFF）

| HTTP | 端点 | 调用方 | 服务 scope | 用户 role | CSRF | 审计 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | Evolution `/healthz` | 探活 | — | — | 否 | 否 | — |
| GET | 实验 / 候选（经 Projection） | 浏览器→BFF | — | Viewer | 否 | 否 | 401 |
| POST | 晋级决定（待加 BFF） | 写 BFF | Evolution `write` | StrategyReviewer | 是（BFF） | 是 | 401/403 |
| POST | Risk Auditor `/v1/audit-promotion` | Evolution | 服务身份 | — | 否 | 是 | 401/403 |
| GET | `/v1/conclusions/{candidate_id}` | Evolution / BFF | 服务身份 | Viewer 或 StrategyReviewer | 否 | 否 | 401 |

## 两个量化系统（仅 Gateway）

只读合同（Shadow / 投影拉取，仍经 Gateway 适配器，不经浏览器）：

```text
GET /health
GET /market/snapshots
GET /signals
GET /accounts/{account_id}
GET /positions
GET /orders
GET /orders/{order_id}
GET /fills
```

| HTTP | 端点类 | 调用方 | 服务 scope | 用户 role | CSRF | 审计 | 失败 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| GET | 上列只读 | Gateway 适配器 | 量化系统服务身份 | — | 否 | 否 | 401/502 |
| POST | 下单 / 撤单 / 资金 | **仅 Gateway** | 量化系统写身份 | — | 否 | 是（在 Gateway） | 401/403 |

浏览器、BFF、Bot 都不得直连这些写接口。

## 实现 PR 禁止事项

- 不在本设计合入后偷偷写认证代码于同一 PR。
- 不把 IdentityAdmin 做成超级 Approver。
- 不把 `service_principal` 与 `actor_principal` 合成一列。
- 不把邮箱当 `subject_id`。
- 三项 ADR 未合并前，不写适配层。
- 不改 Runtime Outbox，不打开 `live`。
