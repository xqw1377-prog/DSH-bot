# Identity 信任模型

里程碑：[Identity/IAP](https://github.com/xqw1377-prog/DSH-bot/milestone/3) **保持开启**。
本文是身份**设计基线**，不包含认证实现，**不表示** Identity/IAP 里程碑完成。
实现必须另开 PR，且须先合并文末三项 ADR。

基线文档 `docs/security/security-gap-efadc53.md` 不在本 PR 修改。
本分支不改 Runtime Outbox，不打开 `live`。

## 版本关系

```text
efadc53  Paper/Shadow 安全基线
   ↓
1093872 Runtime Local Transactional Outbox
   ↓
9e3b468 Runtime Outbox 安全差距增量文档
```

Runtime 本地 Outbox 已完成。Gateway / Incident / Evolution Outbox 未完成。
全系统 Outbox 未完成。实盘门禁未解除。

## 产品形状（身份必须服务它）

一个统一前端控制台，三个 Bot，两个相互隔离的量化系统：

| Bot | 职责 |
| --- | --- |
| Market Chief | 只读汇总两个市场；不直接下单 |
| Crypto Bot | 管理币量化系统；发起审批，批准后经 Gateway 执行 |
| A 股 Bot | 管理 A 股量化系统；发起审批，批准后经 Gateway 执行 |

浏览器不能直连量化系统。资金动作只走：

```text
用户批准 → Next BFF → Quant Gateway → 对应量化系统 → ACK/FILL → 对账 → 投影
```

顶部可显示 `PAPER` / `SHADOW` / `LIVE` 标识；实盘门禁完成前不能出现可选择的 `LIVE` 按钮。

## 信任边界

```text
浏览器
  → 仅 IAP / 反向代理
  → 仅 Next BFF
  →（服务侧）Quant Gateway / Projection / Incident / Evolution
  →（仅 Gateway）两个量化系统
```

| 组件 | 浏览器可达 | 信任 |
| --- | --- | --- |
| IAP / 反向代理 | 是 | 现有 SSO 的 OIDC |
| Next BFF | 是（经 IAP） | IAP 注入的不可伪造 principal |
| Quant Gateway | 否 | BFF 服务身份 + 单独的用户 actor |
| Projection / Incident / Evolution | 否 | 各自服务身份；人只经 BFF |
| 两个量化系统 | 否 | 只认 Gateway |

外部传入的身份 Header 必须由 IAP **删除后重新注入**。BFF 不信任客户端自带的 `X-User`、`decided_by`、`actor_id`。

## Principal 最小字段

稳定身份主键是 `issuer` + `subject_id`，**不要**用邮箱或显示名称。

```text
subject_id
issuer
audience
roles
session_id
auth_time
expires_at
authentication_method
assurance_level
```

邮箱、显示名称若出现，只作展示，不作审计主键，不作审批人主键。

## 两种身份必须分开

| 字段 | 是什么 | 不是什么 |
| --- | --- | --- |
| `service_principal` | 调用 Gateway 的服务（BFF、Crypto Runtime、A 股 Runtime、Chief） | 登录用户 |
| `actor_principal` | IAP 验过的人（`issuer` + `subject_id`） | API Key 名称 |

BFF → Gateway **同时**携带：

1. 服务身份（现有 `X-API-Key` 或后续等价凭证）
2. 用户 actor（见 ADR-2，JWT 或可信 Header）

Gateway 审计必须同时记录 `service_principal` 和 `actor_principal`。
两者不能塞进同一个字段。当前用 API Key 的 `name` 覆盖 `decided_by` 的做法，在实现 PR 里必须废止。

写路径：

```text
IAP principal
  → BFF 校验并生成 actor_principal
  → BFF 用自己的 service_principal 调用 Gateway
  → Gateway 校验服务 scope，再接受 actor
  → 审批 / Kill Switch / 审计写入两个 principal
  → 丢弃请求体里的 decided_by
```

失败关闭：

- 服务身份有效但没有用户 actor → 拒绝写（403/401）
- 用户身份有效但 BFF 服务身份无 `write` scope → 拒绝写（403）

## 角色

角色来自 ADR-3 选定的来源，映射到下列最小集。一个人可以有多个角色。

| 角色 | 权限 |
| --- | --- |
| Viewer | 只读账户、任务、订单、事故、总览、Chief 问答 |
| Approver | 批准 / 拒绝**交易**审批 |
| RiskOperator | Kill Switch、事故缓解和恢复 |
| StrategyReviewer | 策略候选审计与晋级审批 |
| IdentityAdmin | 用户与角色配置；**不默认**具备交易审批权 |

Approver 不能做 Kill Switch 或恢复市场。
RiskOperator 不能默认批准交易。
IdentityAdmin 不能默认批准交易。
Market Chief 对人只提供只读 / 解释 / 任务分解；Bot 自身禁止 `direct_order`。
Crypto / A 股 Bot 可经 Gateway `submit_order`，禁止 `decide_approval` 与持有密钥。

## OIDC / IAP

生产必须配置 `issuer`、`audience`、JWKS。IAP 通过 OIDC 连接**现有 SSO**（登录、会话、超时、撤销、MFA）。

未配置身份系统时，生产写接口失败关闭，**不得**退回 `DSH_SESSION_USER`。
`DSH_ENV=development` 才允许本地假用户。

## 必须先形成 ADR 的三件事

状态：**`PROPOSED/UNDECIDED`**。本设计基线只列选项，**不选定**。
实现 PR 开工前，下列三项必须各自成文并合并为 Accepted ADR。

### ADR-1：浏览器如何登录 — `PROPOSED/UNDECIDED`

- A：IAP 向 BFF 传递**已签名 JWT**（BFF 验 JWKS，不自己跑授权码）。
- B：BFF **自己完成 OIDC 登录**（授权码 + PKCE），IAP 只做 TLS / 入口。

### ADR-2：BFF 如何把用户 actor 传给 Gateway — `PROPOSED/UNDECIDED`

- A：短期签名 JWT（`iss=bff`，`sub=用户 subject`，短 TTL，Gateway 验 BFF JWKS）。
- B：仅在受保护内网链路上使用可信 Header（代理保证浏览器到不了 Gateway）。

无论选哪条，服务身份仍是独立凭证，不能用 actor JWT 代替 API Key。

### ADR-3：角色从哪来 — `PROPOSED/UNDECIDED`

- A：SSO Group
- B：IAP Policy
- C：本项目自己的映射表（`subject_id` → roles）

IdentityAdmin 只在 C 或 C 与 A/B 的叠加里改映射；不能因此获得 Approver。

## 设计验收：失败关闭

实现 PR 的测试必须覆盖：

| 场景 | 期望 |
| --- | --- |
| Token 缺失 / 过期 / issuer 或 audience 错误 | 401，无写副作用 |
| 角色不足（如 Viewer 点批准） | 403，无写副作用 |
| 外部伪造身份 Header | IAP 已剥离；BFF 看到的是重注入值或拒绝 |
| JWKS 暂时不可达，或未知签名 Key | 失败关闭，不“降级匿名” |
| BFF 服务身份有效但没有用户 actor | Gateway / BFF 拒绝写 |
| 用户身份有效但 BFF 无 write scope | Gateway 403 |
| Session 撤销后继续请求 | 401 |
| 审批人请求体伪造 `decided_by` | 忽略；审计主体仍是 `actor_principal` |
| 普通 Approver 尝试 Kill Switch 或恢复市场 | 403 |

端点级行列见 `docs/security/endpoint-permission-matrix.md`。
