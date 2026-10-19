// 의존성 설치 단계 전용 egress 프록시. 격리 빌드 컨테이너는 internal 네트워크에만 붙고, 밖으로 나가는 유일한 길이 이 프록시다.
// CONNECT 만 허용하고, 허용 호스트의 443 으로만 나가며, 이름이 사설·loopback·link-local 주소로 풀리면 거부한다(host.docker.internal 차단).
const http = require("http");
const net = require("net");
const dns = require("dns").promises;

const ALLOW = new Set((process.env.ALLOW_HOSTS || "registry.npmjs.org").split(",").map((h) => h.trim()).filter(Boolean));
const PORT = 3128;

function isPrivate(ip) {
  const p = ip.split(".").map(Number);
  if (p.length !== 4 || p.some((n) => !Number.isInteger(n))) return true;
  const [a, b] = p;
  return (
    a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168)
  );
}

const server = http.createServer((req, res) => {
  res.writeHead(405, { "content-type": "text/plain" });
  res.end("only CONNECT is allowed\n");
});

server.on("connect", async (req, client, head) => {
  const deny = (code, msg) => {
    client.end(`HTTP/1.1 ${code} ${msg}\r\nConnection: close\r\n\r\n`);
  };
  client.on("error", () => client.destroy());
  const idx = req.url.lastIndexOf(":");
  const host = idx > 0 ? req.url.slice(0, idx) : req.url;
  const port = idx > 0 ? req.url.slice(idx + 1) : "";
  if (!ALLOW.has(host) || port !== "443") return deny(403, "Forbidden");
  let addrs;
  try {
    addrs = await dns.lookup(host, { all: true, family: 4 });
  } catch (e) {
    return deny(502, "Bad Gateway");
  }
  if (!addrs.length || addrs.some((a) => isPrivate(a.address))) return deny(403, "Forbidden");
  const upstream = net.connect(443, addrs[0].address, () => {
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head && head.length) upstream.write(head);
    upstream.pipe(client);
    client.pipe(upstream);
  });
  upstream.setTimeout(60000, () => upstream.destroy());
  upstream.on("error", () => {
    if (!client.writableEnded) deny(502, "Bad Gateway");
    client.destroy();
  });
  client.on("close", () => upstream.destroy());
});

server.listen(PORT, "0.0.0.0", () => console.log("READY " + PORT));
