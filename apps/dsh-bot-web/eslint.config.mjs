import coreWebVitals from "eslint-config-next/core-web-vitals";
import tsConfig from "eslint-config-next/typescript";

/** 前端真实 lint：Next 核心规则 + TS 规则 + 生产代码禁 console/debugger。
 * （tsc --noEmit 只是类型检查，不算 lint。） */
const config = [
  ...coreWebVitals,
  ...tsConfig,
  {
    rules: {
      "no-console": ["error", { allow: ["warn", "error"] }],
      "no-debugger": "error",
    },
  },
  {
    ignores: [".next/**", "node_modules/**", "next-env.d.ts"],
  },
];

export default config;
