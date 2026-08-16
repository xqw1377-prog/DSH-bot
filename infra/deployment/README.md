# 部署说明

- 本地开发：见根目录 README，使用 `uvicorn` 和 `pnpm dev`
- Paper/Shadow 环境：通过 `infra/containers/docker-compose.yml` 启动
- 生产：建议每个市场使用独立 Namespace/容器；Quant Gateway 与交易密钥严格隔离

## 升级原则

- DSH 和 Quant Gateway 独立版本化
- 任何升级必须经过：开发 -> Paper -> Shadow -> Canary -> 生产
- 支持一键回滚与策略停机
