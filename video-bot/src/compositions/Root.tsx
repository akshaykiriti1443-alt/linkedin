import React from 'react';
import { Composition } from 'remotion';
import TopHalf from './TopHalf';
import Overlay from './Overlay';

const DURATION = parseInt(process.env.VIDEO_FRAMES ?? '900', 10); // default 30s

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="TopHalf"
      component={TopHalf}
      durationInFrames={DURATION}
      fps={30}
      width={1080}
      height={960}
    />
    <Composition
      id="Overlay"
      component={Overlay}
      durationInFrames={DURATION}
      fps={30}
      width={1080}
      height={1920}
    />
  </>
);
