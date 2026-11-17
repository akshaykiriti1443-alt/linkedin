# ReelVault inside OneX (instructions for both sides)

Goal: ReelVault's screens show **inside the OneX app**, at a OneX address (`/app/w/<wid>/reelvault/...`), with
OneX's sidebar and header around them. No second login, no second navigation, the same look. ReelVault keeps
running as its own server; the merge is an embedded frame plus a signed server-to-server API.

- The embedded page: this file.
- The findings API (OneX's Ad studio reads the member's proven hooks and formats):
  [`docs/onex-findings-api.md`](docs/onex-findings-api.md).
- Decision log: `DESIGN-SAAS.md` D86.

## Settings

| Side | Variable | On this computer | Hosted |
|---|---|---|---|
| ReelVault | `ONEX_SSO_SECRET` | `%USERPROFILE%\ReelVault-Members\onex-sso-secret.txt` (made by the installer) | a NEW long random value |
| ReelVault | `ONEX_URL` | `http://localhost:3000` (the default) | `https://app.scaleitads.com` |
| ReelVault | `RV_LOCAL_MEMBERS` | `1` (set by `members_local.py`) | never set |
| ReelVault | `PUBLIC_URL` | `http://localhost:8800` | `https://reelvault.scaleitads.com` |
| OneX | `REELVAULT_URL` | `http://localhost:8800` | `https://reelvault.scaleitads.com` |
| OneX | `ONEX_SSO_SECRET` | the same value as ReelVault's | the same new value |
| OneX | `APP_URL` | unset (`http://localhost:3000`) | `https://app.scaleitads.com` (= ReelVault's `ONEX_URL`) |

The secret stays on the servers: never in client code, never in Git (this repo is public).

## Who may frame ReelVault

ReelVault sends `Content-Security-Policy: ... frame-ancestors <list>` on exactly two HTML paths, `/` and `/sso/onex`:

| Mode | frame-ancestors | X-Frame-Options |
|---|---|---|
| Local members (`RV_LOCAL_MEMBERS=1` AND `PUBLIC_URL` is `http://localhost` or `http://127.0.0.1`) | `ONEX_URL`'s origin (if valid), `http://localhost:*`, `http://127.0.0.1:*` | none |
| Hosted with a valid `ONEX_URL` | exactly `ONEX_URL`'s origin | none |
| Hosted without a valid `ONEX_URL` | `'none'` | `DENY` |
| Any other HTML page (OAuth consent, print view, ...) | `'none'` | `DENY` |

A valid `ONEX_URL` is a bare origin: `https://host[:port]`, or `http://` only for `localhost` / `127.0.0.1`; no
user, path, query or fragment. Anything else counts as unset, so framing stays denied. The flag on a hosted (https)
server loosens nothing. ReelVault writes the origin the way a browser does, so `https://app.scaleitads.com:443`
becomes `https://app.scaleitads.com` (what OneX sends as `parent`). An empty port (`host:`), port 0 or a trailing dot
counts as invalid.

Local members mode allows any `http://localhost:<port>` and `http://127.0.0.1:<port>` to frame ReelVault on purpose,
so OneX works on any dev port. The cost: any page served from this computer could frame ReelVault with the member's
session. That is acceptable only on the owner's own PC, which is why a hosted (https) server never gets it.

## Cookies

`rv_session` stays `HttpOnly; SameSite=Lax; Path=/`, `Secure` when hosted, no `Domain`. It works inside the frame
because the frame and OneX are **same-site**: `localhost:3000` and `localhost:8800` differ only by port, and
`app.scaleitads.com` and `reelvault.scaleitads.com` share `scaleitads.com`. Use the same host name on both sides in
development: `localhost` with `localhost`. `127.0.0.1` with `localhost` is cross-site and the cookie is dropped
(OneX shows a hint instead of the frame). Cookies ignore ports, so on one computer the frame and a standalone
ReelVault tab share one sign-in.

## The flow

1. OneX's page `/app/w/<wid>/reelvault[/<route>]` mounts an iframe with
   `src="/api/reelvault/open?embed=1&theme=<light|dark>&to=<route>"` (a OneX route, same origin as the page).
2. That OneX route checks the OneX session and a confirmed email, signs the usual v1 ticket and answers
   `307 REELVAULT_URL/sso/onex?t=<ticket>&embed=1&parent=<OneX origin>&theme=<t>&to=<route>`.
   In embed mode its failures are plain text (`401 Sign in to OneX again.` / `403 ReelVault is not available.`),
   never a redirect to a OneX page (that would put OneX inside its own frame).
3. ReelVault's `GET /sso/onex` checks the ticket as before (HMAC-SHA256, at most 10 minutes ahead, single-use
   nonce counted in the ticket's own expiry day), then the extra parameters:
   - `embed`: only `1` counts.
   - `theme`: only `light` or `dark`.
   - `to`: must fully match ROUTE_RE (below) and be at most 300 characters, else `home`.
   - `parent`: must equal `ONEX_URL`'s origin, or (local members mode only) match
     `^http://(localhost|127\.0\.0\.1):[0-9]{1,5}$`. Otherwise it becomes `ONEX_URL`'s origin; if there is none,
     embed is dropped and the sign-in goes on as a normal page.
   - `t` missing or empty: the `bad_link` error page (with `rv:error` inside OneX), never a JSON error.
4. It answers `303` to a Location built only from checked parts, so it can't be an open redirect:
   - no embed, no `to`: `/`
   - no embed, with `to`: `/#<to>`
   - embed: `/?embed=1&parent=<origin>[&theme=<t>]#<to or home>`,
     e.g. `/?embed=1&parent=http%3A%2F%2Flocalhost%3A3000&theme=dark#reel/rl_abcdefghijklmnop`
5. If the browser already has a valid `rv_session` for the same active member, ReelVault keeps it (no new
   session, no `Set-Cookie`); the ticket is still used up and the login is still counted.
6. In the frame, `web/static/theme.js` (sync, before paint) turns embed mode on only when the page **is framed**
   and has `?embed=1&parent=<origin>` (or this tab saw that parent before: `sessionStorage["rv.embed"]`). It sets
   `<html data-embed="1" data-parent="<origin>">`, applies `?theme`, and never touches `localStorage["rv.theme"]`.
   A page opened directly (not framed) is never in embed mode. Only the app page (`/`) reads `parent` from its
   address (that address comes from the checked `/sso/onex` redirect); the framed `/sso/onex` error page takes only
   the theme and stores no parent. Where the browser reports the real parent (`location.ancestorOrigins`, in Chrome
   and Safari), `parent` must equal it, or embed stays off.

ROUTE_RE (the same in `server/api_auth.py`, `web/static/embed.js` and OneX's `src/lib/reelvaultRoutes.ts`):
```
^(home|onboarding|competitors|competitor|discover|reels|reel|working|format|banks|create|outputs|output|settings|help|privacy)(/[A-Za-z0-9_-]{1,64}){0,2}(\?[A-Za-z0-9_.~%+=&-]{1,200})?$
```
Left out on purpose: `admin`, `login`, `join`, `reset`, `verify`. The owner uses standalone http://localhost:8800 for Admin.

Deep links: OneX `/app/w/<wid>/reelvault/<name>[/<a>[/<b>]][?<q>]` = ReelVault `#<name>[/<a>[/<b>]][?<q>]`;
`/app/w/<wid>/reelvault` = `#home`.

## What embed mode changes in ReelVault

- Hidden: the whole left nav (brand, links, Admin, email, Help, Privacy, Log out, the job line), the toast
  (toasts go to OneX), and in Settings > Account the Look picker, the password block and Delete account. The
  re-confirm screen has no Log out. OneX members have a random ReelVault password.
- Layout: no `.app` grid, `.main` has no padding (OneX's container gives it), no viewport-height sizes,
  `html{overflow:hidden}`: the frame grows to its content and OneX's page scrolls.
- Running jobs: a small pill "Working on N jobs" at the top right, linking to `#home`.
- Signed out (a 401, or a page that needs sign-in): never the login screen. The frame shows "Signing you in…"
  and posts `rv:signed-out` once per page load; OneX signs a fresh ticket.
- The look is OneX's everywhere (embedded and standalone): OneX's colour tokens, Geist / Geist Mono /
  Instrument Serif (self-hosted in `web/static/fonts/`, SIL OFL 1.1), radii and pill buttons, light and dark.

## Messages between the frame and OneX

`window.postMessage` with plain objects `{type, ...}`. Never a ticket, cookie, CSRF token, secret or email.

- The frame sends with `targetOrigin = html[data-parent]` (never `"*"`) and accepts a message only when
  `event.source === window.parent`, `event.origin === data-parent`, it is a plain object and `type` starts with `onex:`.
- OneX sends with `targetOrigin = new URL(REELVAULT_URL).origin` and accepts only `event.origin` = that origin,
  `event.source` = the iframe's window, and `type` starting with `rv:`. Unknown types are ignored.

Frame to OneX:

| type | fields | when |
|---|---|---|
| `rv:ready` | `route`, `title` (h1, max 120) | once per page load, after the first page is drawn |
| `rv:route` | `route`, `title` | after every later page (hash change) |
| `rv:height` | `height` (integer 0..100000) | ResizeObserver on `body`, at most once per frame, and after each page |
| `rv:toast` | `text` (max 300) | instead of ReelVault's own toast |
| `rv:signed-out` | | no ReelVault session (once per page load) |
| `rv:error` | `code`, `message` (max 300) | the `/sso/onex` error page (`web/static/embed-error.js`) |

`rv:error` codes: `not_set_up` (503), `bad_link` (400), `expired` (400), `used` (400), `turned_off` (403), `too_many` (429).

OneX to the frame:

| type | fields | effect |
|---|---|---|
| `onex:theme` | `theme`: `light` or `dark` | sets `html[data-theme]` (never localStorage) |
| `onex:navigate` | `route` (ROUTE_RE) | `location.replace("#" + route)`: no new history entry |

Back button: clicks inside the frame push frame history entries (part of the browser's joint history); OneX mirrors
each `rv:route` with `history.replaceState`. OneX sidebar clicks and Back/Forward change OneX's URL; OneX then sends
`onex:navigate` and the frame replaces its hash.

## Test it on this computer

1. Close ReelVault Members if it runs (the launcher stops early when port 8800 is busy).
2. Reinstall it from this folder (PowerShell):
   `powershell -ExecutionPolicy Bypass -File "<repo>\reelvault\install-members.ps1" -Source "<repo>\reelvault"`,
   then start it from the **ReelVault Members** desktop icon.
3. Start OneX (`npm run dev` in its `platform` folder) and open it at **http://localhost:3000** (not 127.0.0.1).
4. Log in with a confirmed email and click **ReelVault** in the sidebar. ReelVault opens inside OneX, signed in.

If the frame says ReelVault and OneX aren't connected, the two `ONEX_SSO_SECRET` values differ. If ReelVault says
sign-in is not set up, run the ReelVault Members installer again (it creates `onex-sso-secret.txt`).

## Hosted later

- ReelVault server `.env`: `PUBLIC_URL=https://reelvault.scaleitads.com`, `ONEX_URL=https://app.scaleitads.com`,
  `ONEX_SSO_SECRET=<new long random value>`. Do NOT set `RV_LOCAL_MEMBERS`.
- Vercel (OneX): `REELVAULT_URL=https://reelvault.scaleitads.com`, the same new `ONEX_SSO_SECRET`,
  `APP_URL=https://app.scaleitads.com`.
- Agency white-label domains of OneX are not same-site with ReelVault: OneX shows an "Open ReelVault" button there
  (a new tab) instead of the frame.

## Known limit (not fixed yet)

A sign-in ticket is not tied to one browser. A OneX member can copy their own 2-minute `/sso/onex?t=...` link and
send it to someone else. If that person opens it in time, ReelVault signs them in to the sender's account and
replaces their own session. They could then type their own AI key into the wrong account. This was already true
before the embed work. The fix needs a change on both sides: OneX sets a short same-site cookie when it signs a
ticket, and `/sso/onex` checks that the ticket belongs to that cookie. It is left for a later ticket version (v2).
