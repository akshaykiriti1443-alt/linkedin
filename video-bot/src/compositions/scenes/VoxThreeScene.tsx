import React, { useMemo } from 'react';
import { useCurrentFrame, useVideoConfig, interpolate, spring } from 'remotion';
import { ThreeCanvas } from '@remotion/three';
import * as THREE from 'three';

interface Props {
  label: string;
  sublabel?: string;
  accentColor?: string;
}

// Terrain geometry — flat grid with random height displacement
function TerrainMesh({ frame, accentColor }: { frame: number; accentColor: string }) {
  const geometry = useMemo(() => {
    const geo = new THREE.PlaneGeometry(12, 8, 40, 26);
    const pos = geo.attributes.position;
    for (let i = 0; i < pos.count; i++) {
      const x = pos.getX(i);
      const y = pos.getY(i);
      // Deterministic noise via sin/cos combos
      const z = Math.sin(x * 0.8) * Math.cos(y * 0.6) * 0.4
              + Math.sin(x * 1.6 + y * 0.9) * 0.2
              + Math.cos(x * 0.4 + y * 1.2) * 0.3;
      pos.setZ(i, z);
    }
    geo.computeVertexNormals();
    return geo;
  }, []);

  const wireGeo = useMemo(() => new THREE.WireframeGeometry(geometry), [geometry]);

  const animZ = interpolate(frame, [0, 60], [-2, 0], { extrapolateRight: 'clamp' });

  return (
    <group position={[0, -1.5, animZ]} rotation={[-0.35, 0, 0]}>
      {/* Solid terrain with grunge-tinted material */}
      <mesh geometry={geometry}>
        <meshStandardMaterial
          color="#111118"
          roughness={0.95}
          metalness={0.1}
        />
      </mesh>
      {/* Neon wireframe overlay */}
      <lineSegments geometry={wireGeo}>
        <lineBasicMaterial color={accentColor} transparent opacity={0.45} />
      </lineSegments>
    </group>
  );
}

// Neon bezier arc (camera-sweep indicator line)
function NeonArc({ frame, accentColor }: { frame: number; accentColor: string }) {
  const points = useMemo(() => {
    const curve = new THREE.CubicBezierCurve3(
      new THREE.Vector3(-5, 0.2, 0),
      new THREE.Vector3(-2, 2.5, -1),
      new THREE.Vector3(2, 2.5, -1),
      new THREE.Vector3(5, 0.2, 0),
    );
    return curve.getPoints(80);
  }, []);

  const drawn = Math.floor(interpolate(frame, [10, 50], [0, 80], { extrapolateRight: 'clamp' }));
  const visiblePoints = points.slice(0, Math.max(2, drawn));

  const lineObj = useMemo(() => {
    const geo = new THREE.BufferGeometry().setFromPoints(visiblePoints);
    const mat = new THREE.LineBasicMaterial({ color: accentColor, linewidth: 2 });
    return new THREE.Line(geo, mat);
  }, [visiblePoints, accentColor]);

  return <primitive object={lineObj} />;
}

// Floating data label planes
function DataLabel({ position, text, frame, delay }: {
  position: [number, number, number];
  text: string;
  frame: number;
  delay: number;
}) {
  const opacity = interpolate(frame, [delay, delay + 20], [0, 1], { extrapolateRight: 'clamp' });
  return (
    <group position={position}>
      <mesh>
        <planeGeometry args={[1.8, 0.5]} />
        <meshBasicMaterial color="#000000" transparent opacity={opacity * 0.75} />
      </mesh>
    </group>
  );
}

export const VoxThreeScene: React.FC<Props> = ({
  label,
  sublabel,
  accentColor = '#ff6b00',
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  // Smooth camera orbit driven by frame
  const camX = interpolate(frame, [0, 150], [-3, 3]);
  const camY = interpolate(frame, [0, 150], [3.5, 2.5]);
  const camZ = interpolate(frame, [0, 150], [6, 5]);

  // Title slide-up
  const titleY = interpolate(frame, [5, 30], [40, 0], { extrapolateRight: 'clamp' });
  const titleOpacity = interpolate(frame, [5, 30], [0, 1], { extrapolateRight: 'clamp' });

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative', background: '#0a0a0f' }}>
      {/* Three.js WebGL canvas */}
      <ThreeCanvas
        width={1080}
        height={960}
        camera={{
          position: [camX, camY, camZ],
          fov: 42,
        }}
      >
        <ambientLight intensity={0.3} />
        <directionalLight position={[5, 8, 3]} intensity={1.2} color="#ffffff" />
        <pointLight position={[-4, 3, 2]} intensity={0.8} color={accentColor} />

        <TerrainMesh frame={frame} accentColor={accentColor} />
        <NeonArc frame={frame} accentColor={accentColor} />
        <DataLabel position={[-3, 0.5, 0]} text="A" frame={frame} delay={20} />
        <DataLabel position={[0, 0.5, 0]} text="B" frame={frame} delay={35} />
        <DataLabel position={[3, 0.5, 0]} text="C" frame={frame} delay={50} />
      </ThreeCanvas>

      {/* HTML overlay — editorial title */}
      <div style={{
        position: 'absolute',
        top: 48,
        left: 60,
        right: 60,
        transform: `translateY(${titleY}px)`,
        opacity: titleOpacity,
      }}>
        <div style={{
          fontFamily: 'Arial Black, Arial, sans-serif',
          fontSize: 52,
          fontWeight: 900,
          color: '#ffffff',
          lineHeight: 1.1,
          textTransform: 'uppercase',
          letterSpacing: '-1px',
          textShadow: `0 0 24px ${accentColor}88`,
        }}>
          {label}
        </div>
        {sublabel && (
          <div style={{
            fontFamily: 'Arial, sans-serif',
            fontSize: 24,
            color: accentColor,
            marginTop: 8,
            fontWeight: 700,
            letterSpacing: '2px',
            textTransform: 'uppercase',
          }}>
            {sublabel}
          </div>
        )}
        {/* Accent bar */}
        <div style={{
          width: 60,
          height: 4,
          background: accentColor,
          marginTop: 14,
          borderRadius: 2,
        }} />
      </div>

      {/* Corner grunge texture simulation via CSS */}
      <div style={{
        position: 'absolute',
        inset: 0,
        backgroundImage: `radial-gradient(ellipse at 0% 100%, ${accentColor}18 0%, transparent 60%),
                          radial-gradient(ellipse at 100% 0%, #ffffff08 0%, transparent 50%)`,
        pointerEvents: 'none',
      }} />
    </div>
  );
};
