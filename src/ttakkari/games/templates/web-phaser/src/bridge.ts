// 따까리 관제탑(부모 PWA)과의 postMessage bridge. 게임은 별도 origin 의 sandbox iframe 안에서 돌기 때문에
// 부모는 이 canvas 를 직접 읽을 수 없고, 캡처는 게임이 직접 만들어 부모 origin 으로만 보낸다.
// 부모 origin 은 play URL 의 #ttk-parent=<origin> 힌트로 받고, 메시지는 event.source 와 event.origin 을 모두 확인한다.

export interface CaptureRequest {
  type: 'ttakkari:capture';
  id: string;
}

const MAX_ID_CHARS = 64;

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

// 부모가 보낸 요청만 통과시킨다: 출처 창, origin, 객체 모양, 알려진 type, 짧은 문자열 id.
export function acceptParentMessage(
  event: { origin: string; source: unknown; data: unknown },
  parentWindow: unknown,
  parentOrigin: string | null,
): CaptureRequest | null {
  if (parentOrigin === null || event.source !== parentWindow || event.origin !== parentOrigin) return null;
  const data = event.data as Record<string, unknown> | null;
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null;
  if (data.type !== 'ttakkari:capture' || typeof data.id !== 'string') return null;
  if (data.id.length === 0 || data.id.length > MAX_ID_CHARS) return null;
  return { type: 'ttakkari:capture', id: data.id };
}

export function installBridge(canvasProvider: () => HTMLCanvasElement | null): void {
  const parentOrigin = parentOriginFromHash(window.location.hash);
  window.addEventListener('message', (event) => {
    const request = acceptParentMessage(event, window.parent, parentOrigin);
    if (request === null || parentOrigin === null) return;
    const canvas = canvasProvider();
    if (canvas === null) return;
    window.parent.postMessage(
      { type: 'ttakkari:capture:result', id: request.id, width: canvas.width, height: canvas.height, dataUrl: canvas.toDataURL('image/png') },
      parentOrigin,
    );
  });
}
