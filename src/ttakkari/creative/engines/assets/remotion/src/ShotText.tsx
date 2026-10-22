import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

// 샷 하나의 타이포 카드. 모든 값은 프레임 번호의 순수 함수다(누적 상태 없음).
export type ShotTextProps = {
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
  title: string;
  subtitle: string;
  palette: string[];
  fontFamily: string;
  // 샷 시작 기준 프레임 번호. 이 프레임마다 제목이 한 번 맥박친다(오디오 이벤트 동기).
  cueFrames: number[];
  fadeOutFrames: number;
};

export const ShotText: React.FC<ShotTextProps> = (p) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames, height } = useVideoConfig();
  const bg = p.palette[0] ?? "#0B0F10";
  const fg = p.palette[1] ?? "#F4F4ED";
  const accent = p.palette[2] ?? fg;
  const enter = spring({ frame, fps, config: { damping: 14, stiffness: 160, mass: 0.6 } });
  const fade =
    p.fadeOutFrames > 0
      ? interpolate(frame, [durationInFrames - p.fadeOutFrames, durationInFrames - 1], [1, 0], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
        })
      : 1;
  const pulse = p.cueFrames.reduce(
    (acc, c) =>
      acc + interpolate(frame - c, [0, 6], [1, 0], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
    0,
  );
  const drift = interpolate(frame, [0, Math.max(1, durationInFrames - 1)], [0, 1]);
  const angle = 120 + drift * 40;
  const lift = (1 - enter) * 60;
  const scale = 1 + Math.min(pulse, 1) * 0.06;
  return (
    <AbsoluteFill
      style={{
        background: "linear-gradient(" + angle + "deg, " + bg + ", " + accent + "33)",
        justifyContent: "center",
        alignItems: "center",
        fontFamily: p.fontFamily,
        opacity: fade,
      }}
    >
      <div
        style={{
          color: fg,
          fontSize: height * 0.12,
          fontWeight: 800,
          textAlign: "center",
          padding: "0 6%",
          transform: "translateY(" + lift + "px) scale(" + scale + ")",
          opacity: enter,
        }}
      >
        {p.title}
      </div>
      {p.subtitle ? (
        <div style={{ color: accent, fontSize: height * 0.045, marginTop: height * 0.03, opacity: enter }}>
          {p.subtitle}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
