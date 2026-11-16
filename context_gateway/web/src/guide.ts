// 명령 예시는 환경변수만 참조한다. 실제 키·세션 값은 이 모듈에 들어오지 않는다.
// MCP 경로는 core app 구현(streamable_http_path="/mcp"를 /mcp에 mount)에 맞춰 바뀔 수 있어 상수 한 곳에 둔다. 실서버 확인 전이다.
export const MCP_PATH = "/mcp";

export function connectionGuide(origin: string): { curl: string; mcp: string } {
  const curl = [
    "# 1) 이 화면과 같은 주소",
    `export CONTEXT_GATEWAY_URL="${origin}"`,
    "",
    "# 2) 새 1회용 키를 붙여 넣는다(입력은 화면과 셸 기록에 남지 않는다)",
    "read -rs CONTEXT_GATEWAY_KEY",
    "",
    "# 3) 키를 세션으로 한 번만 교환한다",
    'CONTEXT_GATEWAY_SESSION="$(curl -fsS -X POST "$CONTEXT_GATEWAY_URL/api/v1/auth/exchange" \\',
    '  -H "Authorization: Bearer $CONTEXT_GATEWAY_KEY" | jq -r .access_token)"',
    "unset CONTEXT_GATEWAY_KEY",
    "export CONTEXT_GATEWAY_SESSION",
    "",
    "# 4) 세션으로 조회한다",
    'curl -fsS "$CONTEXT_GATEWAY_URL/api/v1/projects" \\',
    '  -H "Authorization: Bearer $CONTEXT_GATEWAY_SESSION"',
  ].join("\n");
  const mcp = JSON.stringify(
    {
      mcpServers: {
        "context-gateway": {
          type: "http",
          url: `\${CONTEXT_GATEWAY_URL}${MCP_PATH}`,
          headers: { Authorization: "Bearer ${CONTEXT_GATEWAY_SESSION}" },
        },
      },
    },
    null,
    2,
  );
  return { curl, mcp };
}
