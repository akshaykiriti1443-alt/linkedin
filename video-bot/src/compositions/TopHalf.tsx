import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';

// Populated by /new-series after Claude reads the transcript
export default function TopHalf() {
  return (
    <AbsoluteFill style={{ background: 'transparent', width: 1080, height: 960 }}>
      {/* Claude Code writes scene <Sequence> blocks here */}
    </AbsoluteFill>
  );
}
