// 따까리 관제탑(부모 PWA)과의 postMessage bridge. 게임은 별도 origin 의 sandbox iframe 안에서 돌기 때문에
// 부모는 이 canvas 를 직접 읽을 수 없고, 캡처는 게임이 직접 만들어 부모 origin 으로만 보낸다.
// 부모 origin 은 play URL 의 #ttk-parent=<origin> 힌트로 받고, 메시지는 event.source 와 event.origin 을 모두 확인한다.
//
// 부모→게임 요청(모두 {type, id}): capture · pause · resume · state · save-export · save-import(+data)
// 게임→부모 응답(모두 부모 origin 으로만): capture:result · paused · state:result · save-export:result · save-import:result

export type ParentRequestType = 'capture' | 'pause' | 'resume' | 'state' | 'save-export' | 'save-import';

export interface ParentRequest {
  type: `ttakkari:${ParentRequestType}`;
  id: string;
  data?: string;
}

// 하위 호환: 이전 이름. 캡처 요청도 ParentRequest 의 한 종류다.
export type CaptureRequest = ParentRequest;

const MAX_ID_CHARS = 64;
const MAX_SAVE_CHARS = 64 * 1024;
const MAX_STATE_CHARS = 16 * 1024;
const KNOWN: ReadonlySet<string> = new Set<string>(['capture', 'pause', 'resume', 'state', 'save-export', 'save-import'].map((t) => `ttakkari:${t}`));

export function parentOriginFromHash(hash: string): string | null {
  const raw = new URLSearchParams(hash.replace(/^#/, '')).get('ttk-parent');
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return (url.protocol === 'https:' || url.protocol === 'http:') && url.origin === raw ? raw : null;
  } catch {
    return null;
  }
}

// 부모가 보낸 요청만 통과시킨다: 출처 창, origin, 객체 모양, 알려진 type, 짧은 문자열 id, 크기가 제한된 data.
export function acceptParentMessage(
  event: { origin: string; source: unknown; data: unknown },
  parentWindow: unknown,
  parentOrigin: string | null,
): ParentRequest | null {
  if (parentOrigin === null || event.source !== parentWindow || event.origin !== parentOrigin) return null;
  const data = event.data as Record<string, unknown> | null;
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null;
  if (typeof data.type !== 'string' || !KNOWN.has(data.type) || typeof data.id !== 'string') return null;
  if (data.id.length === 0 || data.id.length > MAX_ID_CHARS) return null;
  const type = data.type as ParentRequest['type'];
  if (type === 'ttakkari:save-import') {
    if (typeof data.data !== 'string' || data.data.length > MAX_SAVE_CHARS) return null;
    return { type, id: data.id, data: data.data };
  }
  return { type, id: data.id };
}

// 게임이 부모에게 내보이는 값. sandbox(opaque origin)라 localStorage/IndexedDB 를 쓸 수 없으므로 저장은 메모리 + export/import 뿐이다
// (docs/adr/0052 의 결정). 직렬화 가능한 값만 둔다.
let published: unknown = {};
let saveStore: Record<string, unknown> = {};

/** 점수·위치 같은 계측 값을 게시한다. 부모의 state 요청과 자동화 테스트(G13/G14)가 읽는다. */
export function publishState(state: unknown): void {
  published = state;
}

export const save = {
  get: (key: string): unknown => saveStore[key],
  set: (key: string, value: unknown): void => {
    saveStore[key] = value;
  },
  clear: (): void => {
    saveStore = {};
  },
};

export interface BridgeControls {
  pause?: () => void;
  resume?: () => void;
  isPaused?: () => boolean;
}

export function installBridge(canvasProvider: () => HTMLCanvasElement | null, controls: BridgeControls = {}): void {
  const parentOrigin = parentOriginFromHash(window.location.hash);
  window.addEventListener('message', (event) => {
    const request = acceptParentMessage(event, window.parent, parentOrigin);
    if (request === null || parentOrigin === null) return;
    const reply = (message: Record<string, unknown>) => window.parent.postMessage({ ...message, id: request.id }, parentOrigin);
    switch (request.type) {
      case 'ttakkari:capture': {
        const canvas = canvasProvider();
        if (canvas === null) return;
        reply({ type: 'ttakkari:capture:result', width: canvas.width, height: canvas.height, dataUrl: canvas.toDataURL('image/png') });
        return;
      }
      case 'ttakkari:pause':
      case 'ttakkari:resume':
        if (request.type === 'ttakkari:pause') controls.pause?.();
        else controls.resume?.();
        reply({ type: 'ttakkari:paused', paused: controls.isPaused?.() ?? false, supported: controls.pause !== undefined });
        return;
      case 'ttakkari:state': {
        let json = JSON.stringify(published ?? null);
        if (json.length > MAX_STATE_CHARS) json = 'null';
        reply({ type: 'ttakkari:state:result', state: JSON.parse(json) });
        return;
      }
      case 'ttakkari:save-export': {
        const data = JSON.stringify(saveStore);
        reply({ type: 'ttakkari:save-export:result', data: data.length <= MAX_SAVE_CHARS ? data : '{}' });
        return;
      }
      case 'ttakkari:save-import': {
        let ok = false;
        try {
          const parsed: unknown = JSON.parse(request.data ?? '');
          if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
            saveStore = parsed as Record<string, unknown>;
            ok = true;
          }
        } catch {
          ok = false;
        }
        reply({ type: 'ttakkari:save-import:result', ok });
        return;
      }
    }
  });
}
