import { isGatewayError } from "../api/errors";
import type { AuthNotice } from "../auth/sessionStore";

export type Where = "projects" | "project" | "snapshots" | "document" | "context" | "search" | "export";

export interface Problem {
  /** 어느 부분이 실패했는지. 부분 오류일 때 나머지 화면과 구분해 읽히게 한다. */
  scope: string;
  title: string;
  hint: string;
  retry: boolean;
}

const SUBJECT: Record<Where, string> = {
  projects: "프로젝트 목록",
  project: "프로젝트 정보",
  snapshots: "문서 목록",
  document: "문서",
  context: "전체 맥락",
  search: "검색 결과",
  export: "내려받기 파일",
};

/** 오류를 사용자가 다음에 무엇을 할지 알 수 있는 문장으로 바꾼다. 서버가 준 detail 문자열은 쓰지 않는다. */
export function problemFor(error: unknown, where: Where): Problem {
  return { scope: SUBJECT[where], ...describe(error, where) };
}

function describe(error: unknown, where: Where): Omit<Problem, "scope"> {
  const subject = SUBJECT[where];
  if (!isGatewayError(error)) return { title: `${subject}을(를) 표시하지 못했습니다`, hint: "화면 처리 중 문제가 생겼습니다. 다시 시도해 보세요.", retry: true };
  switch (error.kind) {
    case "unauthorized":
      return { title: "세션이 만료됐습니다", hint: "1회용 키는 다시 쓸 수 없습니다. 새 키로 다시 접속하세요.", retry: false };
    case "forbidden":
      return { title: `${subject}에 접근할 권한이 없습니다`, hint: "이 키에 허용되지 않은 항목입니다. 필요하면 권한이 있는 새 키를 요청하세요.", retry: false };
    case "not_found":
      if (where === "search" || where === "export") {
        const action = where === "search" ? "검색" : "내려받기";
        return { title: `이 키로는 ${action}할 수 없습니다`, hint: `키에 ${action} 권한이 없거나 스냅샷이 폐기됐을 수 있습니다. 서버는 이유를 구분해 알려 주지 않습니다.`, retry: false };
      }
      return { title: `${subject}을(를) 찾을 수 없습니다`, hint: "스냅샷이 폐기됐거나 이 키로 볼 수 없는 항목입니다. 서버는 둘을 구분해 알려 주지 않습니다.", retry: false };
    case "rate_limited":
      return { title: "요청이 너무 많습니다", hint: error.retryAfterSeconds === null ? "잠시 뒤에 다시 시도하세요." : `${error.retryAfterSeconds}초 뒤에 다시 시도하세요.`, retry: true };
    case "network":
      return { title: "서버에 연결하지 못했습니다", hint: "네트워크 연결을 확인한 뒤 다시 시도하세요.", retry: true };
    case "server":
      return { title: "서버에서 오류가 발생했습니다", hint: "잠시 뒤 다시 시도하세요. 계속되면 스냅샷 무결성 검사에 실패한 것일 수 있어 게시자에게 알려야 합니다.", retry: true };
    case "bad_request":
      return { title: "요청이 올바르지 않습니다", hint: "검색어나 선택을 바꿔서 다시 시도하세요.", retry: false };
    case "contract":
      return { title: "응답을 해석할 수 없어 표시하지 않았습니다", hint: "서버 응답이 예상과 달랐거나 다른 프로젝트의 데이터가 섞여 있었습니다. 화면에는 올리지 않았습니다.", retry: true };
    default:
      return { title: `${subject}을(를) 표시하지 못했습니다`, hint: "화면 처리 중 문제가 생겼습니다. 다시 시도해 보세요.", retry: true };
  }
}

export interface NoticeText {
  tone: "info" | "danger" | "warning";
  title: string;
  hint: string;
}

export function noticeText(notice: AuthNotice): NoticeText {
  switch (notice.kind) {
    case "expired":
      return { tone: "warning", title: "세션이 만료됐습니다", hint: "화면의 문서와 토큰을 메모리에서 지웠습니다. 1회용 키는 다시 쓸 수 없으니 새 키가 필요합니다." };
    case "rejected":
      return { tone: "danger", title: "키가 받아들여지지 않았습니다", hint: "이미 사용했거나, 발급 1시간이 지났거나, 폐기된 키이거나, 잘못 입력한 키일 수 있습니다. 서버는 이유를 구분해 알려 주지 않으며, 같은 키는 다시 쓸 수 없습니다." };
    case "network":
      return { tone: "danger", title: "서버에 연결하지 못했습니다", hint: "키가 사용됐는지 알 수 없습니다. 같은 키로 한 번 더 시도하고, 계속 받아들여지지 않으면 새 키를 받으세요." };
    case "rate_limited":
      return { tone: "danger", title: "시도가 너무 많습니다", hint: notice.retryAfterSeconds === undefined ? "잠시 뒤에 다시 시도하세요." : `${notice.retryAfterSeconds}초 뒤에 다시 시도하세요.` };
    case "invalid_input":
      return { tone: "danger", title: "키 형식이 올바르지 않습니다", hint: "공백이나 줄바꿈 없이 키만 붙여 넣으세요." };
    case "server":
      return { tone: "danger", title: "서버 응답을 처리하지 못했습니다", hint: "잠시 뒤 다시 시도하세요. 계속되면 새 키를 받아야 할 수 있습니다." };
    case "signed_out":
      return { tone: "info", title: "로그아웃했습니다", hint: "화면의 문서와 토큰을 메모리에서 지웠습니다. 다시 보려면 새 1회용 키가 필요합니다." };
  }
}
