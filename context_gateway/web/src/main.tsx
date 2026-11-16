import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { GatewayClient } from "./api/client";
import { App } from "./App";
import { SessionStore } from "./auth/sessionStore";
import "./styles/tokens.css";
import "./styles/app.css";

// 키·세션은 이 store의 메모리에만 있다. 저장소·URL·콘솔·분석 도구로 내보내는 코드는 없다.
const store = new SessionStore({ createClient: (hooks) => new GatewayClient(hooks) });

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App store={store} />
  </StrictMode>,
);
