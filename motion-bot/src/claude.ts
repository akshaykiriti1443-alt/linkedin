import Anthropic from "@anthropic-ai/sdk";
import { SYSTEM_PROMPT } from "./system-prompt.js";

const client = new Anthropic();

export interface Message {
  role: "user" | "assistant";
  content: string;
}

export async function generateComposition(
  history: Message[],
  userPrompt: string
): Promise<string> {
  const messages: Message[] = [...history, { role: "user", content: userPrompt }];

  const response = await client.messages.create({
    model: "claude-opus-4-8",
    max_tokens: 4096,
    system: SYSTEM_PROMPT,
    messages,
  });

  const text = response.content
    .filter((b) => b.type === "text")
    .map((b) => (b as { type: "text"; text: string }).text)
    .join("");

  return text.trim();
}
