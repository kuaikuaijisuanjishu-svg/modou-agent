import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  reporter: [["list"], ["json", {outputFile: "test-results/results.json"}]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: false,
    },
    {
      // The real FastAPI app over a throwaway repo. Every other spec routes
      // /api/v1/* to fixtures, which never exercises the wire — a field the
      // server stopped sending would leave those specs green. This one also
      // serves the production build, so `npm run build` output is under test
      // rather than only the dev bundle.
      command: "npm run build && python e2e/serve_fixture.py",
      url: "http://127.0.0.1:8788/",
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
  projects: [
    {name: "chromium-1080p", use: {...devices["Desktop Chrome"],
      viewport: {width: 1920, height: 1080}}},
  ],
});
