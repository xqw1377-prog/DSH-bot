import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ["@dsh-bot/client-sdk"],
  env: {
    PROJECTION_API_URL: process.env.PROJECTION_API_URL || "http://127.0.0.1:8004",
  },
};

export default config;
