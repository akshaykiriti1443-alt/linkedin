import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';
import { VoxThreeScene } from './scenes/VoxThreeScene';

// This file is auto-populated by /new-series.
// Claude reads transcript.json and writes <Sequence> blocks with VoxThreeScene props.
// Each segment maps to one scene: label = key phrase, sublabel = context, accentColor = theme.

export default function TopHalf() {
  return (
    <AbsoluteFill style={{ background: 'transparent', width: 1080, height: 960 }}>
      {/* EXAMPLE — /new-series replaces this block with real transcript-driven scenes */}
      <Sequence from={0} durationInFrames={150}>
        <VoxThreeScene
          label="Your Topic Here"
          sublabel="Key insight"
          accentColor="#ff6b00"
        />
      </Sequence>
    </AbsoluteFill>
  );
}
