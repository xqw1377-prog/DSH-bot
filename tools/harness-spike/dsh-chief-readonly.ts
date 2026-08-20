/**
 * Harness Spike：只读 Market Chief 插件（out-of-tree，--patch 挂载）。
 *
 * 验证目标（docs/harness-spike.md 六项清单）：
 * 1. 插件能在 Harness 插件树中加载（apply 生命周期）
 * 2. Profile 能力映射：本插件只注册只读工具，与 profiles/market-chief
 *    的 primary_tools 一一对应，无任何写工具
 * 3. 工具经 defineTool 注册进受管注册表（ctx.tools）
 * 4. 无凭据：只有 gatewayUrl（只读 REST），不接触交易密钥
 *
 * 红线：本插件不注册任何资金写操作（下单/撤单/审批决定）。
 */
import type { Context } from '@deepseek-ai/cordis'
import Schema from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'dsh-chief-readonly'
export const inject = ['tools']

export interface Config {
  /** Quant Gateway 只读地址；无凭据，禁止填交易密钥 */
  gatewayUrl: string
  timeoutMs: number
}

export const Config: Schema<Config> = Schema.object({
  gatewayUrl: Schema.string().default('http://127.0.0.1:8001'),
  timeoutMs: Schema.number().default(5000),
})

/** 只读工具清单：与 profiles/market-chief 声明对齐（不含任何写操作） */
const READONLY_TOOLS = [
  'chief_query_health',
  'chief_pending_approvals',
  'chief_readonly_audit',
] as const

async function gatewayGet(config: Config, path: string): Promise<unknown> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), config.timeoutMs)
  try {
    const resp = await fetch(`${config.gatewayUrl}${path}`,
                             { signal: controller.signal })
    if (!resp.ok) {
      throw new Error(`gateway ${resp.status}: ${await resp.text()}`)
    }
    return await resp.json()
  } finally {
    clearTimeout(timer)
  }
}

export function apply(ctx: Context, config: Config): void {
  // 生命周期可见性：boot 日志中出现即证明插件被加载
  console.error(`[dsh-chief-readonly] loaded (gateway=${config.gatewayUrl})`)

  ctx.tools.register(defineTool({
    name: 'chief_query_health',
    description: '查询指定市场（A_SHARE/CRYPTO）的量化系统健康状态。只读。',
    parameters: {
      market: {
        type: 'string', required: true,
        description: '市场：A_SHARE 或 CRYPTO',
      },
    },
    output: {
      schema: { type: "object", additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(args) {
      return gatewayGet(config, `/v1/markets/${args.market}/health`) as Promise<Record<string, unknown>>
    },
  }))

  ctx.tools.register(defineTool({
    name: 'chief_pending_approvals',
    description: '列出全市场待人工审批事项。只读。',
    parameters: {},
    output: {
      schema: { type: "object", additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute() {
      return gatewayGet(config, '/v1/approvals?status=REQUESTED') as Promise<Record<string, unknown>>
    },
  }))

  ctx.tools.register(defineTool({
    name: 'chief_readonly_audit',
    description: '审计本插件能力：列出注册的只读工具，并断言注册表中不存在任何写操作工具。',
    parameters: {},
    output: {
      schema: { type: "object", additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute() {
      const schemas = ctx.tools.schemas() as Array<{ name: string }>
      const names = schemas.map((s) => s.name)
      const forbidden = [
        'request_order', 'cancel_order', 'submit_order', 'decide_approval',
        'emergency_stop', 'pause_strategy', 'resume_strategy',
        'chief_request_order', 'chief_decide_approval',
      ]
      const violations = names.filter((n) => forbidden.includes(n))
      return {
        plugin: name,
        readonly_tools: READONLY_TOOLS,
        registry_size: names.length,
        write_tool_violations: violations,
        readonly_guaranteed: violations.length === 0,
      }
    },
  }))
}
