# ADR-001：IAP 向 BFF 传递签名 JWT

- Status: **Accepted**
- Milestone: [Identity/IAP](https://github.com/xqw1377-prog/DSH-bot/milestone/3)（保持开启）
- 取代：`docs/security/identity-trust-model.md` 中 ADR-1 的 `PROPOSED/UNDECIDED`
- 不包含认证实现

## Context

浏览器只能访问 IAP 和 Next BFF。需要决定：登录由 IAP 用现有 SSO 做完，还是由 BFF 自己跑完整 OIDC。

DSH 不自建账号、密码、会话。SSO 已有登录、超时、撤销、MFA。

## Decision

**IAP 完成 OIDC 登录，并向 BFF 传递可验证的签名 JWT。**

BFF 不实现授权码 / PKCE / 会话存储。BFF 只验证 IAP 注入的 JWT，并映射成内部 principal。

BFF 必须验证：

- 签名与允许的算法（仅明确白名单，例如 RS256 / ES256；拒绝 `none` 与对称算法）
- `issuer` 等于配置的 SSO/IAP issuer
- `audience` 等于本控制台 audience
- `exp` / `nbf` / `iat`
- 稳定 `subject_id`（`iss` + `sub`，不用邮箱）
- JWKS Key ID（`kid` 必须能解析到已知公钥）
- 最大允许时钟偏差（建议 ≤ 60s）

未知 Key 或 JWKS 暂不可用：

- 允许使用**尚未过期**的缓存 Key 验签
- 无法对应到缓存 Key 的新 Token **失败关闭**
- 不得降级为“只看 Header”

IAP 必须删除外部传入的同名身份 Header，再注入自己签发或转签的 JWT。

## Rejected Alternatives

- **BFF 自行完整 OIDC 登录**：在 DSH 内重造回调、会话 Cookie、刷新与撤销，重复 SSO 已有能力，扩大攻击面。
- **只信任普通身份 Header（无签名）**：外部可伪造；过度依赖网络拓扑。

## Security Invariants

- 浏览器永远不把 IAP Token 发给 Gateway。
- BFF 不把面向 BFF audience 的 IAP Token 转发给 Gateway（见 ADR-002）。
- 邮箱 / 显示名称不是主键。
- 生产未配置 issuer / audience / JWKS 时，写接口失败关闭。

## Failure Behavior

| 场景 | 行为 |
| --- | --- |
| Token 缺失、过期、iss/aud 错误 | 401，无写副作用 |
| 算法不在白名单 | 401 |
| `kid` 未知且不在未过期缓存中 | 401，失败关闭 |
| JWKS 不可达但缓存 Key 仍有效且能验签 | 允许 |
| JWKS 不可达且无法验签 | 401 |
| 时钟偏差超过上限 | 401 |
| Session 已撤销（IAP/SSO 侧） | 401 |
| 外部伪造身份 Header | 被 IAP 剥离；BFF 只见重注入 JWT |

## Key Rotation / Revocation

- JWKS 轮换：同时接受当前与上一把未过期 Key。
- 撤销：以 SSO/IAP 会话撤销为准；BFF 不自建黑名单作为唯一手段。
- 缓存 Key 必须带过期时间，过期后不得继续使用。

## Rollout

1. 本 ADR 与 002/003 合并。
2. 实现 PR-1：BFF 验 IAP JWT，只开放 Viewer 只读。
3. 实现 PR-2：Approver / RiskOperator 写路径。
4. 去掉生产 `DSH_SESSION_USER` 回退。
5. `live` 保持硬禁用。

## Acceptance Tests

- 合法 JWT（iss/aud/exp/kid 正确）→ Viewer 可读投影。
- 缺 Token / 过期 / 错 iss / 错 aud → 401。
- `alg=none` 或 HS256 → 401。
- 未知 `kid` 且无缓存 → 401。
- JWKS 故障 + 有效缓存 Key → 仍可验已签发 Token。
- JWKS 故障 + 新 kid → 401。
- 请求携带伪造 `X-User` / `decided_by` → 不影响 principal。
- 生产未配 OIDC → 写 503 或拒启。
