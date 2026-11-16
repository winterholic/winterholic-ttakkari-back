import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayClient } from "./api/client";
import { downloadExport, saveBlob, textFile } from "./download";
import { FakeGateway, KEY, PROJECT_A, SESSION_TOKEN } from "./test/fakeGateway";

describe("downloadExport", () => {
  it("Authorization을 붙인 fetch로 받은 Blob을 저장 함수에 넘긴다(URL에 토큰 없음)", async () => {
    const gateway = new FakeGateway();
    const client = new GatewayClient({ fetch: gateway.fetch });
    await client.exchange(KEY);
    const save = vi.fn();
    await downloadExport(client, PROJECT_A, "zip", null, save);
    const call = gateway.callsTo("/export")[0]!;
    expect(call.authorization).toMatch(/^Bearer synthetic-session-token/);
    expect(call.url).not.toContain(SESSION_TOKEN);
    expect(save).toHaveBeenCalledTimes(1);
    const file = save.mock.calls[0]![0] as { blob: Blob; filename: string };
    expect(file.filename).toMatch(/\.zip$/);
    expect(file.blob.type).toBe("application/zip");
    expect(new Uint8Array(await file.blob.arrayBuffer())).toEqual(new Uint8Array([80, 75, 3, 4]));
  });

  it("실패하면 저장하지 않고 오류를 그대로 던진다", async () => {
    const gateway = new FakeGateway();
    const client = new GatewayClient({ fetch: gateway.fetch });
    await client.exchange(KEY);
    gateway.failNext("/export", { status: 429, headers: { "retry-after": "30" } });
    const save = vi.fn();
    await expect(downloadExport(client, PROJECT_A, "json", null, save)).rejects.toMatchObject({ kind: "rate_limited" });
    expect(save).not.toHaveBeenCalled();
  });
});

describe("saveBlob", () => {
  let created: string[];
  beforeEach(() => {
    created = [];
    URL.createObjectURL = vi.fn(() => {
      created.push("blob:synthetic/1");
      return "blob:synthetic/1";
    });
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
  });

  it("blob: URL과 파일 이름으로 앵커를 눌러 저장하고 URL을 해제한다", async () => {
    const clicked: { href: string; download: string }[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.href, download: this.download });
    });
    saveBlob({ blob: new Blob(["x"]), filename: "ctx.md" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(clicked).toEqual([{ href: "blob:synthetic/1", download: "ctx.md" }]);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:synthetic/1");
    expect(document.querySelector("a[download]")).toBeNull();
  });
});

describe("textFile", () => {
  it("메모리 문서를 서버 호출 없이 파일로 만든다", async () => {
    const file = textFile("a.md", "# 내용", "text/markdown;charset=utf-8");
    expect(file.filename).toBe("a.md");
    expect(await file.blob.text()).toBe("# 내용");
  });

  it("파일 이름의 경로 구분자·제어문자는 지운다", () => {
    expect(textFile("../a/b\u0000.md", "x", "text/plain").filename).toBe(".._a_b_.md");
  });
});
