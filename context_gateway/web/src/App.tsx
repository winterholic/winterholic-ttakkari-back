import { useEffect, useSyncExternalStore } from "react";
import type { DownloadedFile } from "./api/types";
import type { SessionStore } from "./auth/sessionStore";
import { saveBlob } from "./download";
import { LoginScreen } from "./ui/LoginScreen";
import { Viewer } from "./ui/Viewer";

export function App({ store, save = saveBlob }: { store: SessionStore; save?: (file: DownloadedFile) => void }) {
  const state = useSyncExternalStore(store.subscribe, store.getState);

  // 절전·백그라운드에서 타이머가 늦게 돌아도 화면이 다시 보일 때 만료를 확인한다.
  useEffect(() => {
    const check = () => store.checkExpiry();
    document.addEventListener("visibilitychange", check);
    window.addEventListener("focus", check);
    return () => {
      document.removeEventListener("visibilitychange", check);
      window.removeEventListener("focus", check);
    };
  }, [store]);

  const client = store.client;
  if (state.status === "active" && client) {
    // 로그아웃하면 이 트리가 LoginScreen으로 교체되어 상태가 사라진다. sessionId key는 교체 없이 세션만 바뀌는 경우를 대비한 방어선이다.
    return <Viewer key={state.sessionId} client={client} expiresAt={state.expiresAt} onSignOut={() => store.signOut()} save={save} />;
  }
  return <LoginScreen store={store} notice={state.status === "signed_out" ? state.notice : null} exchanging={state.status === "exchanging"} />;
}
