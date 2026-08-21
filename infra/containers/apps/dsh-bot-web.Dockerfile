FROM node:24-slim

WORKDIR /app
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml* ./
COPY apps/dsh-bot-web ./apps/dsh-bot-web
COPY packages/client-sdk ./packages/client-sdk

RUN corepack enable pnpm && pnpm install --frozen-lockfile
RUN pnpm --filter @dsh-bot/client-sdk build
RUN pnpm --filter dsh-bot-web build

WORKDIR /app/apps/dsh-bot-web
EXPOSE 3000

# 非特权用户 + 首页探针
USER node
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["pnpm", "start"]
