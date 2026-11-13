import type { NextConfig } from "next";

// A static export: the app runs entirely in the browser against the API and chat URLs (docs/DECISIONS.md,
// Phase 7), so Amplify Hosting serves plain files and there is no server-side rendering to pay for or secure.
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
