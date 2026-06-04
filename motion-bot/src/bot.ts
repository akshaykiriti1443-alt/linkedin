#!/usr/bin/env tsx
import fs from "fs";
import path from "path";
import { execSync } from "child_process";

const COMPOSITIONS_DIR = path.resolve("src/compositions");
const INDEX_FILE = path.resolve("src/index.ts");
const OUT_DIR = path.resolve("out");

export function ensureDirs() {
  fs.mkdirSync(COMPOSITIONS_DIR, { recursive: true });
  fs.mkdirSync(OUT_DIR, { recursive: true });
}

export function writeComposition(code: string, name: string): string {
  ensureDirs();
  const file = path.join(COMPOSITIONS_DIR, `${name}.tsx`);
  fs.writeFileSync(file, code, "utf8");
  return file;
}

export function writeIndex(name: string, durationInFrames = 150, fps = 30) {
  const content = `import React from 'react';
import { Composition } from 'remotion';
import Scene from './compositions/${name}.js';

export const RemotionRoot = () => (
  <Composition
    id="Scene"
    component={Scene}
    durationInFrames={${durationInFrames}}
    fps={${fps}}
    width={1920}
    height={1080}
  />
);
`;
  fs.writeFileSync(INDEX_FILE, content, "utf8");
}

export function render(name: string): string {
  ensureDirs();
  const outFile = path.join(OUT_DIR, `${name}.mp4`);
  console.log("🎬 Rendering...");
  execSync(
    `npx remotion render src/index.ts Scene ${outFile} --log=error`,
    { stdio: "inherit" }
  );
  return outFile;
}

export function slug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40) || "scene";
}
