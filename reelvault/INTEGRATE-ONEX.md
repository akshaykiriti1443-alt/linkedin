# Add ReelVault to OneX (instructions for Claude Code in the OneX folder)

Goal: a **ReelVault** item in the OneX app. A logged-in OneX member clicks it and lands in ReelVault Members,
already signed in. No second sign-up. ReelVault keeps running as its own app; OneX only adds a menu item and one
small server route that signs a short ticket.

## How it works
1. The member clicks **ReelVault** in OneX -> the browser opens `/api/reelvault/open` on OneX.
2. That OneX server route checks the member is logged in to OneX, then signs a ticket:
   `v1.<base64url(JSON)>.<base64url(HMAC-SHA256(secret, "v1." + base64url(JSON)))>` with JSON
   `{ "email", "name", "exp": now + 120 s, "nonce": 16 random bytes hex }`.
3. It redirects to `REELVAULT_URL/sso/onex?t=<ticket>`.
4. ReelVault checks the signature, the expiry (max 10 minutes ahead) and that the nonce is new, creates the member
   if needed (active, no approval queue), signs them in and opens its home page.

The shared secret is `ONEX_SSO_SECRET`. Same value on both sides. It must stay on the server: never in client code,
never in Git.

## Settings for OneX (`.env.local` for local dev; Vercel env vars later)
```
REELVAULT_URL=http://localhost:8800
ONEX_SSO_SECRET=<paste the value from %USERPROFILE%\ReelVault-Members\onex-sso-secret.txt>
```
Make sure `.env.local` is in `.gitignore`.

## Code to add (Next.js)

### 1. Ticket helper, server-only: `lib/reelvault.ts` (or `.js`)
```ts
import "server-only"; // remove this line if the project doesn't use the server-only package
import crypto from "node:crypto";

export function makeReelVaultTicket(user: { email: string; name?: string | null }): string {
  const secret = process.env.ONEX_SSO_SECRET;
  if (!secret || secret.length < 32) throw new Error("ONEX_SSO_SECRET is missing or too short");
  const payload = Buffer.from(JSON.stringify({
    email: user.email.trim().toLowerCase(),
    name: user.name ?? "",
    exp: Math.floor(Date.now() / 1000) + 120,
    nonce: crypto.randomBytes(16).toString("hex"),
  })).toString("base64url");
  const sig = crypto.createHmac("sha256", secret).update(`v1.${payload}`).digest("base64url");
  return `v1.${payload}.${sig}`;
}

export function reelVaultSignInUrl(user: { email: string; name?: string | null }): string {
  const base = (process.env.REELVAULT_URL || "http://localhost:8800").replace(/\/$/, "");
  return `${base}/sso/onex?t=${encodeURIComponent(makeReelVaultTicket(user))}`;
}
```
(This exact ticket code was tested against ReelVault's `/sso/onex`.)

### 2. Route that signs and redirects
App Router: `app/api/reelvault/open/route.ts`
```ts
import { NextResponse } from "next/server";
import { reelVaultSignInUrl } from "@/lib/reelvault";

export const runtime = "nodejs"; // node:crypto is needed

export async function GET(request: Request) {
  const user = await getCurrentOneXUser(); // <- use OneX's existing session helper (find it in this codebase)
  if (!user?.email) return NextResponse.redirect(new URL("/login", request.url));
  // Optional: only Skool members, if OneX tracks that (e.g. user.isMember). Otherwise every OneX user gets in.
  return NextResponse.redirect(reelVaultSignInUrl({ email: user.email, name: user.name }));
}
```
Pages Router instead: `pages/api/reelvault/open.ts` with the same logic, `res.redirect(307, url)`.

Replace `getCurrentOneXUser()` with whatever OneX already uses to read the logged-in user on the server
(for example its auth library's session function). Do not invent a new login system.

### 3. Menu item
Add **ReelVault** to OneX's main navigation, linking to `/api/reelvault/open`. Open it in a new tab
(`target="_blank" rel="noopener"`): ReelVault is a separate app. Use plain copy, e.g. a short line under it:
"Study your competitors' reels and turn the winners into your scripts."

## Test it on this computer
1. Start ReelVault Members (desktop icon **ReelVault Members**; it runs at http://localhost:8800).
2. Start OneX (`npm run dev`, usually http://localhost:3000) and log in.
3. Click **ReelVault**. You should land on ReelVault's home page, signed in as the same email.
4. Click it again later: it signs in again (every click makes a fresh ticket; a used ticket never works twice).

If ReelVault shows "Sign-in from OneX is not set up yet", ReelVault is missing its secret: run the ReelVault
Members installer again (it creates `onex-sso-secret.txt`). If it says the link is not valid, the two secrets differ.

## Later, when hosted
- ReelVault runs on its own small server at a subdomain such as `https://reelvault.scaleitads.com`
  (see ReelVault's `deploy/DEPLOY.md`), with `ONEX_SSO_SECRET` and `ONEX_URL=https://app.scaleitads.com` in its `.env`.
- In Vercel (OneX project) set `REELVAULT_URL=https://reelvault.scaleitads.com` and the same `ONEX_SSO_SECRET`.
- Use a NEW long random secret for production (not the local one).
