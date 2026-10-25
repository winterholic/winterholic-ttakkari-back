// Isolated page renderer. Runs as a child process of the collector and has no network of its own:
// Chromium is pointed at a dead proxy, and every request it makes is paused here and answered by the parent
// (collector/render.py), which applies the same DNS-pinned SSRF guard as every other collector fetch.
// Protocol: JSON lines. stdin <- {t:"start"}, {t:"res",id,...}; stdout -> {t:"req",id,...}, {t:"done"|"error"}.
import { createRequire } from "node:module";
import path from "node:path";
import readline from "node:readline";

const out = (message) => process.stdout.write(JSON.stringify(message) + "\n");
const pending = new Map();
let start;
const started = new Promise((resolve) => { start = resolve; });
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  if (message.t === "start") start(message);
  else if (message.t === "res" && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
});
process.stdin.on("end", () => process.exit(4));

const fail = (code) => { out({ t: "error", code }); process.exit(3); };
const config = await started;
setTimeout(() => fail("render_wall_timeout"), config.wall_ms);

let chromium;
try {
  chromium = createRequire(path.join(config.runtime, "package.json"))("playwright-core").chromium;
} catch { fail("playwright_core_missing"); }

let nextId = 1;
let inflight = 0;
let lastActivity = Date.now();
const browser = await chromium.launch({
  headless: true, executablePath: config.executable,
  args: ["--proxy-server=socks5://127.0.0.1:9", "--disable-dev-shm-usage", "--no-first-run", "--disable-background-networking",
         "--disable-sync", "--mute-audio", "--disable-extensions"],
}).catch(() => fail("browser_launch_failed"));
try {
  const context = await browser.newContext({ serviceWorkers: "block", acceptDownloads: false, javaScriptEnabled: true });
  await context.addInitScript(() => {
    for (const name of ["WebSocket", "EventSource", "RTCPeerConnection", "WebTransport"]) {
      try { Object.defineProperty(window, name, { value: undefined, configurable: false }); } catch { /* already locked */ }
    }
    navigator.sendBeacon = () => false;
  });
  await context.route("**/*", async (route) => {
    const request = route.request();
    lastActivity = Date.now();
    if (request.isNavigationRequest() && request.url() === config.url && request.frame() === page.mainFrame()) {
      return route.fulfill({ status: 200, contentType: config.content_type, body: Buffer.from(config.body_b64, "base64") });
    }
    const id = nextId++;
    inflight++;
    out({ t: "req", id, url: request.url(), method: request.method(), type: request.resourceType(),
          navigation: request.isNavigationRequest() });
    const answer = await new Promise((resolve) => pending.set(id, resolve));
    inflight--;
    lastActivity = Date.now();
    if (answer.action === "fulfill") {
      return route.fulfill({ status: answer.status, headers: answer.headers, body: Buffer.from(answer.body_b64, "base64") });
    }
    return route.abort("blockedbyclient");
  });
  const page = await context.newPage();
  page.on("dialog", (dialog) => dialog.dismiss().catch(() => {}));
  page.on("popup", (popup) => popup.close().catch(() => {}));
  await page.goto(config.url, { waitUntil: "domcontentloaded", timeout: config.settle_ms });
  const deadline = Date.now() + config.settle_ms;
  while (Date.now() < deadline && (inflight > 0 || Date.now() - lastActivity < config.quiet_ms)) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const html = await page.content();
  out({ t: "done", html: html.slice(0, config.max_html_chars), title: await page.title(), version: browser.version() });
  await browser.close();
  process.exit(0);
} catch (error) {
  fail(String(error?.message || error).includes("Timeout") ? "render_navigation_timeout" : "render_failed");
}
