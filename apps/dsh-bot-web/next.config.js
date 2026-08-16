/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  env: {
    PROJECTION_API_URL: process.env.PROJECTION_API_URL || "http://127.0.0.1:8004",
  },
};

module.exports = nextConfig;
