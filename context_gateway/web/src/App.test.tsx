import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayClient } from "./api/client";
import type { DownloadedFile } from "./api/types";
import { App } from "./App";
import { SessionStore } from "./auth/sessionStore";
import { B_SECRET, FakeGateway, KEY, KEY_TWO_PROJECTS, PROJECT_A, PROJECT_B, SESSION_TOKEN, SNAP_A_NEW, SNAP_A_OLD } from "./test/fakeGateway";

function mount(gateway = new FakeGateway()) {
  const save = vi.fn<(file: DownloadedFile) => void>();
  const store = new SessionStore({ createClient: (hooks) => new GatewayClient({ fetch: gateway.fetch, ...hooks }) });
  const view = render(<App store={store} save={save} />);
  return { gateway, store, save, view, user: userEvent.setup() };
}

type User = ReturnType<typeof userEvent.setup>;

async function login(user: User, key = KEY) {
  await user.type(screen.getByLabelText("1회용 키"), key);
  await user.click(screen.getByRole("button", { name: "키로 접속" }));
}

async function loggedIn(key = KEY, gateway = new FakeGateway()) {
  const ctx = mount(gateway);
  await login(ctx.user, key);
  await screen.findByRole("heading", { level: 1, name: /프로젝트/ });
  return ctx;
}

const docList = () => within(screen.getByRole("navigation", { name: "문서 목록" }));

let consoleSpies: ReturnType<typeof vi.spyOn>[];
beforeEach(() => {
  consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => undefined));
});
afterEach(() => {
  for (const spy of consoleSpies) {
    for (const call of spy.mock.calls) expect(JSON.stringify(call)).not.toMatch(new RegExp(`${KEY}|${KEY_TWO_PROJECTS}|${SESSION_TOKEN}`));
  }
  vi.restoreAllMocks();
});

describe("로그인과 정상 조회", () => {
  it("처음에는 키 입력만 보이고 API를 호출하지 않는다", () => {
    const { gateway } = mount();
    expect(screen.getByLabelText("1회용 키")).toHaveAttribute("type", "password");
    expect(gateway.calls).toHaveLength(0);
  });

  it("키를 한 번 교환하고 세션으로 프로젝트 목록·문서를 읽는다", async () => {
    const { gateway } = await loggedIn();
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
    expect(gateway.callsTo("/auth/exchange")[0]!.authorization).toBe(`Bearer ${KEY}`);
    const list = gateway.callsTo("/api/v1/projects").filter((c) => c.path === "/api/v1/projects");
    expect(list[0]!.authorization).toMatch(/^Bearer synthetic-session-token-/);
    expect(await screen.findByRole("heading", { level: 2, name: "개요" })).toBeInTheDocument();
    expect(await screen.findByText(/합성 데이터/)).toBeInTheDocument();
    expect(docList().getByRole("button", { name: /개요/ })).toHaveAttribute("aria-current", "true");
    expect(screen.queryByLabelText("1회용 키")).toBeNull();
  });

  it("교환이 끝나면 입력창의 키는 사라지고 DOM 어디에도 키·세션이 없다", async () => {
    await loggedIn();
    expect(document.body.innerHTML).not.toContain(KEY);
    expect(document.body.innerHTML).not.toContain(SESSION_TOKEN);
  });

  it("문서별 출처(source_refs·리비전·생성·검증 시각)를 보여 준다", async () => {
    await loggedIn();
    await screen.findByText(/합성 데이터/);
    const provenance = screen.getByRole("group", { name: "문서 출처" });
    expect(within(provenance).getByText("repo://alpha@abc1234")).toBeInTheDocument();
    expect(within(provenance).getByText("docs/overview.md")).toBeInTheDocument();
    expect(within(provenance).getByText("abc1234")).toBeInTheDocument();
    expect(within(provenance).queryByText("미검증")).toBeNull();
  });

  it("스냅샷을 바꾸면 그 스냅샷의 문서를 읽고 verified_at이 null이면 미검증으로 표시한다", async () => {
    const { user, gateway } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    await user.selectOptions(screen.getByLabelText("스냅샷"), SNAP_A_OLD);
    expect(await screen.findByText(/이전판/)).toBeInTheDocument();
    expect(within(screen.getByRole("group", { name: "문서 출처" })).getByText("미검증")).toBeInTheDocument();
    expect(gateway.callsTo("/documents/overview").at(-1)!.url).toContain(`snapshot_id=${SNAP_A_OLD}`);
  });
});

describe("다른 프로젝트 데이터 비노출", () => {
  it("키에 허용된 프로젝트만 보이고 다른 프로젝트 경로는 호출하지 않는다", async () => {
    const { gateway } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    expect(screen.queryByText(/베타/)).toBeNull();
    expect(document.body.textContent).not.toContain(B_SECRET);
    expect(gateway.calls.filter((c) => c.path.includes(PROJECT_B))).toHaveLength(0);
  });

  it("프로젝트를 바꾸면 이전 프로젝트의 문서·목록이 화면에서 사라진다", async () => {
    const { user } = await loggedIn(KEY_TWO_PROJECTS);
    await screen.findByText(/합성 데이터/);
    await user.selectOptions(screen.getByLabelText("프로젝트"), PROJECT_B);
    expect(await screen.findByText(new RegExp(B_SECRET))).toBeInTheDocument();
    expect(screen.queryByText(/합성 데이터/)).toBeNull();
    expect(docList().queryByRole("button", { name: /API 노트/ })).toBeNull();
  });

  it("서버가 다른 프로젝트의 검색 결과를 섞어 보내면 표시하지 않는다", async () => {
    const gateway = new FakeGateway();
    const { user } = await loggedIn(KEY, gateway);
    await screen.findByText(/합성 데이터/);
    gateway.leakProjectInSearch = true;
    await user.type(screen.getByLabelText("문서 검색"), "알파");
    await user.click(screen.getByRole("button", { name: "검색" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("응답을 해석할 수 없어");
    expect(docList().queryByText(/베타/)).toBeNull();
  });

  it("다른 문서를 고른 직후에는 이전 문서 내용을 새 제목 아래에 남기지 않고 로딩을 보여 준다", async () => {
    const gateway = new FakeGateway();
    const { user } = await loggedIn(KEY, gateway);
    await screen.findByText(/합성 데이터/);
    const release = gateway.hold("/documents/api");
    await user.click(docList().getByRole("button", { name: /API 노트/ }));
    expect(screen.getByRole("heading", { level: 2, name: "API 노트" })).toBeInTheDocument();
    expect(screen.queryByText(/합성 데이터/)).toBeNull();
    expect(screen.getByRole("status", { name: "문서 불러오는 중" })).toBeInTheDocument();
    release();
    expect(await screen.findByText(/GET \/things/)).toBeInTheDocument();
  });

  it("문서 요청이 늦게 끝나도 나중에 고른 문서를 덮어쓰지 않는다", async () => {
    const gateway = new FakeGateway();
    const release = gateway.hold("/documents/overview");
    const { user } = mount(gateway);
    await login(user);
    await screen.findByRole("heading", { level: 1, name: /프로젝트/ });
    await user.click(docList().getByRole("button", { name: /API 노트/ }));
    expect(await screen.findByText(/GET \/things/)).toBeInTheDocument();
    release();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText(/합성 데이터/)).toBeNull();
    expect(screen.getByRole("heading", { level: 2, name: "API 노트" })).toBeInTheDocument();
  });
});

describe("새로고침·로그아웃·만료", () => {
  it("새로고침(앱 재생성) 뒤에는 자격이 복원되지 않고 요청도 없다", async () => {
    const gateway = new FakeGateway();
    const first = await loggedIn(KEY, gateway);
    await screen.findByText(/합성 데이터/);
    first.view.unmount();
    const callsBefore = gateway.calls.length;
    const second = mount(gateway);
    expect(screen.getByLabelText("1회용 키")).toHaveValue("");
    expect(screen.queryByText(/합성 데이터/)).toBeNull();
    expect(second.store.client).toBeNull();
    expect(gateway.calls).toHaveLength(callsBefore);
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).toBe("{}");
    expect(location.href).not.toContain(KEY);
    expect(location.href).not.toContain("synthetic-session");
  });

  it("로그아웃하면 문서와 토큰을 메모리에서 지우고 로그인 화면으로 돌아간다", async () => {
    const { user, store } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    const client = store.client!;
    await user.click(screen.getByRole("button", { name: "로그아웃" }));
    expect(screen.getByLabelText("1회용 키")).toBeInTheDocument();
    expect(screen.queryByText(/합성 데이터/)).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("로그아웃");
    expect(client.hasSession).toBe(false);
  });

  it("세션이 만료된 뒤 요청이 401이면 화면의 문서를 지우고 새 키를 요구한다", async () => {
    const { user, gateway } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    gateway.expireAllSessions();
    await user.click(docList().getByRole("button", { name: /API 노트/ }));
    expect(await screen.findByLabelText("1회용 키")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("만료");
    expect(screen.getByRole("alert")).toHaveTextContent("새 키");
    expect(document.body.textContent).not.toContain("합성 데이터");
    expect(document.body.textContent).not.toContain("GET /things");
  });

  it("세션 만료 시각이 지나면 타이머만으로도 초기화된다", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"], shouldAdvanceTime: true });
    try {
      const { user } = await loggedIn();
      await screen.findByText(/합성 데이터/);
      expect(user).toBeDefined();
      await act(async () => {
        vi.advanceTimersByTime(60 * 60 * 1000 + 1000);
      });
      expect(screen.getByLabelText("1회용 키")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("만료");
      expect(document.body.textContent).not.toContain("합성 데이터");
    } finally {
      vi.useRealTimers();
    }
  });

  it("이미 쓴 키는 거절 안내를 보여 주고 입력을 비운다", async () => {
    const gateway = new FakeGateway();
    gateway.keys.get(KEY)!.used = true;
    const { user } = mount(gateway);
    await login(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("이미 사용");
    expect(screen.getByLabelText("1회용 키")).toHaveValue("");
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });

  it("네트워크 오류면 같은 키로 다시 시도할 수 있게 입력을 남긴다", async () => {
    const gateway = new FakeGateway();
    gateway.failNext("/auth/exchange", "network");
    const { user } = mount(gateway);
    await login(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("연결하지 못했습니다");
    expect(screen.getByLabelText("1회용 키")).toHaveValue(KEY);
    await user.click(screen.getByRole("button", { name: "키로 접속" }));
    expect(await screen.findByRole("heading", { level: 1, name: /프로젝트/ })).toBeInTheDocument();
  });

  it("교환 중에는 버튼이 잠겨 중복 교환이 나가지 않는다", async () => {
    const gateway = new FakeGateway();
    const release = gateway.hold("/auth/exchange");
    const { user } = mount(gateway);
    await user.type(screen.getByLabelText("1회용 키"), KEY);
    await user.click(screen.getByRole("button", { name: "키로 접속" }));
    expect(screen.getByRole("button", { name: /교환 중/ })).toBeDisabled();
    await user.keyboard("{Enter}");
    release();
    await screen.findByRole("heading", { level: 1, name: /프로젝트/ });
    expect(gateway.callsTo("/auth/exchange")).toHaveLength(1);
  });
});

describe("오류·빈·부분 오류 상태", () => {
  it("프로젝트가 하나도 없으면 빈 상태와 다음 행동을 안내한다", async () => {
    const gateway = new FakeGateway();
    gateway.keys.set("synthetic-empty-key", { used: false, projects: [], scopes: ["read"] });
    const { user } = mount(gateway);
    await login(user, "synthetic-empty-key");
    expect(await screen.findByText("볼 수 있는 프로젝트가 없습니다")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "로그아웃" })).toBeInTheDocument();
  });

  it("프로젝트 목록 429는 대기 시간을 알리고 다시 시도하면 불러온다", async () => {
    const gateway = new FakeGateway();
    const { user } = mount(gateway);
    gateway.failNext("/api/v1/projects", { status: 429, headers: { "retry-after": "60" } });
    await login(user);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("요청이 너무 많습니다");
    expect(alert).toHaveTextContent("60초");
    await user.click(within(alert).getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByRole("heading", { level: 1, name: /프로젝트/ })).toBeInTheDocument();
  });

  it("프로젝트 목록 네트워크 오류도 다시 시도할 수 있다", async () => {
    const gateway = new FakeGateway();
    const { user } = mount(gateway);
    gateway.failNext("/api/v1/projects", "network");
    await login(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("서버에 연결하지 못했습니다");
    await user.click(screen.getByRole("button", { name: "다시 시도" }));
    expect(await screen.findByRole("heading", { level: 1, name: /프로젝트/ })).toBeInTheDocument();
  });

  it("스냅샷 목록만 실패해도 프로젝트 정보와 내보내기는 남고 문서 목록 자리에 오류와 재시도가 뜬다", async () => {
    const gateway = new FakeGateway();
    const { user } = mount(gateway);
    gateway.failNext(`/${PROJECT_A}/snapshots`, { status: 503 });
    await login(user);
    await screen.findByRole("heading", { level: 1, name: /프로젝트/ });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("문서 목록");
    expect(screen.getByRole("button", { name: "ZIP 내려받기" })).toBeEnabled();
    await user.click(within(alert).getByRole("button", { name: "다시 시도" }));
    expect(await docList().findByRole("button", { name: /개요/ })).toBeInTheDocument();
  });

  it("문서 하나가 404면 그 자리에만 안내가 뜨고 목록은 그대로다", async () => {
    const gateway = new FakeGateway();
    const { user } = await loggedIn(KEY, gateway);
    await screen.findByText(/합성 데이터/);
    gateway.failNext("/documents/api", { status: 404 });
    await user.click(docList().getByRole("button", { name: /API 노트/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("찾을 수 없");
    expect(docList().getByRole("button", { name: /개요/ })).toBeInTheDocument();
  });

  it("문서를 불러오는 동안 로딩 표시가 보인다", async () => {
    const gateway = new FakeGateway();
    const release = gateway.hold("/documents/overview");
    const { user } = mount(gateway);
    await login(user);
    expect(await screen.findByRole("status", { name: "문서 불러오는 중" })).toBeInTheDocument();
    release();
    expect(await screen.findByText(/합성 데이터/)).toBeInTheDocument();
  });
});

describe("검색·전체 맥락·복사", () => {
  it("검색 결과를 누르면 그 문서가 열린다", async () => {
    const { user } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    await user.type(screen.getByLabelText("문서 검색"), "things");
    await user.click(screen.getByRole("button", { name: "검색" }));
    const results = await screen.findByRole("region", { name: "검색 결과" });
    expect(within(results).getByText(/1건/)).toBeInTheDocument();
    await user.click(within(results).getByRole("button", { name: /API 노트/ }));
    expect(await screen.findByRole("heading", { level: 2, name: "API 노트" })).toBeInTheDocument();
  });

  it("검색 결과가 없으면 그렇게 알리고 검색을 지울 수 있다", async () => {
    const { user } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    await user.type(screen.getByLabelText("문서 검색"), "zzzzzz");
    await user.click(screen.getByRole("button", { name: "검색" }));
    expect(await screen.findByText(/일치하는 문서가 없습니다/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "검색 지우기" }));
    expect(docList().getByRole("button", { name: /개요/ })).toBeInTheDocument();
  });

  it("검색 권한이 없는 키는 404를 권한 안내로 풀어서 보여 준다", async () => {
    const gateway = new FakeGateway();
    gateway.keys.set("synthetic-read-only", { used: false, projects: [PROJECT_A], scopes: ["read"] });
    const { user } = mount(gateway);
    await login(user, "synthetic-read-only");
    await screen.findByText(/합성 데이터/);
    await user.type(screen.getByLabelText("문서 검색"), "알파");
    await user.click(screen.getByRole("button", { name: "검색" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("검색");
    expect(screen.getByRole("alert")).toHaveTextContent("권한");
  });

  it("전체 맥락은 문서마다 출처를 붙여 이어서 보여 주고, 복사하면 전체 마크다운이 복사된다", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const { user } = await loggedIn();
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    await screen.findByText(/합성 데이터/);
    await user.click(docList().getByRole("button", { name: /전체 맥락/ }));
    expect(await screen.findByRole("heading", { level: 2, name: "전체 맥락" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 3, name: "API 노트" })).toBeInTheDocument();
    expect(screen.getAllByRole("group", { name: "문서 출처" })).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "복사" }));
    expect(writeText).toHaveBeenCalledTimes(1);
    const copied = writeText.mock.calls[0]![0] as string;
    expect(copied).toContain("# 알파 프로젝트");
    expect(copied).toContain("## API 노트");
    expect(copied).not.toContain(B_SECRET);
    expect(await screen.findByRole("status")).toHaveTextContent("복사했습니다");
  });

  it("복사가 막히면 실패를 알린다", async () => {
    const { user } = await loggedIn();
    Object.defineProperty(navigator, "clipboard", { value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) }, configurable: true });
    (document as unknown as { execCommand: unknown }).execCommand = vi.fn().mockReturnValue(false);
    await screen.findByText(/합성 데이터/);
    await user.click(screen.getByRole("button", { name: "복사" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("복사하지 못했습니다");
  });
});

describe("Markdown 안전성", () => {
  it("문서 안의 스크립트·이벤트 핸들러·javascript 링크는 실행 요소가 되지 않는다", async () => {
    const gateway = new FakeGateway();
    gateway.snapshots.find((s) => s.id === SNAP_A_NEW)!.docs[0]!.content =
      '# 위험\n\n<script>window.__pwned = 1</script>\n\n<img src=x onerror="window.__pwned=1">\n\n[눌러](javascript:window.__pwned=1)\n\n![트래커](https://tracker.test/p.png)';
    await loggedIn(KEY, gateway);
    await screen.findByRole("heading", { level: 3, name: "위험" });
    const reader = screen.getByRole("article");
    expect(reader.querySelector("script, img, iframe, [onerror]")).toBeNull();
    expect(reader.querySelector('a[href^="javascript"]')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(gateway.calls.some((c) => c.url.includes("tracker.test"))).toBe(false);
  });
});

describe("다운로드", () => {
  it("ZIP 내려받기는 Authorization을 붙인 fetch로 받아 저장한다", async () => {
    const { user, gateway, save } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    await user.click(screen.getByRole("button", { name: "ZIP 내려받기" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    const call = gateway.callsTo("/export")[0]!;
    expect(call.authorization).toMatch(/^Bearer synthetic-session-token-/);
    expect(call.url).toContain("format=zip");
    expect(call.url).toContain(`snapshot_id=${SNAP_A_NEW}`);
    expect(call.url).not.toContain(SESSION_TOKEN);
    expect(save.mock.calls[0]![0].filename).toMatch(/\.zip$/);
    expect(await screen.findByRole("status")).toHaveTextContent("저장했습니다");
  });

  it("Markdown·JSON 내려받기도 각각 format을 달리해 호출한다", async () => {
    const { user, gateway, save } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    await user.click(screen.getByRole("button", { name: "Markdown 내려받기" }));
    await screen.findByText(/저장했습니다/);
    expect(save).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "JSON 내려받기" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
    expect(gateway.callsTo("/export").map((c) => new URL(c.url).searchParams.get("format"))).toEqual(["markdown", "json"]);
  });

  it("내려받기가 429면 저장하지 않고 이유를 알린다", async () => {
    const gateway = new FakeGateway();
    const { user, save } = await loggedIn(KEY, gateway);
    await screen.findByText(/합성 데이터/);
    gateway.failNext("/export", { status: 429, headers: { "retry-after": "10" } });
    await user.click(screen.getByRole("button", { name: "JSON 내려받기" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("요청이 너무 많습니다");
    expect(save).not.toHaveBeenCalled();
  });

  it("현재 문서는 서버 호출 없이 .md로 저장한다", async () => {
    const { user, gateway, save } = await loggedIn();
    await screen.findByText(/합성 데이터/);
    const before = gateway.calls.length;
    await user.click(screen.getByRole("button", { name: "Markdown으로 저장" }));
    expect(gateway.calls).toHaveLength(before);
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0]![0].filename).toBe("overview.md");
  });
});

describe("연결 안내", () => {
  it("로그인 화면의 curl·MCP 안내는 환경변수만 쓰고 입력한 키를 반영하지 않는다", async () => {
    const { user } = mount();
    await user.type(screen.getByLabelText("1회용 키"), KEY);
    await user.click(screen.getByText("curl·MCP로 쓰려면"));
    const text = document.body.textContent ?? "";
    expect(text).toContain("$CONTEXT_GATEWAY_SESSION");
    expect(text).toContain("${CONTEXT_GATEWAY_SESSION}");
    expect(document.querySelector("pre")?.textContent ?? "").not.toContain(KEY);
  });
});
