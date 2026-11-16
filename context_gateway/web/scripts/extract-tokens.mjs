// 따까리 확정 토큰(front 저장소 src/vendor/ttakkari/tokens.css)에서 src/styles/app.css가 실제로 쓰는 토큰만 뽑아 src/styles/tokens.css를 만든다.
// 사용: node scripts/extract-tokens.mjs <따까리 tokens.css 경로>
// 토큰 값을 손으로 고치지 않는다. 원본이 바뀌면 이 스크립트를 다시 돌린다.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = process.argv[2];
if (!sourcePath) throw new Error("usage: node scripts/extract-tokens.mjs <ttakkari tokens.css>");
const source = readFileSync(sourcePath, "utf8");
const css = readFileSync(join(root, "src/styles/app.css"), "utf8");

const used = new Set([...css.matchAll(/var\((--tk-[a-z0-9-]+)/g)].map((m) => m[1]));
const declarations = (block) => new Map([...block.matchAll(/^\s*(--tk-[a-z0-9-]+):\s*(.+?);\s*$/gm)].map((m) => [m[1], m[2]]));

const lightEnd = source.indexOf("/* 다크: 시스템 설정을 따르되");
const light = declarations(source.slice(0, lightEnd));
const darkStart = source.indexOf(":root:not([data-theme=\"light\"]):not(.light) {", lightEnd);
const darkEnd = source.indexOf("\n  }\n}", darkStart);
const dark = declarations(source.slice(darkStart, darkEnd));

// 토큰이 다른 토큰을 참조하면 그 토큰도 함께 가져온다.
const closure = new Set(used);
for (let changed = true; changed; ) {
  changed = false;
  for (const name of [...closure]) {
    for (const value of [light.get(name), dark.get(name)]) {
      for (const ref of value?.matchAll(/var\((--tk-[a-z0-9-]+)/g) ?? []) if (!closure.has(ref[1])) (closure.add(ref[1]), (changed = true));
    }
  }
}
const missing = [...used].filter((name) => !light.has(name));
if (missing.length) throw new Error(`원본에 없는 토큰: ${missing.join(", ")}`);

const pick = (map) => [...closure].filter((name) => map.has(name)).sort().map((name) => `  ${name}: ${map.get(name)};`).join("\n");
const sourceRef = process.argv[3] ?? "front-context-gateway-20261116 @ ttakkari 1.3.3 (f9d78eb)";
writeFileSync(
  join(root, "src/styles/tokens.css"),
  `/* 따까리 디자인 토큰(${sourceRef})의 부분집합. scripts/extract-tokens.mjs가 생성한다. 손으로 고치지 않는다.
   외부 폰트·CDN은 쓰지 않는다. Pretendard/JetBrains Mono는 사용자 기기에 설치돼 있을 때만 쓰이고 없으면 시스템 글꼴이다. */
:root {
  color-scheme: light dark;
${pick(light)}
}

@media (prefers-color-scheme: dark) {
  :root {
${pick(dark).replace(/^/gm, "  ")}
  }
}
`,
);
console.log(`light ${[...closure].filter((n) => light.has(n)).length} / dark overrides ${[...closure].filter((n) => dark.has(n)).length}`);
