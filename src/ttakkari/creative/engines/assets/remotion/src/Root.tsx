import { Composition } from "remotion";
import { ShotText, type ShotTextProps } from "./ShotText";

const defaults: ShotTextProps = {
  width: 1280,
  height: 720,
  fps: 24,
  durationInFrames: 48,
  title: "Title",
  subtitle: "",
  palette: ["#0B0F10", "#F4F4ED", "#00F0B5"],
  fontFamily: "sans-serif",
  cueFrames: [],
  fadeOutFrames: 0,
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id="ShotText"
    component={ShotText}
    width={defaults.width}
    height={defaults.height}
    fps={defaults.fps}
    durationInFrames={defaults.durationInFrames}
    defaultProps={defaults}
    calculateMetadata={({ props }) => ({
      width: props.width,
      height: props.height,
      fps: props.fps,
      durationInFrames: props.durationInFrames,
    })}
  />
);
