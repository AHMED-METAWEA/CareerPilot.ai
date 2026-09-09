/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The API lives on a separate origin (Caddy in front of FastAPI). Server
  // components call it directly; the browser never holds an access token, so
  // there is no client-side base URL to configure.
  env: {
    NEXT_PUBLIC_APP_NAME: "CareerPilot.ai",
  },
};

export default nextConfig;
