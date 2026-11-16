// 실제 Gateway에 연결한 UI 전체 흐름(jsdom). 레이아웃·키보드·실제 다운로드 저장은 이 테스트 범위 밖이며 브라우저 검증이 따로 필요하다.
// 실행: scripts/live/start-gateway.sh && python3 scripts/live/seed.py && GATEWAY_LIVE_URL=http://127.0.0.1:18787 npx vitest run src/live.app.test.tsx
import { readFileSync } from "node:fs";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { GatewayClient } from "./api/client";
import type { DownloadedFile } from "./api/types";
import { App } from "./App";
import { SessionStore } from "./auth/sessionStore";

const base = process.env["GATEWAY_LIVE_URL"];
const keysFile = process.env["GATEWAY_LIVE_KEYS_FILE"] ?? "/tmp/ctx-gw-live/keys.json";

describe.skipIf(!base)("실제 Gateway + UI", () => {
  it("로그인→문서 읽기→이전 스냅샷(미검증 아님)→검색→ZIP 내려받기, 악성 Markdown은 실행되지 않는다", async () => {
    const keys = JSON.parse(readFileSync(keysFile, "utf8")) as Record<string, string>;
    const calls: { url: string; auth: string | null }[] = [];
    const liveFetch: typeof fetch = (input, init) => {
      calls.push({ url: String(input), auth: new Headers(init?.headers).get("authorization") });
      return fetch(`${base}${String(input)}`, init);
    };
    const save = vi.fn<(file: DownloadedFile) => void>();
    const store = new SessionStore({ createClient: (hooks) => new GatewayClient({ fetch: liveFetch, ...hooks }) });
    render(<App store={store} save={save} />);
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("1회용 키"), keys["alpha"]!);
    await user.click(screen.getByRole("button", { name: "키로 접속" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Synthetic Alpha" })).toBeInTheDocument();
    expect(calls.filter((c) => c.url.endsWith("/auth/exchange"))).toHaveLength(1);
    expect(await screen.findByRole("heading", { level: 2, name: "Approved overview" })).toBeInTheDocument();
    expect(await screen.findByText(/두 번째 스냅샷/)).toBeInTheDocument();
    const prov = screen.getByRole("group", { name: "문서 출처" });
    expect(within(prov).getByText("opaque-ref-synthetic-alpha")).toBeInTheDocument();
    expect(within(prov).getByText("rev-2")).toBeInTheDocument();
    expect(within(prov).queryByText("미검증")).toBeNull();
    expect(document.body.innerHTML).not.toContain(keys["alpha"]!);

    const select = screen.getByLabelText("스냅샷") as HTMLSelectElement;
    const oldOption = [...select.options].find((o) => o.textContent?.includes("rev-1"))!;
    await user.selectOptions(select, oldOption.value);
    expect(await screen.findByText(/<script>window\.__pwned/)).toBeInTheDocument(); // 글자로만
    const reader = screen.getByRole("article");
    expect(reader.querySelector("script, img, iframe, [onerror]")).toBeNull();
    expect(reader.querySelector('a[href^="javascript"]')).toBeNull();
    expect(reader.querySelector('a[href="https://example.com/a"]')).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();

    await user.type(screen.getByLabelText("문서 검색"), "알파");
    await user.click(screen.getByRole("button", { name: "검색" }));
    expect(await screen.findByRole("region", { name: "검색 결과" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "검색 지우기" }));
    await user.click(screen.getByRole("button", { name: "ZIP 내려받기" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    expect(save.mock.calls[0]![0].filename).toMatch(/^synthetic-alpha-[0-9a-f-]{36}\.zip$/);
    const exportCall = calls.find((c) => c.url.includes("/export"))!;
    expect(exportCall.auth).toMatch(/^Bearer /);
    expect(exportCall.url).not.toMatch(/[?&](token|key|access_token)=/);

    await user.click(screen.getByRole("button", { name: "로그아웃" }));
    expect(screen.getByLabelText("1회용 키")).toHaveValue("");
    expect(document.body.textContent).not.toContain("두 번째 스냅샷");
  });
});
