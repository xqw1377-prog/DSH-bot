# 事件情报（只读，Shadow）

舆情不能靠万能爬虫。采集优先级：

1. 官方 API
2. RSS / 数据接口
3. 静态网页增量抓取
4. Playwright 仅作最后兜底，**本服务禁用**

行情、成交、X/推特不用野生爬虫。重大政策、公告、项目官网可以做合规增量采集。

```text
权威数据源 → 采集与原文存证 → 事件抽取与去重 → 影响因子与持仓关联 → Shadow 决策与复盘
```

## 边界

- 独立服务 `services/intelligence-ingest`，只能联网读取并写情报库
- 不能持有 Gateway write key，不能读交易密钥
- 事件 `mode=SHADOW`，`can_apply=false`
- 没有 URL、发布时间、原文和哈希的记录，不允许进入影响判断
- X 只用 Filtered Stream + Recent Search；禁止 Playwright 登录抓时间线
- 美股行情：Shadow MVP 用授权延迟/IEX（Alpaca），不上完整 SIP，不爬 Yahoo/Google

## 24 币白名单

复制自 6celue `DEFAULT_SYMBOLS_ORDER`，不导入 6celue 代码。清单与官网/X 在 `services/intelligence-ingest/data/source-registry.yaml`。

比特币没有官方 X，禁止编造账号。`handle_status=probable` 的创始人账号使用前再人工确认。

## 第一版已接通

| 来源 | 方法 | 默认 |
|---|---|---|
| GitHub Release / 项目博客 | RSS Atom | `--derived` 才拉 |
| SEC EDGAR 最近申报 | 官方 Atom | 开 |
| Nasdaq 停牌 | 官方 RSS | 开 |
| 国务院 / 央行 / 证监会 / 发改委 | HTML 增量（列表发现） | 开；无正文不评分 |
| X Filtered Stream | 官方 API | 关，等 `X_BEARER_TOKEN` |
| 巨潮公告 | 授权 API | 关 |
| 美股行情 | Alpaca IEX | 关 |

## 主动闭环

```text
自动唤醒 → 读权威源 → 去重核实并关联持仓 → Shadow 决策
→ 通知（今日关注 / Chief 简报）→ 1h/1d/3d 复盘 → 每日审计
```

三个 Bot 挂两类长期任务：`IntelligenceJob` 与 `AuditJob`。建议停在 SUGGESTION，必须走 重放 → 回测 → Shadow → Paper → 人工批准。不能改线上策略。

```powershell
# 与 15 秒快照导出分开。默认 5 分钟一轮。
python scripts/run_autonomous_layer.py --once
python scripts/run_autonomous_layer.py --every 300
```

写出 `INTELLIGENCE.json`，并写入 Runtime 的 `intelligence_items` / `SHADOW_RECORDED` / 日报。控制台 `/intel` 读 `GET /v1/intelligence`。今日看板 `attention` 是最多五件该看的事。

不要把采集循环并进 15 秒快照导出器。不要启动第二条导出循环。X 没有 Bearer 就保持关闭。

## 决策账本

两条闭环共用 `decision_ledger`：情报/信号 → 决策 → 任务 → 风险快照 → 进出场方案 → 审计。传闻只观察，不加仓属于风险增加必须批准，减仓保护可走 PROTECT 车道。`can_apply` 恒为 false，Live 阻断。
