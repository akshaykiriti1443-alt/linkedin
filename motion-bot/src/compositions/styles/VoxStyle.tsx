import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, spring, interpolate, Easing } from 'remotion';

export type VoxType = 'text_callout' | 'highlight_word' | 'stat' | 'chapter_card';

interface VoxStyleProps {
  type: VoxType;
  content: string;
  subtext?: string;
  statValue?: string;
  accentColor?: string;
}

const DEFAULT_ACCENT = '#F5C518'; // Vox yellow

export const VoxStyle: React.FC<VoxStyleProps> = ({
  type,
  content,
  subtext,
  statValue,
  accentColor = DEFAULT_ACCENT,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const fadeIn = spring({ frame, fps, config: { damping: 20, stiffness: 120 }, from: 0, to: 1 });
  const fadeOut = interpolate(frame, [durationInFrames - 10, durationInFrames], [1, 0], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp',
    easing: Easing.bezier(0.4, 0, 1, 1),
  });
  const opacity = Math.min(fadeIn, fadeOut);

  const slideY = spring({ frame, fps, config: { damping: 18, stiffness: 140 }, from: 40, to: 0 });

  if (type === 'chapter_card') {
    return (
      <AbsoluteFill style={{ background: 'rgba(0,0,0,0.82)', display: 'flex', alignItems: 'center', justifyContent: 'center', opacity }}>
        <div style={{ textAlign: 'center', transform: `translateY(${slideY}px)` }}>
          <div style={{
            width: 64, height: 6, background: accentColor, borderRadius: 3,
            margin: '0 auto 24px',
          }} />
          <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 72, fontWeight: 900, color: '#fff', lineHeight: 1.1, letterSpacing: '-0.02em' }}>
            {content}
          </div>
          {subtext && (
            <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 32, color: 'rgba(255,255,255,0.65)', marginTop: 16 }}>
              {subtext}
            </div>
          )}
        </div>
      </AbsoluteFill>
    );
  }

  if (type === 'stat') {
    const countProgress = spring({ frame, fps, config: { damping: 28, stiffness: 60 }, from: 0, to: 1 });
    return (
      <AbsoluteFill style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'flex-start', padding: '0 80px 80px', opacity }}>
        <div style={{ transform: `translateY(${slideY}px)` }}>
          <div style={{
            background: accentColor,
            display: 'inline-block',
            padding: '6px 18px',
            marginBottom: 8,
            fontFamily: '"Arial Black", sans-serif',
            fontSize: 22,
            fontWeight: 900,
            color: '#000',
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
          }}>
            {content}
          </div>
          <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 110, fontWeight: 900, color: '#fff', lineHeight: 1, textShadow: '0 4px 32px rgba(0,0,0,0.6)' }}>
            {statValue || '—'}
          </div>
          {subtext && (
            <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 28, color: 'rgba(255,255,255,0.75)', marginTop: 8 }}>
              {subtext}
            </div>
          )}
        </div>
      </AbsoluteFill>
    );
  }

  if (type === 'highlight_word') {
    const lineWidth = spring({ frame, fps, config: { damping: 22, stiffness: 80 }, from: 0, to: 1 });
    return (
      <AbsoluteFill style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', opacity }}>
        <div style={{ textAlign: 'center', transform: `translateY(${slideY}px)` }}>
          <div style={{ position: 'relative', display: 'inline-block' }}>
            <div style={{
              fontFamily: '"Arial Black", sans-serif', fontSize: 96, fontWeight: 900,
              color: '#fff', letterSpacing: '-0.02em', textShadow: '0 4px 32px rgba(0,0,0,0.5)',
            }}>
              {content}
            </div>
            <div style={{
              position: 'absolute', bottom: -8, left: 0,
              height: 8, width: `${lineWidth * 100}%`,
              background: accentColor, borderRadius: 4,
            }} />
          </div>
          {subtext && (
            <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 36, color: 'rgba(255,255,255,0.8)', marginTop: 24 }}>
              {subtext}
            </div>
          )}
        </div>
      </AbsoluteFill>
    );
  }

  // Default: text_callout — bottom-left bold callout box
  return (
    <AbsoluteFill style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'flex-start', padding: '0 80px 80px', opacity }}>
      <div style={{ transform: `translateY(${slideY}px)`, maxWidth: 900 }}>
        <div style={{
          background: accentColor,
          display: 'inline-block',
          padding: '14px 28px',
          marginBottom: 0,
        }}>
          <div style={{ fontFamily: '"Arial Black", sans-serif', fontSize: 42, fontWeight: 900, color: '#000', lineHeight: 1.2, letterSpacing: '-0.01em' }}>
            {content}
          </div>
          {subtext && (
            <div style={{ fontFamily: 'Arial, sans-serif', fontSize: 24, color: 'rgba(0,0,0,0.75)', marginTop: 4 }}>
              {subtext}
            </div>
          )}
        </div>
      </div>
    </AbsoluteFill>
  );
};
