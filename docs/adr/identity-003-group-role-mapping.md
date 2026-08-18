# ADR-003：SSO Group 映射项目角色

- Status: **Accepted**
- Milestone: [Identity/IAP](https://github.com/xqw1377-prog/DSH-bot/milestone/3)（保持开启）
- 取代：`docs/security/identity-trust-model.md` 中 ADR-3 的 `PROPOSED/UNDECIDED`
- 不包含认证实现

## Context

IAP 决定“谁能打开控制台”。交易批准、Kill Switch、策略晋级需要更细的角色。
不能让任意 SSO Group 名称自动变成系统角色，也不能把全部细权塞进 IAP Policy。

## Decision

**SSO Group 是身份属性来源；项目维护显式、版本化的 Group → Role 映射。**

IAP Policy 只负责：**能否进入应用**。
细粒度交易权限只认映射后的项目角色。

建议初始映射（名称可按现有 SSO 调整，但必须写在版本库里）：

```text
dsh-viewers            → Viewer
dsh-approvers          → Approver
dsh-risk-operators     → RiskOperator
dsh-strategy-reviewers → StrategyReviewer
dsh-identity-admins    → IdentityAdmin
```

未知 Group **忽略**，不自动升格。
一个人可有多个 Group，角色取并集。
`IdentityAdmin` **默认没有** Approver 与 RiskOperator，因此默认不能审批交易、不能 Kill Switch / 恢复市场。

映射变化必须：

- 走代码或配置审查（本仓库 PR）
- 产生审计记录
- 支持撤销（从映射或 SSO Group 去掉）
- 在 Session / Token **刷新后**生效（不要求热改已签发的 1–5 分钟 Actor JWT）
- 审查记录写明变更人（`actor_principal`）

## Rejected Alternatives

- **IAP Policy 承担全部交易权限**：Policy 变更往往不在本仓库审查，难以对审批/停机做同样的 diff 与审计。
- **SSO Group 名直接当系统角色**：拼写或新建 Group 即可获得 `Approver`。
- **仅项目内 `subject_id` 表、不读 Group**：无法复用现有 SSO，IdentityAdmin 会变成影子账号系统。
- **IdentityAdmin 默认超级用户**：违反职责分离。

## Security Invariants

- 角色枚举闭合：Viewer / Approver / RiskOperator / StrategyReviewer / IdentityAdmin。
- 映射文件或配置是唯一“Group → Role”来源。
- 未映射 Group 不影响角色。
- 无 Viewer 以外角色的用户：只读。
- 实现不得把 IdentityAdmin 写进 decide / emergency-stop 的允许列表。

## Failure Behavior

| 场景 | 行为 |
| --- | --- |
| Token 无任何已映射 Group | 仅当 IAP 已放行时视为无项目角色 → 只读失败则 403 |
| Viewer 调 decide | 403 |
| Approver 调 emergency-stop / resume | 403 |
| IdentityAdmin 调 decide 或 Kill Switch | 403 |
| 映射未审查就在生产手工改 | 禁止；生产只读部署产物 |
| 撤销 Group 后旧 Actor JWT 仍在 TTL 内 | 最多 5 分钟；刷新后失效 |

## Key Rotation / Revocation

- 角色撤销：SSO 移出 Group，或映射 PR 删除该 Group。
- 不在 DSH 内做独立密码重置。
- 映射文件变更本身记审计（谁合入、何时生效）。

## Rollout

1. 本 ADR 合并，映射以仓库文件落地（实现 PR 再加机器可读格式）。
2. 实现 PR-1：只认 Viewer（有任一已映射 Group 或默认进入者 + 显式 Viewer Group）。
3. 实现 PR-2：打开 Approver / RiskOperator 写权限。
4. StrategyReviewer / IdentityAdmin 端点按矩阵后补。
5. 删除 `DSH_SESSION_USER` 生产回退。
6. `live` 保持硬禁用。

## Acceptance Tests

- `dsh-viewers` → 可读，decide 403。
- `dsh-approvers` → decide 允许；emergency-stop 403。
- `dsh-risk-operators` → emergency-stop 允许；decide 403。
- `dsh-identity-admins` → 映射配置允许；decide 与 Kill Switch 403。
- 未列出的 Group `dsh-approvers-tmp` → 不授予 Approver。
- 映射文件无对应项 → 不授角色。
- Group 撤销且 Token 刷新后 → 原写权限 403。
