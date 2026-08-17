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

CMD ["pnpm", "start"]
