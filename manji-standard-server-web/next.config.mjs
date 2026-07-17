/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: "standalone", // Dockerfile(runner ステージ)が .next/standalone を使う
};

export default nextConfig;
