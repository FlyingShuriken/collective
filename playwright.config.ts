import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3000",
    viewport: { width: 390, height: 844 },
    timezoneId: "Asia/Kuala_Lumpur",
    locale: "en-MY",
    screenshot: "only-on-failure",
    trace: "off", // Login traces would record the disposable account credentials.
  },
  webServer: [
    {
      command: "uv run --project services/api uvicorn collective.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log",
      url: "http://127.0.0.1:8000/v1/ready",
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: "pnpm --dir apps/web dev --hostname 127.0.0.1",
      url: "http://127.0.0.1:3000",
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
});
