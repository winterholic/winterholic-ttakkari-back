import { useEffect, useMemo, useRef, useState } from "react";
import { asRecord, toContext } from "../api/adapters";
import { GatewayError } from "../api/errors";
import type { ContextBundle, DownloadedFile, GatewayDocument } from "../api/types";
import { saveBlob } from "../download";
import { SafeMarkdown } from "../markdown/SafeMarkdown";
import { ProvenanceBlock } from "../ui/Provenance";

const SHARE_PATHS = new Set([
  "/api/v1/share/exchange",
  "/api/v1/share/context",
  "/api/v1/share/export?format=markdown",
  "/api/v1/share/logout",
]);

export function consumeShareFragment(): string | null {
  if (window.location.pathname !== "/s") return null;
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const token = fragment.get("token");
  if (window.location.hash) window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}`);
  return token && /^[\x21-\x7e]{1,512}$/.test(token) ? token : null;
}

type Fetcher = typeof fetch;
type ShareLink = { label: string; expiresAt: string; snapshotPolicy: string; scopes: string[] };
type ShareBundle = ContextBundle & { snapshot: { id: string; publishedAt: string | null } & ContextBundle["provenance"]; link: ShareLink; sessionExpiresAt: string };

export interface ShareReaderProps {
  initialToken: string | null;
  fetcher?: Fetcher;
  save?: (file: DownloadedFile) => void;
}

function safeMessage(error: unknown): { title: string; hint: string } {
  if (error instanceof GatewayError && error.kind === "unauthorized") return { title: "공유 링크가 만료되었거나 폐기되었습니다", hint: "원래 공유 링크를 다시 여세요. 링크 소유자가 폐기했거나 공유가 만료된 경우 소유자에게 새 링크를 요청하세요." };
  if (error instanceof GatewayError && error.kind === "forbidden") return { title: "이 공유 링크에는 내려받기 권한이 없습니다", hint: "전체 맥락 복사는 계속 사용할 수 있습니다." };
  if (error instanceof GatewayError && error.kind === "not_found") return { title: "공유 맥락을 찾을 수 없습니다", hint: "링크가 폐기되었거나 현재 열람할 수 없습니다. 원래 공유 링크를 다시 열거나 소유자에게 문의하세요." };
  if (error instanceof GatewayError && error.kind === "network") return { title: "공유 서버에 연결하지 못했습니다", hint: "연결을 확인한 뒤 다시 시도하세요. 서버가 중단되었다가 재개된 경우에도 다시 시도할 수 있습니다." };
  return { title: "공유 맥락을 표시하지 못했습니다", hint: "잠시 뒤 다시 시도하세요. 계속되면 링크 소유자에게 문의하세요." };
}

function parseBundle(value: unknown): ShareBundle {
  const rec = asRecord(value, "share.context");
  const project = asRecord(rec["project"], "share.project");
  const snapshotRaw = asRecord(rec["snapshot"], "share.snapshot");
  const id = typeof project["id"] === "string" ? project["id"] : "";
  const raw = toContext({ ...rec, snapshot: snapshotRaw }, id, null);
  if (raw.snapshotId !== snapshotRaw["id"]) throw new GatewayError("contract", "공유 응답을 표시할 수 없습니다");
  const linkRaw = asRecord(rec["link"], "share.link");
  if (!Array.isArray(linkRaw["scopes"]) || typeof linkRaw["expires_at"] !== "string" || typeof rec["session_expires_at"] !== "string") throw new GatewayError("contract", "공유 응답을 표시할 수 없습니다");
  return {
    ...raw,
    snapshot: {
      id: raw.snapshotId,
      publishedAt: typeof snapshotRaw["published_at"] === "string" ? snapshotRaw["published_at"] : null,
      sourceRefs: raw.provenance.sourceRefs,
      sourceRevision: raw.provenance.sourceRevision,
      generatedAt: raw.provenance.generatedAt,
      verifiedAt: raw.provenance.verifiedAt,
    },
    link: { label: typeof linkRaw["label"] === "string" ? linkRaw["label"] : "공유 링크", expiresAt: linkRaw["expires_at"], snapshotPolicy: typeof linkRaw["snapshot_policy"] === "string" ? linkRaw["snapshot_policy"] : "pinned", scopes: linkRaw["scopes"].filter((v): v is string => typeof v === "string") },
    sessionExpiresAt: rec["session_expires_at"],
  };
}

async function shareFetch(fetcher: Fetcher, path: string, init: RequestInit = {}): Promise<Response> {
  if (!SHARE_PATHS.has(path)) throw new GatewayError("misuse", "허용되지 않은 공유 API 경로");
  let response: Response;
  try {
    response = await fetcher(path, { ...init, credentials: "same-origin", cache: "no-store", referrerPolicy: "no-referrer", redirect: "error", headers: { Accept: "application/json", ...(init.headers ?? {}) } });
  } catch {
    throw new GatewayError("network", "서버에 연결하지 못했습니다");
  }
  if (!response.ok) {
    const status = response.status;
    throw new GatewayError(status === 401 ? "unauthorized" : status === 403 ? "forbidden" : status === 404 ? "not_found" : status >= 500 ? "server" : "bad_request", "공유 요청을 처리하지 못했습니다", { status });
  }
  return response;
}

async function restoreSessionIntent(fetcher: Fetcher, getIntent: () => { token: string | null; stopped: boolean }): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const intent = getIntent();
    if (intent.stopped) await shareFetch(fetcher, "/api/v1/share/logout", { method: "POST" });
    else if (intent.token) await shareFetch(fetcher, "/api/v1/share/exchange", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: intent.token }) });
    const current = getIntent();
    if (current.token === intent.token && current.stopped === intent.stopped) return;
  }
  throw new GatewayError("network", "공유 세션 상태를 확인하지 못했습니다");
}

export function ShareReader({ initialToken, fetcher = fetch, save = saveBlob }: ShareReaderProps) {
  const [activeToken, setActiveToken] = useState(initialToken);
  const [bundle, setBundle] = useState<ShareBundle | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [problem, setProblem] = useState<{ title: string; hint: string } | null>(null);
  const [busy, setBusy] = useState(true);
  const [copied, setCopied] = useState(false);
  const [retry, setRetry] = useState(0);
  const [visit, setVisit] = useState(0);
  const [stopped, setStopped] = useState(false);
  const [logoutStatus, setLogoutStatus] = useState<"idle" | "pending" | "failed" | "succeeded">("idle");
  const requestEpoch = useRef(0);
  const desiredToken = useRef(initialToken);
  const stoppedRef = useRef(false);
  const logoutBusy = useRef(false);

  async function recoverCurrentIntent(): Promise<void> {
    const epoch = requestEpoch.current;
    const getIntent = () => ({ token: desiredToken.current, stopped: stoppedRef.current });
    setBundle(null);
    setSelected(null);
    setBusy(true);
    setProblem(null);
    try {
      await restoreSessionIntent(fetcher, getIntent);
      const intent = getIntent();
      if (requestEpoch.current !== epoch) return;
      if (intent.stopped) {
        setLogoutStatus("succeeded");
        setProblem({ title: "서버 공유 세션을 종료했습니다", hint: "다시 보려면 원래 공유 링크를 여세요." });
      } else {
        const response = await shareFetch(fetcher, "/api/v1/share/context");
        const parsed = parseBundle(await response.json());
        if (requestEpoch.current === epoch && !stoppedRef.current && desiredToken.current === intent.token) {
          setBundle(parsed);
          setSelected(parsed.documents[0]?.id ?? null);
        }
      }
    } catch (error) {
      if (requestEpoch.current === epoch) {
        if (stoppedRef.current) {
          setLogoutStatus("failed");
          setProblem({ title: "이 브라우저 화면을 비웠지만 서버 세션은 종료되지 않았습니다", hint: "서버 쿠키가 남아 있을 수 있습니다. 서버 로그아웃을 다시 시도하세요." });
        } else {
          setProblem(safeMessage(error));
        }
      }
    } finally {
      if (requestEpoch.current === epoch) setBusy(false);
    }
  }

  useEffect(() => {
    const onHashChange = () => {
      if (window.location.pathname === "/s" && window.location.hash) {
        const token = consumeShareFragment();
        desiredToken.current = token;
        stoppedRef.current = false;
        requestEpoch.current += 1;
        setStopped(false);
        setLogoutStatus("idle");
        setActiveToken(token);
        setBundle(null);
        setSelected(null);
        setProblem(null);
        setBusy(true);
        setVisit((n) => n + 1);
      }
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (stopped) return;
    const epoch = ++requestEpoch.current;
    const isCurrent = () => !cancelled && requestEpoch.current === epoch;
    const load = async () => {
      setBusy(true);
      setProblem(null);
      try {
        if (activeToken) {
          if (!/^[\x21-\x7e]{1,512}$/.test(activeToken)) throw new GatewayError("bad_request", "공유 링크 형식이 올바르지 않습니다");
          await shareFetch(fetcher, "/api/v1/share/exchange", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: activeToken }) });
        }
        if (!isCurrent()) { void recoverCurrentIntent(); return; }
        const response = await shareFetch(fetcher, "/api/v1/share/context");
        const parsed = parseBundle(await response.json());
        if (isCurrent()) {
          setBundle(parsed);
          setSelected(parsed.documents[0]?.id ?? null);
        }
      } catch (error) {
        if (isCurrent()) { setBundle(null); setProblem(safeMessage(error)); }
      } finally {
        if (isCurrent()) setBusy(false);
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [fetcher, activeToken, retry, stopped, visit]);

  const selectedDoc = useMemo(() => bundle?.documents.find((doc) => doc.id === selected) ?? null, [bundle, selected]);
  const allMarkdown = bundle ? [
    `# ${bundle.project.name}`,
    bundle.project.description,
    `승인 스냅샷: ${bundle.snapshot.id}`,
    `생성: ${bundle.snapshot.generatedAt ?? "기록 없음"}`,
    `게시: ${bundle.snapshot.publishedAt ?? "기록 없음"}`,
    `검증: ${bundle.snapshot.verifiedAt ?? "미검증"}`,
    `출처: ${bundle.snapshot.sourceRefs.length ? bundle.snapshot.sourceRefs.join(", ") : "기록 없음"}`,
    ...bundle.documents.flatMap((doc) => [`## ${doc.title}`, doc.content]),
  ].join("\n\n") : "";

  async function copyContext() {
    try { await navigator.clipboard.writeText(allMarkdown); setCopied(true); setTimeout(() => setCopied(false), 1800); }
    catch { setProblem({ title: "복사하지 못했습니다", hint: "브라우저의 클립보드 권한을 확인한 뒤 다시 시도하세요." }); }
  }

  async function download() {
    if (!bundle?.link.scopes.includes("export")) return;
    const epoch = requestEpoch.current;
    try {
      const response = await shareFetch(fetcher, "/api/v1/share/export?format=markdown", { headers: { Accept: "text/markdown" } });
      const blob = await response.blob();
      if (requestEpoch.current !== epoch || stoppedRef.current) return;
      save({ blob, filename: "shared-context.md" });
    } catch (error) {
      if (requestEpoch.current === epoch && !stoppedRef.current) setProblem(safeMessage(error));
    }
  }

  async function logout() {
    if (logoutBusy.current) return;
    logoutBusy.current = true;
    requestEpoch.current += 1;
    desiredToken.current = null;
    stoppedRef.current = true;
    setActiveToken(null);
    setStopped(true);
    setBundle(null);
    setSelected(null);
    setBusy(false);
    setLogoutStatus("pending");
    setProblem({ title: "이 브라우저 화면을 비웠습니다", hint: "서버의 공유 세션 종료를 확인하고 있습니다…" });
    const logoutEpoch = requestEpoch.current;
    try {
      await shareFetch(fetcher, "/api/v1/share/logout", { method: "POST" });
      if (requestEpoch.current !== logoutEpoch || !stoppedRef.current) { void recoverCurrentIntent(); return; }
      setLogoutStatus("succeeded");
      setProblem({ title: "서버 공유 세션을 종료했습니다", hint: "다시 보려면 원래 공유 링크를 여세요." });
    } catch {
      if (requestEpoch.current !== logoutEpoch || !stoppedRef.current) { void recoverCurrentIntent(); return; }
      setLogoutStatus("failed");
      setProblem({ title: "이 브라우저 화면을 비웠지만 서버 세션은 종료되지 않았습니다", hint: "서버 쿠키가 남아 있을 수 있습니다. 서버 로그아웃을 다시 시도하세요." });
    } finally {
      logoutBusy.current = false;
    }
  }

  return <main className="share-page">
    <header className="share-top"><a className="brand" href="/" aria-label="Context Gateway">Context Gateway <span className="badge badge-neutral">공개 Reader</span></a>{(bundle || (busy && activeToken && !stopped)) && <button className="btn btn-ghost btn-sm" onClick={() => void logout()}>이 브라우저에서 나가기</button>}</header>
    {busy ? <section className="share-loading" role="status" aria-label="공유 맥락 불러오는 중"><span className="skeleton-line" /><span className="skeleton-line" /><p>공유 맥락을 확인하고 있습니다…</p></section> : problem ? <section className="share-error" role="alert"><h1>{problem.title}</h1><p>{problem.hint}</p>{stopped && logoutStatus === "failed" ? <button className="btn btn-primary" onClick={() => void logout()}>서버 로그아웃 다시 시도</button> : stopped && logoutStatus === "pending" ? null : !stopped && <button className="btn btn-primary" onClick={() => setRetry((n) => n + 1)}>다시 시도</button>}</section> : bundle ? <>
      <section className="share-intro"><p className="share-kicker">승인된 맥락 · {bundle.link.label}</p><h1>{bundle.project.name}</h1><p>{bundle.project.description || "프로젝트 요약이 없습니다."}</p><dl className="share-meta"><div><dt>공유 만료</dt><dd>{formatDate(bundle.link.expiresAt)}</dd></div><div><dt>스냅샷 게시</dt><dd>{formatDate(bundle.snapshot.publishedAt)}</dd></div><div><dt>정책</dt><dd>{bundle.link.snapshotPolicy === "pinned" ? "승인 당시 고정" : "최신 승인본"}</dd></div><div><dt>세션 만료</dt><dd>{formatDate(bundle.sessionExpiresAt)}</dd></div></dl><div className="share-actions"><button className="btn btn-primary" onClick={() => void copyContext()}>전체 맥락 복사</button>{bundle.link.scopes.includes("export") && <button className="btn" onClick={() => void download()}>Markdown 내려받기</button>}{copied && <span role="status">복사했습니다</span>}</div></section>
      <section className="share-layout"><nav className="share-docs" aria-label="공유 문서"><h2>문서 {bundle.documents.length}</h2>{bundle.documents.map((doc) => <button key={doc.id} className="share-doc" aria-current={selected === doc.id ? "true" : undefined} onClick={() => setSelected(doc.id)}>{doc.title}<small>{doc.id}</small></button>)}</nav><article className="share-article"><section className="share-snapshot" aria-label="승인 스냅샷 출처"><h2>승인 스냅샷 출처</h2><ProvenanceBlock provenance={bundle.provenance} /></section>{selectedDoc ? <ShareDocument document={selectedDoc} /> : <p>공유된 문서가 없습니다.</p>}</article></section>
    </> : null}
    <footer className="share-foot">이 페이지는 링크에 승인된 프로젝트 맥락만 읽습니다. 원문 키는 브라우저 저장소에 보관하지 않습니다. 프로그램 연결에는 별도 발급한 reusable_api_v2 키를 사용하세요. share_link_v2 원문은 REST·MCP Bearer로 사용할 수 없습니다.</footer>
  </main>;
}

function ShareDocument({ document }: { document: GatewayDocument }) {
  return <><header className="reader-head"><h2 className="reader-title">{document.title}</h2></header><ProvenanceBlock provenance={document} /><SafeMarkdown source={document.content} headingOffset={2} /></>;
}

function formatDate(value: string | null): string {
  if (!value) return "기록 없음";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "기록 없음" : new Intl.DateTimeFormat("ko-KR", { dateStyle: "medium", timeStyle: "short" }).format(date);
}
