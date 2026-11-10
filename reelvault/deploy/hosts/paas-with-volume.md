# ReelVault on a platform with a volume (Fly.io, Railway, Render)

Use this when a helper prefers a platform over a server. You get one service that runs the same Docker image.
The platform gives you HTTPS, so there is no Caddy. The image's default command (`reelvault-start`) runs the web
part and the worker in the same container and restarts either if it stops.

**It must have all of this** (from "Host must have" in [../DEPLOY.md](../DEPLOY.md)):
- an **always-on** plan: no sleeping, no scale to zero, at least one machine running at all times;
- a **volume** (local block storage) mounted at `/data`, 80 GB or more. Never a network file share;
- 2 CPUs / 4 GB memory or more;
- request bodies of 100 MB or more (video uploads are up to 50 MB);
- your own domain on it, with HTTPS.

## Settings for every platform

| setting | value |
|---|---|
| Dockerfile | `deploy/Dockerfile`, build context = the `reelvault` folder (the one with `pyproject.toml`) |
| Port | `8000` (HTTP inside; the platform adds HTTPS) |
| Volume | mounted at `/data` |
| Health check | `GET /healthz` |
| Instances | exactly **1** (SQLite lives on that one volume) |

**The volume must belong to the app's user.** The image runs as user `reelvault` (uid 10001). Fly.io, Railway and
Render mount a new volume owned by root, and then ReelVault stops at start with "ReelVault cannot write to /data".
The fix is one setting per platform (below): the container starts as root, gives `/data` to uid 10001 once, and
then runs the app as uid 10001. The app itself never runs as root.

Environment variables (put the secret ones in the platform's "secrets"):

| name | value |
|---|---|
| `PUBLIC_URL` | `https://app.yourdomain.com` |
| `RV_MASTER_KEYS` | make it on any computer with Python: `python3 -c "import os,base64;print('k1:'+base64.b64encode(os.urandom(32)).decode())"`. **Secret.** Keep a copy in your password manager. |
| `ADMIN_EMAIL` | your email |
| `ADMIN_PASSWORD` | a long random password. **Secret.** Change it after the first sign-in. |
| `TRUSTED_PROXIES` | the platform's proxy addresses (see below) |
| `SKOOL_HOOK_SECRET` | optional, for Zapier. **Secret.** |
| `RESEND_API_KEY` or `SMTP_URL` | optional, for email. **Secret.** |

**`TRUSTED_PROXIES`.** ReelVault limits sign-in tries per visitor, so it must know the visitor's real address. It
trusts the `X-Forwarded-For` header only from the addresses you list. On these platforms the app can only be
reached through the platform's own proxy, which connects from a private address. So list the private ranges:
```
TRUSTED_PROXIES=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7
```
If your platform documents an exact proxy range, use that instead. Never put `0.0.0.0/0` here.

## Fly.io

Fly Volumes are local NVMe disks on the machine, which is what SQLite needs.

1. Install `flyctl` ([fly.io/docs/flyctl/install](https://fly.io/docs/flyctl/install/)) and run `fly auth login`.
2. In the `reelvault` folder, make a file `fly.toml`:
   ```toml
   app = "reelvault-yourname"
   primary_region = "fra"

   [build]
     dockerfile = "deploy/Dockerfile"

   [build.args]
     RV_RUN_USER = "root"   # the Fly volume is root-owned: start as root, give /data to uid 10001, drop to it

   [env]
     PUBLIC_URL = "https://app.yourdomain.com"
     TRUSTED_PROXIES = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7"

   [http_service]
     internal_port = 8000
     force_https = true
     auto_stop_machines = "off"
     auto_start_machines = true
     min_machines_running = 1

     [[http_service.checks]]
       grace_period = "60s"
       interval = "30s"
       method = "GET"
       path = "/healthz"
       timeout = "5s"

   [[mounts]]
     source = "rvdata"
     destination = "/data"

   [[vm]]
     size = "performance-2x"
     memory = "4gb"
   ```
3. Run:
   ```
   fly apps create reelvault-yourname
   fly volumes create rvdata --size 80 --region fra
   fly secrets set RV_MASTER_KEYS="k1:..." ADMIN_EMAIL="you@yourdomain.com" ADMIN_PASSWORD="..."
   fly deploy
   fly certs add app.yourdomain.com
   ```
   `fly certs add` shows the DNS record to add at your domain company.
4. Keep it at one machine: `fly scale count 1`.

## Railway

1. New project > **Deploy from GitHub repo** > pick the repository.
2. Service settings: **Root directory** `reelvault`, **Dockerfile path** `deploy/Dockerfile`.
3. **Add a volume** to the service, mount path `/data`, 80 GB or more.
4. Variables: the table above, plus `RAILWAY_RUN_UID=0` (Railway's setting for images that run as a non-root
   user: the container starts as root, gives the volume to uid 10001, then drops to it). Railway runs the service
   always on (do not turn on "serverless"/sleep).
5. Settings > Networking: **Generate domain** to test, then **Custom domain** for `app.yourdomain.com`.
6. Keep **1 replica** (a volume belongs to one service).

## Render

1. New > **Web Service** > connect the repository. Pick a **paid** instance type (the free type sleeps).
2. **Root directory** `reelvault`, **Runtime** Docker, **Dockerfile path** `deploy/Dockerfile`.
3. **Advanced > Add disk:** mount path `/data`, 80 GB or more.
4. Environment: the table above, plus `RV_RUN_USER=root` (Render passes environment variables to the Docker build
   as build arguments; the container then starts as root, gives the disk to uid 10001 and drops to it).
   **Health check path** `/healthz`.
5. Settings > Custom domains: add `app.yourdomain.com` and follow the DNS steps.
6. Keep 1 instance (Render disks attach to one instance).

## After it runs

- Sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`, then follow "After the first sign-in" in [../DEPLOY.md](../DEPLOY.md).
- **Backups** are made daily inside `/data/backups`. Use the platform's volume snapshots too (Fly: `fly volumes
  snapshots`, Railway and Render have volume backups in the dashboard).
- **Restore a database backup.** The platform's shell runs inside the live app, so don't restore from the shell
  (ReelVault refuses: the running app would keep writing to the old file). Do this instead:
  1. See the backups: open a shell in the service and run
     `setpriv --reuid=10001 --regid=10001 --clear-groups -- python -m reelvault.server.storage list`.
  2. In the platform's settings, add the variable `RV_RESTORE_FROM` with the file name, for example
     `db-2026-11-09.sqlite` (Fly: `fly secrets set RV_RESTORE_FROM=db-2026-11-09.sqlite`).
  3. Restart or redeploy. ReelVault restores the file before it starts. The old database is kept as
     `before-restore-<time>.sqlite`.
  4. Remove `RV_RESTORE_FROM` again (Fly: `fly secrets unset RV_RESTORE_FROM`).
- **Commands in the platform's shell** run as root. Start them with
  `setpriv --reuid=10001 --regid=10001 --clear-groups --` so new files belong to the app. ReelVault's own commands
  (`storage`, `security`, `bench`) switch to the app user by themselves. Files a root shell made anyway are given
  back to the app at the next restart.
- **Measure** with `python -m reelvault.server.bench` from the platform's shell (Fly: `fly ssh console`).
- **Updates:** push to GitHub (Railway, Render) or run `fly deploy` again. Migrations run by themselves.
