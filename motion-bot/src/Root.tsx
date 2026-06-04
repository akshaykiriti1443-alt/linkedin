import React from 'react';
import { Composition } from 'remotion';
import AIAutomation from './compositions/AIAutomation';

export const RemotionRoot: React.FC = () => (
  <Composition
    id="AIAutomation"
    component={AIAutomation}
    durationInFrames={240}
    fps={30}
    width={1920}
    height={1080}
  />
);
