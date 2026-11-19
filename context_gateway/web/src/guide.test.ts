import { describe, expect, it } from "vitest";
import { KEY, SESSION_TOKEN } from "./test/fakeGateway";
import { connectionGuide, MCP_PATH } from "./guide";

describe("connectionGuide", () => {
  const guide = connectionGuide("https://gateway.example.test");

  it("키와 세션은 환경변수로만 참조한다", () => {
    expect(guide.curl).toContain('"$CONTEXT_GATEWAY_URL/api/v1/auth/exchange"');
    expect(guide.curl).toContain("Authorization: Bearer $CONTEXT_GATEWAY_KEY");
    expect(guide.curl).toContain("Authorization: Bearer $CONTEXT_GATEWAY_SESSION");
    expect(guide.mcp).toContain("${CONTEXT_GATEWAY_SESSION}");
  });

  it("키를 명령행 인자로 받거나 기록에 남기는 형태를 쓰지 않는다", () => {
    expect(guide.curl).toContain("read -rs CONTEXT_GATEWAY_KEY");
    expect(guide.curl).not.toMatch(/export CONTEXT_GATEWAY_(KEY|SESSION)=/);
    expect(guide.curl).not.toMatch(/\?(key|token|access_token)=/i);
  });

  it("실제 키·세션 값이 들어갈 자리가 없다", () => {
    for (const text of [guide.curl, guide.mcp]) {
      expect(text).not.toContain(KEY);
      expect(text).not.toContain(SESSION_TOKEN);
    }
  });

  it("reusable_api_v2는 raw Bearer로 REST·MCP에 연결하고 share 링크를 자격증명으로 오해하지 않게 한다", () => {
    expect(guide.reusableCurl).toContain("Authorization: Bearer $CONTEXT_GATEWAY_KEY");
    expect(guide.reusableMcp).toContain("Bearer ${CONTEXT_GATEWAY_KEY}");
    expect(guide.reusableCurl).toContain("unset CONTEXT_GATEWAY_KEY");
  });

  it("이 화면의 origin만 URL로 보여 준다", () => {
    expect(guide.curl).toContain('export CONTEXT_GATEWAY_URL="https://gateway.example.test"');
    expect(guide.mcp).toContain(MCP_PATH);
  });
});
