#!/usr/bin/env tsx
import fs from "fs";
import path from "path";
import readline from "readline";
import { execSync } from "child_process";
import { generateComposition, Message } from "./claude.js";

const COMPOSITIONS_DIR = path.resolve("src/compositions");
const INDEX_FILE = path.resolve("src/index.ts");
const OUT_DIR = path.resolve("out");

function ensureDirs() {
  fs.mkdirSync(COMPOSITIONS_DIR, { recursive: true });
  fs.mkdirSync(OUT_DIR, { recursive: true });
}

function writeComposition(code: string, name: string): string {
  const file = path.join(COMPOSITIONS_DIR, `${name}.tsx`);
  fs.writeFileSync(file, code, "utf8");
  return file;
}

function writeIndex(name: string, durationInFrames: number = 150, fps: number = 30) {
  const content = `import { Composition } from 'remotion';
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

function render(name: string): string {
  const outFile = path.join(OUT_DIR, `${name}.mp4`);
  console.log("\n🎬 Rendering...");
  execSync(
    `npx remotion render src/index.ts Scene ${outFile} --log=error`,
    { stdio: "inherit" }
  );
  return outFile;
}

function slug(prompt: string): string {
  return prompt
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40) || "scene";
}

async function main() {
  ensureDirs();

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const ask = (q: string) => new Promise<string>((res) => rl.question(q, res));

  const history: Message[] = [];
  let currentName = "";

  console.log("🎨 Motion Graphics Bot — powered by Claude + Remotion");
  console.log("Commands: 'render' to render current scene, 'exit' to quit\n");

  while (true) {
    const input = (await ask("You > ")).trim();
    if (!input) continue;

    if (input.toLowerCase() === "exit") break;

    if (input.toLowerCase() === "render") {
      if (!currentName) {
        console.log("No scene generated yet. Describe a motion graphic first.\n");
        continue;
      }
      try {
        const out = render(currentName);
        console.log(`✅ Rendered → ${out}\n`);
      } catch (e) {
        console.error("Render failed:", e);
      }
      continue;
    }

    console.log("⏳ Generating...");
    try {
      const code = await generateComposition(history, input);

      // Store in conversation history
      history.push({ role: "user", content: input });
      history.push({ role: "assistant", content: code });

      currentName = slug(input);
      const file = writeComposition(code, currentName);
      writeIndex(currentName);

      console.log(`✅ Composition written → ${file}`);
      console.log('Type "render" to render it, or describe changes to iterate.\n');
    } catch (e) {
      console.error("Error:", e);
    }
  }

  rl.close();
}

main();
