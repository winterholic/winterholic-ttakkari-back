import { useId, useState, type FormEvent } from "react";
import type { AuthNotice, SessionStore } from "../auth/sessionStore";
import { ConnectGuide } from "./ConnectGuide";
import { noticeText } from "./messages";

const KEEP_INPUT_ON: ReadonlySet<AuthNotice["kind"]> = new Set(["network", "rate_limited"]);

export function LoginScreen({ store, notice, exchanging }: { store: SessionStore; notice: AuthNotice | null; exchanging: boolean }) {
  const inputId = useId();
  const [key, setKey] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (exchanging || key.trim() === "") return;
    await store.signIn(key);
    const next = store.getState();
    // 연결 문제로 실패하면 키가 아직 유효할 수 있어 같은 키로 다시 시도할 수 있게 둔다. 그 밖의 실패에서는 입력을 비운다.
    if (next.status === "signed_out" && next.notice && KEEP_INPUT_ON.has(next.notice.kind)) return;
    setKey("");
  }

  const text = notice ? noticeText(notice) : null;

  return (
    <main className="login">
      <div className="login-card">
        <p className="brand">Context Gateway</p>
        <h1 className="login-title">Context Gateway 접속</h1>
        <p className="login-lead">승인된 프로젝트 맥락을 읽기 전용으로 엽니다. 키는 한 번만 쓸 수 있고 이 탭의 메모리에만 머뭅니다. 새로고침하면 다시 입력해야 합니다.</p>
        {text ? (
          <div className={`notice notice-${text.tone}`} role={text.tone === "info" ? "status" : "alert"}>
            <p className="notice-title">{text.title}</p>
            <p className="notice-hint">{text.hint}</p>
          </div>
        ) : null}
        <form onSubmit={submit} className="login-form" autoComplete="off">
          <label htmlFor={inputId} className="field-label">
            1회용 키
          </label>
          <input
            id={inputId}
            className="input input-mono"
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            data-1p-ignore
            data-lpignore="true"
            disabled={exchanging}
            aria-describedby={`${inputId}-help`}
          />
          <p id={`${inputId}-help`} className="field-help">
            받은 키를 그대로 붙여 넣으세요. 주소창이나 저장소에는 남기지 않습니다.
          </p>
          <button type="submit" className="btn btn-primary" disabled={exchanging || key.trim() === ""}>
            {exchanging ? "교환 중…" : "키로 접속"}
          </button>
        </form>
        <ConnectGuide />
      </div>
    </main>
  );
}
