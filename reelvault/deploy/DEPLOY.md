# Put ReelVault Members online

This guide is for the owner. You don't need to code. You copy a few lines, paste them, and press Enter.
Plan about one hour the first time. Most of it is waiting.

> **Host must have** (check this before you pay for any server)
>
> | must have | why |
> |---|---|
> | An always-on server. No "scale to zero", no sleeping, no free plan that pauses. | Jobs run in the background. The Claude connector must answer at any time. |
> | A **local** disk (local SSD or block volume) for the data, 80 GB or more. | The database and member files live there. Network drives (NFS, SMB, Azure Files, EFS, a NAS share) break the database, and ReelVault refuses to start on them. |
> | 2 CPUs and 4 GB memory or more. 4 CPUs and 8 GB for about 20 members or more. | Transcription and sound analysis run on the CPU. |
> | A public HTTPS address on your own domain, that accepts uploads of 100 MB. | Members' browsers upload videos (up to 50 MB). Claude's cloud must reach `/mcp`. |
> | Internet access out to AI providers and Instagram's video servers. | Analysis and video downloads. |

**Good fits:** a small VPS with Docker, for example Hetzner Cloud, DigitalOcean, Vultr or Linode
(guide: [hosts/vps.md](hosts/vps.md), the easiest path). Or a platform with an attached volume and an always-on
plan: Fly.io with a volume, Railway with a volume, Render paid web service with a disk
(guide: [hosts/paas-with-volume.md](hosts/paas-with-volume.md)).

**Does not fit:** Google Cloud Run, AWS Lambda or App Runner, any free or sleeping plan (Render free, Heroku eco),
Vercel, Netlify, Azure Container Apps or ECS Fargate with network file storage, a home NAS share for the data.

---

## What you need before you start

1. **A domain** you control, for example `scaleitads.com`. You will make an address like `app.scaleitads.com`.
2. **A server** from the box above. For 10 members: 2 CPUs, 4 GB memory, 80 GB disk (about 8 to 15 euros a month).
   For 20 to 30 members: 4 CPUs, 8 GB.
3. **A terminal** on your computer. Mac: open the app **Terminal**. Windows: open **PowerShell**. Both have `ssh`.
4. **A password manager** to keep one file safe (`deploy/.env`, step 5 of the VPS guide).

## The steps, short version

1. Point your address at the server (an "A record" at your domain company).
2. Sign in to the server with `ssh`.
3. Download ReelVault and run `./deploy/setup.sh`. It asks for the address and your email, then does the rest.
4. Open your address, sign in as admin, and copy the community code from the banner.
5. Post the signup link and the code in a Skool Classroom lesson that only members can see.

The full click-by-click guide is [hosts/vps.md](hosts/vps.md).

---

## After the first sign-in

1. **Change your password.** The setup showed it once. Go to Settings > Account > Change password.
2. **Post the code.** The admin page shows a banner with the community code and the signup link.
   Put both in a Skool Classroom lesson that is hidden from logged-out visitors. Press **I posted it**.
   The code changes by itself every 30 days. The old one keeps working for 2 more days. You see the new one at sign-in.
3. **Approve people.** New signups with the code wait for you. The admin menu shows a number when someone waits.
   Open Admin > Members, check their Skool profile link, and press **Approve** or **Reject**.
   People on your allowlist, or with a single-use invite, get in at once.
4. **Turn off people who left.** Admin > Members > **Sync members**. Upload the member export from Skool (CSV).
   ReelVault lists the accounts that are no longer in your group. Tick them and press **Turn off**.

## The Chrome extension

Members read Instagram through their own Chrome with the ReelVault extension.

- **Chrome Web Store (main path).** Do this in week 1, because the review can take days.
  On the server, in the ReelVault folder, run:
  ```
  python3 deploy/build_extension.py --server https://app.scaleitads.com
  ```
  It writes `dist/reelvault-members-extension-2.0.0-store.zip`. Copy it to your computer
  (`scp root@YOUR_SERVER_IP:/root/ReelVault/reelvault/dist/*.zip .`), open the
  [Chrome Web Store developer dashboard](https://chrome.google.com/webstore/devconsole), add a new item, upload the
  ZIP, and choose **Unlisted**. The privacy policy address is `https://app.scaleitads.com/#privacy`.
- **ZIP for you and beta testers (until the store says yes).** Members download it from
  `https://app.scaleitads.com/download/reelvault-members-extension.zip`, extract it, open `chrome://extensions`,
  turn on **Developer mode**, press **Load unpacked**, and pick the extracted folder.

## Email (optional)

Without email, everything works. You just tell people yourself when they are approved, and you make password reset
links in Admin > Members. With email, ReelVault sends: a note to you for each new signup, "You're in" when you
approve someone, and password reset links people ask for.

Pick one and put it in `deploy/.env`, then restart (`./deploy/setup.sh update`):
- `RESEND_API_KEY=re_...` from [resend.com](https://resend.com) (free plan is enough), and
  `MAIL_FROM=ReelVault <no-reply@scaleitads.com>` on a domain you verified there.
- or `SMTP_URL=smtp://USER:PASSWORD@smtp.yourmail.com:587` from your email provider.

## Zapier (optional)

If you use Zapier with Skool, ReelVault can add new Skool members to the allowlist by itself.
The secret is already in `deploy/.env` (`SKOOL_HOOK_SECRET`). In Zapier, make a "Webhooks by Zapier" POST action:
- URL `https://app.scaleitads.com/api/hooks/skool-allowlist`, header `X-Hook-Secret: <the secret>`,
  JSON body `{"email": "<member email>"}` for "new member".
- URL `https://app.scaleitads.com/api/hooks/skool-removed` with the same header for "member left", if Skool offers it.

## Backups

ReelVault backs itself up every day: the database (last 7 days) and member files without videos (last 4 weeks).
They are on the server in the data volume. Admin > Health shows the last backup. It turns red after 36 hours.

On the server, in the ReelVault folder:
```
./deploy/backup.sh                     # make a backup now
./deploy/backup.sh list                # see the backups
./deploy/backup.sh copy-out /root/rv-backups     # copy them out of the container
./deploy/backup.sh restore db-2026-11-09.sqlite  # put an older database back (the app stops for a minute)
```
A restore keeps the current database as `before-restore-<time>.sqlite`, so you can undo it.
**Try one restore before members join**, so you know it works.

**Copy backups off the server.** A server can die. Once a night, copy the backups somewhere else. The simplest way:
install [rclone](https://rclone.org/install/) on the server, connect it to any storage (Backblaze B2, Cloudflare R2,
Google Drive, ...) with `rclone config`, then add this line with `crontab -e`:
```
15 4 * * *  cd /root/ReelVault/reelvault && ./deploy/backup.sh copy-out /root/rv-backups && rclone copy /root/rv-backups remote:reelvault
```

## Move to a new server, or recover a dead one

You need two things you saved earlier: the file `deploy/.env` (in your password manager) and your off-site
backups (`db-....sqlite` and `users-....tar.zst`). Without that `deploy/.env`, every member must paste their AI key
again, because the master key in it locks those keys.
1. On the new server, get the code again (the same `git clone` as in the steps above) and go into the `reelvault`
   folder.
2. Put your saved `deploy/.env` back into the `deploy/` folder **before** step 3. If it is missing, setup makes a
   new master key.
3. Run `./deploy/setup.sh`. It sees `deploy/.env`, keeps your keys and starts ReelVault (empty for now).
4. Copy your backups in: `./deploy/backup.sh copy-in /root/rv-backups/db-2026-11-09.sqlite` and
   `./deploy/backup.sh copy-in /root/rv-backups/users-2026-45.tar.zst` (use your newest names).
5. Put them back: `./deploy/backup.sh restore db-2026-11-09.sqlite`, then
   `./deploy/backup.sh restore-files users-2026-45.tar.zst`.
6. Point your domain's A record at the new server. Members sign in as before. Videos are not in the backups; they
   come back the next time ReelVault needs them.

## Updates

```
cd /root/ReelVault/reelvault && ./deploy/setup.sh update
```
It gets the newest version, makes a backup, rebuilds, and restarts. Database changes run by themselves.

## Is it healthy?

- `./deploy/setup.sh status` shows the containers and checks the address.
- `https://app.scaleitads.com/healthz` shows `"ok": true` and when the worker was last seen.
- Admin > Health shows members, disk space, the work queue, seconds per reel and the last backup.
- `./deploy/setup.sh logs` shows what is happening (Ctrl+C stops watching; the app keeps running).

## Measure your server

Run this once on the new server. It times the media work and prints the two settings to use.
```
cd /root/ReelVault/reelvault/deploy && docker compose exec app python -m reelvault.server.bench
```
For real numbers, give it a real reel with speech:
`docker compose cp reel.mp4 app:/data/tmp/reel.mp4`, then run the same line with `--video /data/tmp/reel.mp4` at the end.
Put the two printed lines in `deploy/.env` and run `./deploy/setup.sh update`.

Rough sizes (measured on a 2-CPU build box): one priority reel about 28 s, one sample reel about 9 s,
one pull of 100 reels about 11 minutes, one member's first setup (10 competitors) about 2 hours.
So 2 CPUs / 4 GB fit about 10 members, 4 CPUs / 8 GB about 20 to 30.

## When something goes wrong

| what you see | what to do |
|---|---|
| Setup says the domain does not answer | Check the A record at your domain company. It must show this server's IP. Wait 10 minutes, then run `./deploy/setup.sh status`. Ports 80 and 443 must be open. |
| "Your data folder is on a network drive" | The data must be on a local disk. Pick a server type with a local SSD or a block volume. |
| The page loads but sign-in fails | Check `PUBLIC_URL` in `deploy/.env` is exactly your address with `https://` and no slash at the end. |
| Members' AI keys stopped working after a move | The master key changed. Put back the old `RV_MASTER_KEYS` from your password manager. |
| Admin > Health shows the worker in red | Run `./deploy/setup.sh logs`. Restart with `cd deploy && docker compose restart app`. (After you change `deploy/.env`, use `./deploy/setup.sh update` instead: `restart` keeps the old settings.) |
| The first `./deploy/setup.sh` stopped during the build | Run `./deploy/setup.sh` again. It keeps `deploy/.env` and shows where to sign in. Your first password is `ADMIN_PASSWORD` in `deploy/.env`. |
| The disk is almost full | Lower `video_keep_days` in Admin > Settings, or make the disk bigger. |

## Move to Postgres later (optional)

SQLite is right for one server. If you ever need more servers:
1. Add `postgres` to `COMPOSE_PROFILES` in `deploy/.env`, set `POSTGRES_PASSWORD` and
   `DATABASE_URL=postgresql+psycopg://reelvault:<that password>@postgres:5432/reelvault`.
2. Copy the data (the app stops while it copies):
   `cd deploy && docker compose stop app && docker compose up -d postgres && docker compose run --rm --no-deps app python -m reelvault.server.db copy-from-sqlite /data/db/reelvault.sqlite`
3. Restart: `./deploy/setup.sh update`. Back up Postgres with
   `docker compose exec postgres pg_dump -U reelvault reelvault > reelvault.sql` (restore with `psql`).

## Try it before you have a domain, or test the Claude connector

Claude connects from Anthropic's cloud, so `localhost` alone won't work. A free Cloudflare tunnel gives you a
temporary public https address:
1. On the server: `./deploy/setup.sh --local` (runs on `http://localhost:8000` without Caddy).
2. Install `cloudflared` ([guide](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/))
   and run `cloudflared tunnel --url http://localhost:8000`. It prints an address like `https://xyz.trycloudflare.com`.
3. Put that address in `deploy/.env` as `PUBLIC_URL`, then `cd deploy && docker compose up -d`.
Cloudflare's free tunnel allows uploads up to 100 MB, so the 50 MB video limit fits.

**When you have your domain:** point its A record at the server, then run `./deploy/setup.sh domain`. It asks for
the domain, switches `deploy/.env` to `https://<your domain>` with Caddy (HTTPS), keeps your keys and data, and
restarts. (`RV_DOMAIN=app.example.com ./deploy/setup.sh` does the same without the question.)

## Keep it safe

- `deploy/.env` holds the master key. Keep a copy in your password manager. Never put it in git, chat or email.
- Let only ports 22, 80 and 443 in (the VPS guide shows how with `ufw`).
- To change the master key (all commands in the `reelvault` folder):
  1. Make a new key: `cd deploy && docker compose exec app python -m reelvault.server.security genkey k2`.
  2. Put it in front of the old one in `RV_MASTER_KEYS` in `deploy/.env`: `RV_MASTER_KEYS=k2:NEW,k1:OLD`.
  3. Load the new setting: `./deploy/setup.sh update` (or `cd deploy && docker compose up -d`). Not
     `docker compose restart`: that keeps the old settings.
  4. Re-encrypt: `cd deploy && docker compose exec app python -m reelvault.server.security rotate`. It says how many
     keys it re-encrypted, and refuses if the new key is not loaded yet.
  5. Only then remove `,k1:OLD` from `deploy/.env` and run `./deploy/setup.sh update` again.
