import { defineConfig } from "@playwright/test";

// The server is the real API with a freshly executed 20-seed benchmark in a
// temporary data directory, so every screenshot shows real stored runs.
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  timeout: 120_000,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:8123", viewport: { width: 1440, height: 1000 } },
  webServer: {
    command: "bash e2e/serve.sh",
    url: "http://localhost:8123/api/scenarios",
    timeout: 180_000,
    reuseExistingServer: false,
  },
});
