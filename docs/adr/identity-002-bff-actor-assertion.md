# ADR-002：BFF → Gateway 使用短期 Actor JWT

- Status: **Accepted**
- Milestone: [Identity/IAP](https://github.com/xqw1377-prog/DSH-bot/milestone/3)（保持开启）
- 取代：`docs/security/identity-trust-model.md` 中 ADR-2 的 `PROPOSED/UNDECIDED`
- 不包含认证实现

## Context

Gateway 不对浏览器开放。写操作需要同时知道：

- 哪个**服务**在调用（BFF 还是某个 Bot）
- 哪个人在授权（审批、Kill Switch）

这两种身份不能共用一个字段。当前用 API Key 的 `name` 覆盖 `decided_by` 必须废止。

## Decision

**独立服务身份 + 短期用户 Actor Assertion JWT。**

```text
service_principal = Next BFF   （X-API-Key 或等价服务凭证）
actor_principal   = 当前用户   （BFF 签发的 Actor JWT）
```

Gateway 必须同时验证两者。缺任一，生产写操作失败关闭。

Actor JWT 建议声明：

```text
iss=dsh-bff
aud=quant-gateway
sub=<issuer+subject_id>
roles
auth_time
iat
exp
jti
request_id
```

要求：

- 非对称签名，BFF 与 Gateway 使用**独立轮换**的密钥（不是 IAP 的 JWKS）。
- `exp - iat` 在 **1–5 分钟**。
- **禁止**转发 audience 只面向 BFF 的 IAP Token。
- Gateway **不接受**浏览器直连，也不接受浏览器提交的 actor Header。
- 审计同时写 `service_principal` 与 `actor_principal`。
- 请求体 `decided_by` 丢弃。

受保护网络上的“可信 Header”可作为**未来优化**，不能作为第一版，因为它过度依赖拓扑正确。

## Rejected Alternatives

- **只传可信 Header**：内网配错或 Gateway 被误暴露时即可伪造 actor。
- **把 IAP JWT 原样转给 Gateway**：audience 错误，扩大 IAP Token 使用面，撤销与时钟语义混乱。
- **单字段身份**：无法区分“BFF 代用户写”与“Bot 自己写”。
- **长期 Actor JWT**：失窃窗口过大。

## Security Invariants

- 浏览器永远拿不到 Gateway 服务 Key，也拿不到 BFF 的 Actor 签名私钥。
- Bot 调 Gateway 下单时：`service_principal=该 Bot`，`actor_principal` 为 bot 身份或空（下单不替代人的审批）。
- 人的审批 / Kill Switch：**仅写 BFF** 可带用户 Actor JWT。
- IdentityAdmin 的 Token 即使到达 Gateway，也不因此获得 Approver / RiskOperator。

## Failure Behavior

| 场景 | 行为 |
| --- | --- |
| 有服务 Key、无 Actor JWT 的生产写 | 403/401，无副作用 |
| Actor JWT 有效、服务 Key 无 `write` | 403 |
| Actor `aud` ≠ `quant-gateway` 或 `iss` ≠ `dsh-bff` | 401 |
| Actor 过期 / 未知 kid | 401 |
| 浏览器直打 Gateway | 网络层不可达；若到达则无服务 Key → 401 |
| 伪造 `decided_by` | 忽略；审计主体为 Actor JWT 的 `sub` |
| 转发 IAP Token 当 Actor | 因 iss/aud 失败 |

## Key Rotation / Revocation

- BFF Actor 签名密钥独立轮换；Gateway 同时认当前与上一把未过期公钥。
- `jti` 可用于短窗去重；撤销仍以 IAP session 为准——BFF 在 session 已撤时不得再签发 Actor JWT。
- 私钥只存在 BFF 进程 / 密钥管理，不进前端、不进仓库。

## Rollout

1. 本 ADR 合并后，实现 PR-1 仍只做 IAP 验证 + Viewer 只读（可不签发 Actor JWT）。
2. 实现 PR-2 才签发 Actor JWT，并用于 Approver / RiskOperator 写路径。
3. 迁移并删除 `DSH_SESSION_USER` 生产回退。
4. `live` 保持硬禁用。

## Acceptance Tests

- BFF Key + 合法 Actor JWT + Approver → decide 成功；审计两列都有。
- 仅有 BFF Key、无 Actor → 写失败。
- 仅有 Actor、无 BFF Key → 写失败。
- Actor 过期 / 错 aud / 错 iss → 401。
- IAP Token 被当作 Actor → 401。
- 请求体 `decided_by=forged` → 存储与审计仍是 JWT `sub`。
- Approver 的 Actor 调 emergency-stop → 403。
- 浏览器直连 Gateway 带 Actor Header → 401（无服务身份）。
