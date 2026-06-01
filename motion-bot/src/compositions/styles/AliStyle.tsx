import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, spring, interpolate, Easing } from 'remotion';

export type AliType = 'lower_third' | 'chapter_card' | 'icon_text' | 'tip_box';

interface AliStyleProps {
  type: AliType;
  content: string;
  subtext?: string;
  emoji?: string;
  accentColor?: string;
}

const DEFAULT_ACCENT = '#4F46E5'; // Ali indigo

export const AliStyle: React.FC<AliStyleProps> = ({
  type,
  content,
  subtext,
  emoji,
  accentColor = DEFAULT_ACCENT,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const slideX = spring({ frame, fps, config: { damping: 22, stiffness: 130 }, from: -80, to: 0 });
  const fadeIn = spring({ frame, fps, config: { damping: 24, stiffness: 100 }, from: 0, to: 1 });
  const fadeOut = interpolate(frame, [durationInFrames - 12, durationInFrames], [1, 0], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 1, 1),
  });
  const opacity = Math.min(fadeIn, fadeOut);

  const cardStyle: React.CSSProperties = {
    background: '#fff',
    borderRadius: 16,
    boxShadow: '0 8px 48px rgba(0,0,0,0.18)',
    padding: '20px 32px',
    display: 'flex',
    alignItems: 'center',
    gap: 18,
  };

  if (type === 'lower_third') {
    return (
      <AbsoluteFill style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'flex-start', padding: '0 72px 72px', opacity }}>
        <div style={{ transform: `translateX(${slideX}px)` }}>
          <div style={{ ...cardStyle, borderLeft: `6px solid ${accentColor}` }}>
            <div>
              <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 36, fontWeight: 900, color: '#111', lineHeight: 1.2 }}>
                {content}
              </div>
              {subtext && (
                <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 22, color: '#666', marginTop: 4 }}>
                  {subtext}
                </div>
              )}
            </div>
          </div>
        </div>
      </AbsoluteFill>
    );
  }

  if (type === 'chapter_card') {
    const scaleIn = spring({ frame, fps, config: { damping: 16, stiffness: 100 }, from: 0.92, to: 1 });
    return (
      <AbsoluteFill style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.55)', opacity }}>
        <div style={{ transform: `scale(${scaleIn})`, textAlign: 'center' }}>
          <div style={{ ...cardStyle, flexDirection: 'column', padding: '48px 64px', borderRadius: 24 }}>
            {emoji && <div style={{ fontSize: 72, lineHeight: 1 }}>{emoji}</div>}
            <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 56, fontWeight: 900, color: '#111', marginTop: emoji ? 16 : 0 }}>
              {content}
            </div>
            {subtext && (
              <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 28, color: '#555', marginTop: 12 }}>
                {subtext}
              </div>
            )}
          </div>
        </div>
      </AbsoluteFill>
    );
  }

  if (type === 'tip_box') {
    return (
      <AbsoluteFill style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'flex-end', padding: '72px 72px 0', opacity }}>
        <div style={{ transform: `translateX(${-slideX}px)`, maxWidth: 520 }}>
          <div style={{ ...cardStyle, flexDirection: 'column', alignItems: 'flex-start', borderTop: `5px solid ${accentColor}` }}>
            <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 16, fontWeight: 700, color: accentColor, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 8 }}>
              {emoji ? `${emoji} ` : '💡 '}TIP
            </div>
            <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 28, fontWeight: 800, color: '#111', lineHeight: 1.3 }}>
              {content}
            </div>
            {subtext && (
              <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 20, color: '#666', marginTop: 8 }}>
                {subtext}
              </div>
            )}
          </div>
        </div>
      </AbsoluteFill>
    );
  }

  // icon_text — centered card with emoji
  return (
    <AbsoluteFill style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', opacity }}>
      <div style={{ transform: `translateX(${slideX}px)` }}>
        <div style={{ ...cardStyle, padding: '28px 40px', borderRadius: 20 }}>
          {emoji && <div style={{ fontSize: 56, lineHeight: 1 }}>{emoji}</div>}
          <div>
            <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 40, fontWeight: 900, color: '#111' }}>
              {content}
            </div>
            {subtext && (
              <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 22, color: '#666', marginTop: 6 }}>
                {subtext}
              </div>
            )}
          </div>
        </div>
      </div>
    </AbsoluteFill>
  );
};
