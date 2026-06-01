import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';

// Populated by /real-overlay after Claude reads word timestamps
export default function Overlay() {
  return (
    <AbsoluteFill style={{ background: 'transparent' }}>
      {/* Claude Code writes overlay <Sequence> blocks here */}
    </AbsoluteFill>
  );
}
