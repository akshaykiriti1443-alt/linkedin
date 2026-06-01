import React from 'react';
import {
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
  Easing,
  AbsoluteFill,
} from 'remotion';

export type BeatStyle = 'syne' | 'mono';

interface TextBeatProps {
  text: string;
  startFrame: number;
  beatStyle?: BeatStyle;
  color?: string;
  fontSize?: number;
  isTypewriter?: boolean;
  highlight?: string; // substring to color orange
  zoomPunch?: boolean;
  italic?: boolean;
}

const ORANGE = '#FF6B1A';
const CHARCOAL = '#1A1A1A';

export const TextBeat: React.FC<TextBeatProps> = ({
  text,
  startFrame,
  beatStyle = 'syne',
  color = CHARCOAL,
  fontSize = 72,
  isTypewriter = false,
  highlight,
  zoomPunch = false,
  italic = false,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = frame - startFrame;

  // Slide-up + fade entrance via spring
  const slideY = spring({
    frame: localFrame,
    fps,
    config: { damping: 18, stiffness: 120, mass: 0.8 },
    from: 60,
    to: 0,
  });

  const opacity = interpolate(localFrame, [0, 12], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });

  // Zoom punch (beat 4)
  const scale = zoomPunch
    ? spring({
        frame: localFrame,
        fps,
        config: { damping: 10, stiffness: 200, mass: 0.6 },
        from: 1.3,
        to: 1.0,
      })
    : 1;

  const fontFamily =
    beatStyle === 'mono'
      ? '"Courier New", "Courier", monospace'
      : '"Arial Black", "Arial Bold", "Helvetica Neue", sans-serif';

  const renderText = () => {
    if (!highlight) {
      return <span style={{ color }}>{text}</span>;
    }
    const idx = text.indexOf(highlight);
    if (idx === -1) return <span style={{ color }}>{text}</span>;
    return (
      <>
        <span style={{ color }}>{text.slice(0, idx)}</span>
        <span style={{ color: ORANGE }}>{highlight}</span>
        <span style={{ color }}>{text.slice(idx + highlight.length)}</span>
      </>
    );
  };

  // Typewriter effect (beat 3)
  if (isTypewriter) {
    const charsPerFrame = 1 / 3;
    const visibleChars = Math.min(
      Math.floor(localFrame * charsPerFrame),
      text.length
    );
    const showCursor = localFrame >= 0;
    const cursorBlink = Math.floor(localFrame / 15) % 2 === 0;

    return (
      <div
        style={{
          opacity,
          transform: `translateY(${slideY}px)`,
          fontFamily,
          fontSize,
          fontWeight: 700,
          color,
          letterSpacing: '0.02em',
          display: 'flex',
          alignItems: 'center',
        }}
      >
        {text.slice(0, visibleChars)}
        {showCursor && (
          <span
            style={{
              color: ORANGE,
              opacity: cursorBlink ? 1 : 0,
              marginLeft: 2,
            }}
          >
            ▌
          </span>
        )}
      </div>
    );
  }

  return (
    <div
      style={{
        opacity,
        transform: `translateY(${slideY}px) scale(${scale})`,
        fontFamily,
        fontSize,
        fontWeight: 700,
        fontStyle: italic ? 'italic' : 'normal',
        letterSpacing: '0.01em',
        lineHeight: 1.2,
      }}
    >
      {renderText()}
    </div>
  );
};
