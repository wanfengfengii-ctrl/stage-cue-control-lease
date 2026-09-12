import { defineConfig } from "@playwright/test";

// Against the compose stack: BASE_URL=http://web (set by the verify service).
// Locally: start the API on :8000 and `npm run build && npm run preview`,
// then run with BASE_URL=http://localhost:8080.
const baseURL = process.env.BASE_URL ?? "http://localhost:4173";

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : "list",
  use: {
    baseURL,
    actionTimeout: 10_000,
  },
  webServer: process.env.BASE_URL
    ? undefined
    : {
        command: "npm run build && npm run preview -- --port 4173",
        port: 4173,
        timeout: 120_000,
        reuseExistingServer: true,
      },
});
