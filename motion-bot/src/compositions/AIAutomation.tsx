import React from 'react';
import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
  Easing,
} from 'remotion';
import { loadFont as loadSyne } from '@remotion/google-fonts/Syne';
import { loadFont as loadSpaceMono } from '@remotion/google-fonts/SpaceMono';
import { TextBeat } from './TextBeat';

loadSyne();
loadSpaceMono();

const ORANGE = '#FF6B1A';
const CHARCOAL = '#1A1A1A';
const WHITE = '#FFFFFF';

// ── Subtle grid background ──────────────────────────────────────────────────
const Grid: React.FC = () => (
  <svg
    width="1920"
    height="1080"
    style={{ position: 'absolute', top: 0, left: 0, opacity: 0.04 }}
  >
    <defs>
      <pattern id="grid" width="80" height="80" patternUnits="userSpaceOnUse">
        <path
          d="M 80 0 L 0 0 0 80"
          fill="none"
          stroke={ORANGE}
          strokeWidth="1"
        />
      </pattern>
    </defs>
    <rect width="1920" height="1080" fill="url(#grid)" />
  </svg>
);

// ── Noise grain overlay ──────────────────────────────────────────────────────
const Grain: React.FC = () => {
  const frame = useCurrentFrame();
  // shift noise each frame for animated grain
  const offset = (frame * 37) % 200;
  return (
    <svg
      width="1920"
      height="1080"
      style={{ position: 'absolute', top: 0, left: 0, opacity: 0.025, mixBlendMode: 'multiply' }}
    >
      <filter id="noise">
        <feTurbulence
          type="fractalNoise"
          baseFrequency="0.65"
          numOctaves="3"
          seed={offset}
          stitchTiles="stitch"
        />
        <feColorMatrix type="saturate" values="0" />
      </filter>
      <rect width="1920" height="1080" filter="url(#noise)" />
    </svg>
  );
};

// ── Orange underline that draws left-to-right ────────────────────────────────
const Underline: React.FC<{ startFrame: number; width: number }> = ({ startFrame, width }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const progress = spring({
    frame: frame - startFrame,
    fps,
    config: { damping: 20, stiffness: 80, mass: 0.5 },
    from: 0,
    to: 1,
  });
  return (
    <div
      style={{
        height: 4,
        width: width * progress,
        background: ORANGE,
        borderRadius: 2,
        marginTop: 8,
      }}
    />
  );
};

// ── Pulsing "=" glow ─────────────────────────────────────────────────────────
const EqualsPulse: React.FC<{ startFrame: number }> = ({ startFrame }) => {
  const frame = useCurrentFrame();
  const local = frame - startFrame;
  const glow = interpolate(local, [0, 10, 20, 30], [0, 1, 0.5, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });
  return (
    <span
      style={{
        color: ORANGE,
        textShadow: `0 0 ${30 * glow}px ${ORANGE}, 0 0 ${60 * glow}px ${ORANGE}`,
        transition: 'text-shadow 0.1s',
      }}
    >
      =
    </span>
  );
};

// ── AI badge pill ─────────────────────────────────────────────────────────────
const AIBadge: React.FC<{ startFrame: number }> = ({ startFrame }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - startFrame;
  const scale = spring({
    frame: local,
    fps,
    config: { damping: 8, stiffness: 250, mass: 0.5 },
    from: 0,
    to: 1,
  });
  const opacity = interpolate(local, [0, 8], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  return (
    <div
      style={{
        opacity,
        transform: `scale(${scale})`,
        marginTop: 24,
        display: 'inline-block',
        background: ORANGE,
        borderRadius: 100,
        padding: '12px 36px',
        fontFamily: '"Syne", sans-serif',
        fontWeight: 800,
        fontSize: 28,
        letterSpacing: '0.12em',
        color: WHITE,
        boxShadow: `0 0 40px ${ORANGE}88, 0 0 80px ${ORANGE}44`,
      }}
    >
      AI AUTOMATION
    </div>
  );
};

// ── Warm vignette at edges ────────────────────────────────────────────────────
const Vignette: React.FC<{ startFrame: number }> = ({ startFrame }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame - startFrame, [0, 30], [0, 0.35], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        background: `radial-gradient(ellipse at center, transparent 40%, ${ORANGE}55 140%)`,
        opacity,
        pointerEvents: 'none',
      }}
    />
  );
};

// ── Beat 2 text with inline = pulse ──────────────────────────────────────────
const Beat2Text: React.FC<{ startFrame: number }> = ({ startFrame }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - startFrame;

  const slideY = spring({
    frame: local,
    fps,
    config: { damping: 18, stiffness: 120, mass: 0.8 },
    from: 60,
    to: 0,
  });
  const opacity = interpolate(local, [0, 12], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });

  return (
    <div
      style={{
        opacity,
        transform: `translateY(${slideY}px)`,
        fontFamily: '"Syne", sans-serif',
        fontSize: 72,
        fontWeight: 700,
        color: ORANGE,
        letterSpacing: '0.01em',
      }}
    >
      Automations <EqualsPulse startFrame={startFrame + 10} /> repetitive processes.
    </div>
  );
};

// ── Beat 4: "Now that code is done by AI!" with zoom punch ───────────────────
const Beat4Text: React.FC<{ startFrame: number }> = ({ startFrame }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const local = frame - startFrame;

  const scale = spring({
    frame: local,
    fps,
    config: { damping: 10, stiffness: 200, mass: 0.6 },
    from: 1.3,
    to: 1.0,
  });
  const opacity = interpolate(local, [0, 10], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  // exclamation flash
  const exFlash = interpolate(local, [0, 6, 14, 22], [0, 1, 0.6, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  return (
    <div
      style={{
        opacity,
        transform: `scale(${scale})`,
        fontFamily: '"Syne", sans-serif',
        fontSize: 88,
        fontWeight: 800,
        color: CHARCOAL,
        letterSpacing: '0.01em',
      }}
    >
      Now that code is done by{' '}
      <span style={{ color: ORANGE }}>AI</span>
      <span
        style={{
          color: ORANGE,
          textShadow: `0 0 ${20 * exFlash}px ${ORANGE}`,
        }}
      >
        !
      </span>
    </div>
  );
};

// ── Main composition ──────────────────────────────────────────────────────────
export default function AIAutomation() {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Beat 1 scales down when beat 2 arrives
  const beat1Scale = interpolate(frame, [48, 72], [1, 0.6], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });
  const beat1Y = interpolate(frame, [48, 72], [0, -160], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });

  // Beat 2 moves up when beat 3 arrives
  const beat2Y = interpolate(frame, [96, 120], [0, -140], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 0.2, 1),
  });
  const beat2Scale = interpolate(frame, [96, 120], [1, 0.75], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  // Stack opacity fade for older beats
  const beat1Opacity = interpolate(frame, [96, 120], [1, 0.35], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const beat2Opacity = interpolate(frame, [144, 168], [1, 0.35], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill style={{ background: WHITE, overflow: 'hidden' }}>
      <Grid />
      <Grain />

      {/* Vignette appears at beat 5 */}
      {frame >= 192 && <Vignette startFrame={192} />}

      {/* All beats centred in a flex column */}
      <AbsoluteFill
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 0,
        }}
      >
        {/* ── Beat 1 ── */}
        {frame >= 0 && (
          <div
            style={{
              transform: `translateY(${beat1Y}px) scale(${beat1Scale})`,
              opacity: beat1Opacity,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
            }}
          >
            <TextBeat
              text="Automations always existed."
              startFrame={0}
              beatStyle="syne"
              color={CHARCOAL}
              fontSize={72}
            />
            {/* Underline draws from frame 20 */}
            {frame >= 20 && <Underline startFrame={20} width={780} />}
          </div>
        )}

        {/* ── Beat 2 ── */}
        {frame >= 48 && (
          <div
            style={{
              transform: `translateY(${beat2Y}px) scale(${beat2Scale})`,
              opacity: beat2Opacity,
              marginTop: 32,
            }}
          >
            <Beat2Text startFrame={48} />
          </div>
        )}

        {/* ── Beat 3 (typewriter) ── */}
        {frame >= 96 && (
          <div style={{ marginTop: frame < 144 ? 32 : 0 }}>
            <TextBeat
              text="Processed by code."
              startFrame={96}
              beatStyle="mono"
              color={CHARCOAL}
              fontSize={64}
              isTypewriter
            />
          </div>
        )}

        {/* ── Beat 4 ── */}
        {frame >= 144 && (
          <div style={{ marginTop: 40 }}>
            <Beat4Text startFrame={144} />
          </div>
        )}

        {/* ── Beat 5 ── */}
        {frame >= 192 && (
          <div
            style={{
              marginTop: 40,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
            }}
          >
            <TextBeat
              text="As simple as that — that's AI Automation."
              startFrame={192}
              beatStyle="syne"
              color={CHARCOAL}
              fontSize={52}
              italic
            />
            <AIBadge startFrame={210} />
          </div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
}
