// 플레이 녹화(ADR 0060): 캔버스를 낮은 해상도 JPEG 프레임으로 주기적으로 모아 둔다. 영상 파일이 아니라 프레임 시퀀스인 이유는 ADR 0060 참고.
// 메모리에만 두고 stop 때 한 번에 돌려준다. 상한은 게임이 스스로 지키고, 넘으면 오래된 프레임을 버리지 않고 녹화를 멈춘다(조용히 잘리지 않는다).
// 엔진과 무관하다(캔버스 요소만 쓴다). 부모와의 메시지는 bridge.ts 가 맡는다.

export interface RecordLimits {
  maxMs: number;
  maxFps: number;
  maxWidth: number;
  maxBytes: number;
  maxFrames: number;
  jpegQuality: number;
}

export const RECORD_LIMITS: RecordLimits = { maxMs: 30_000, maxFps: 6, maxWidth: 480, maxBytes: 6 * 1024 * 1024, maxFrames: 180, jpegQuality: 0.6 };
export const DEFAULT_FPS = 4;

export interface RecordFrame {
  tMs: number;
  dataUrl: string;
}

export type RecordReason = 'stopped' | 'limit_duration' | 'limit_bytes' | 'limit_frames' | 'canvas_unavailable';

export interface RecordResult {
  frames: RecordFrame[];
  width: number;
  height: number;
  fps: number;
  durationMs: number;
  reason: RecordReason;
}

export interface Clock {
  now: () => number;
  setInterval: (fn: () => void, ms: number) => unknown;
  clearInterval: (id: unknown) => void;
}

const JPEG_PREFIX = 'data:image/jpeg;base64,';
const realClock: Clock = {
  now: () => performance.now(),
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (id) => clearInterval(id as ReturnType<typeof setInterval>),
};

export function clampFps(value: unknown, limits: RecordLimits = RECORD_LIMITS): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return Math.min(DEFAULT_FPS, limits.maxFps);
  return Math.min(limits.maxFps, Math.max(1, Math.round(value)));
}

/** 프레임 하나를 만든다. 캔버스를 가로 maxWidth 이하로 줄여 JPEG data URL 로. 실패하면 null. */
export type FrameGrabber = (canvas: HTMLCanvasElement, maxWidth: number, quality: number) => { dataUrl: string; width: number; height: number } | null;

export const grabFrame: FrameGrabber = (canvas, maxWidth, quality) => {
  if (canvas.width <= 0 || canvas.height <= 0) return null;
  const scale = Math.min(1, maxWidth / canvas.width);
  const width = Math.max(1, Math.round(canvas.width * scale));
  const height = Math.max(1, Math.round(canvas.height * scale));
  const scratch = document.createElement('canvas');
  scratch.width = width;
  scratch.height = height;
  const ctx = scratch.getContext('2d');
  if (ctx === null) return null;
  ctx.drawImage(canvas, 0, 0, width, height);
  const dataUrl = scratch.toDataURL('image/jpeg', quality);
  return dataUrl.startsWith(JPEG_PREFIX) ? { dataUrl, width, height } : null;
};

export class FrameRecorder {
  private frames: RecordFrame[] = [];
  private bytes = 0;
  private timer: unknown = null;
  private startedAt = 0;
  private fps = DEFAULT_FPS;
  private size = { width: 0, height: 0 };
  private ended: RecordReason | null = null;
  private endedAt = 0;

  // 생성자 매개변수 프로퍼티(private readonly x)는 node 의 타입 제거 모드가 읽지 못해 필드를 직접 선언한다.
  private readonly canvasProvider: () => HTMLCanvasElement | null;
  private readonly limits: RecordLimits;
  private readonly clock: Clock;
  private readonly grab: FrameGrabber;

  constructor(canvasProvider: () => HTMLCanvasElement | null, limits: RecordLimits = RECORD_LIMITS, clock: Clock = realClock, grab: FrameGrabber = grabFrame) {
    this.canvasProvider = canvasProvider;
    this.limits = limits;
    this.clock = clock;
    this.grab = grab;
  }

  get active(): boolean {
    return this.timer !== null;
  }

  /** 녹화를 시작한다. 이미 녹화 중이면 처음부터 다시 시작한다(이전 프레임은 버린다). */
  start(fps?: unknown): void {
    this.halt();
    this.frames = [];
    this.bytes = 0;
    this.ended = null;
    this.fps = clampFps(fps, this.limits);
    this.startedAt = this.clock.now();
    this.tick();
    if (this.ended === null) this.timer = this.clock.setInterval(() => this.tick(), Math.round(1000 / this.fps));
  }

  /** 녹화를 끝내고 모은 프레임을 돌려준다. 한도로 이미 멈춘 경우 그 사유가 reason 이다. 녹화 중이 아니었으면 빈 결과. */
  stop(): RecordResult {
    const wasActive = this.timer !== null;
    this.halt();
    const reason: RecordReason = this.ended ?? 'stopped';
    const durationMs = this.ended !== null ? this.endedAt - this.startedAt : wasActive ? this.clock.now() - this.startedAt : 0;
    const result: RecordResult = { frames: this.frames, width: this.size.width, height: this.size.height, fps: this.fps, durationMs: Math.max(0, Math.round(durationMs)), reason };
    this.frames = [];
    this.bytes = 0;
    this.ended = null;
    return result;
  }

  private halt(): void {
    if (this.timer !== null) this.clock.clearInterval(this.timer);
    this.timer = null;
  }

  private finish(reason: RecordReason): void {
    this.ended = reason;
    this.endedAt = this.clock.now();
    this.halt();
  }

  private tick(): void {
    const elapsed = this.clock.now() - this.startedAt;
    if (elapsed > this.limits.maxMs) return this.finish('limit_duration');
    if (this.frames.length >= this.limits.maxFrames) return this.finish('limit_frames');
    const canvas = this.canvasProvider();
    if (canvas === null) return this.finish('canvas_unavailable');
    const grabbed = this.grab(canvas, this.limits.maxWidth, this.limits.jpegQuality);
    if (grabbed === null) return this.finish('canvas_unavailable');
    const bytes = Math.floor(((grabbed.dataUrl.length - JPEG_PREFIX.length) * 3) / 4);
    if (this.bytes + bytes > this.limits.maxBytes) return this.finish('limit_bytes');
    this.bytes += bytes;
    this.size = { width: grabbed.width, height: grabbed.height };
    this.frames.push({ tMs: Math.max(0, Math.round(elapsed)), dataUrl: grabbed.dataUrl });
  }
}
