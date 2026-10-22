// 사용: node render.mjs <workdir>   (workdir 에 spec.json, shot.html, scene.mjs, node_modules 링크가 있어야 한다)
// 소프트웨어(SwiftShader) WebGL 로 프레임을 PNG 로 저장한다. GPU 모델과 Metal 은 쓰지 않는다.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright-core";

const root = path.resolve(process.argv[2]);
const spec = JSON.parse(fs.readFileSync(path.join(root, "spec.json"), "utf8"));
const chrome = process.env.TTAKKARI_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const outDir = path.join(root, "frames");
fs.mkdirSync(outDir, { recursive: true });

const MIME = { ".html": "text/html", ".mjs": "text/javascript", ".js": "text/javascript", ".json": "application/json" };
const server = http.createServer((req, res) => {
  const u = decodeURIComponent(req.url.split("?")[0]);
  const f = path.resolve(path.join(root, u));
  const allowed = f.startsWith(root + path.sep) || f === root;
  if (!allowed || u.includes("..") || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); return res.end("nf"); }
  res.writeHead(200, { "content-type": MIME[path.extname(f)] || "application/octet-stream" });
  fs.createReadStream(f).pipe(res);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const port = server.address().port;

const browser = await chromium.launch({
  executablePath: chrome, headless: true,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist", "--disable-background-timer-throttling"],
});
const logs = [];
const t0 = Date.now();
try {
  const page = await browser.newPage({ viewport: { width: spec.width, height: spec.height } });
  page.on("console", (m) => logs.push("[" + m.type() + "] " + m.text()));
  page.on("pageerror", (e) => logs.push("[pageerror] " + e.message));
  await page.goto("http://127.0.0.1:" + port + "/shot.html");
  await page.waitForFunction(() => window.__ready === true, null, { timeout: 60000 });
  const info = await page.evaluate(() => window.__info());
  const readyMs = Date.now() - t0;
  const frames = spec.render_frames ?? Array.from({ length: spec.frames }, (_, k) => k);
  for (const f of frames) {
    const data = await page.evaluate((i) => window.renderFrame(i), f);
    fs.writeFileSync(path.join(outDir, "f_" + String(f).padStart(5, "0") + ".png"), Buffer.from(data.split(",")[1], "base64"));
  }
  console.log(JSON.stringify({ ok: true, frames: frames.length, ready_ms: readyMs, total_ms: Date.now() - t0, info, console: logs.slice(0, 20) }));
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: String(e && e.message || e), console: logs.slice(0, 20) }));
  process.exitCode = 2;
} finally {
  await browser.close();
  server.close();
}
