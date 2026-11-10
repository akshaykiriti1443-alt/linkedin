# ReelVault on a VPS (the easiest path)

You rent a small Linux server, point your address at it, and run one command. Caddy (included) gets the HTTPS
certificate by itself. This guide uses **Hetzner Cloud** as the example. DigitalOcean, Vultr and Linode work the
same way: pick Ubuntu 24.04 and a size from the table, and use the server's IP address in step 2.

| members | server | example (Hetzner) |
|---|---|---|
| up to 10 | 2 CPUs, 4 GB memory, 80 GB local SSD | CX32 or CPX21 + more disk |
| 20 to 30 | 4 CPUs, 8 GB memory, 160 GB local SSD | CPX31 / CX42 |
| more | double it, or add a block volume for space | CPX41 |

Type names change over time: pick by CPUs, memory and disk. Pick a type with a **local SSD** (Hetzner's shared
and dedicated cloud servers have one). Do not use a network file share for the data.

## 1. Make the server

1. Go to [console.hetzner.cloud](https://console.hetzner.cloud), sign up, and make a project.
2. Click **Add Server**.
3. **Location:** close to you and your members (for example Falkenstein or Ashburn).
4. **Image:** Ubuntu 24.04.
5. **Type:** see the table.
6. **SSH key:** if you have one, add it. If not, skip it: Hetzner emails you a root password.
7. Click **Create & Buy now**. Copy the server's **IPv4 address** (for example `203.0.113.10`).

## 2. Point your address at the server

At the company where you bought your domain (GoDaddy, Namecheap, Cloudflare, ...), open the DNS settings and add:

| type | name | value |
|---|---|---|
| A | `app` | the server's IPv4 address |

That makes `app.yourdomain.com`. If you use Cloudflare DNS, set the cloud to **DNS only** (grey) for this record
during setup, so Caddy can get the certificate. Changes can take 5 to 30 minutes.

## 3. Sign in to the server

Open Terminal (Mac) or PowerShell (Windows) and type (use your IP):
```
ssh root@203.0.113.10
```
Type `yes` the first time. Paste the root password if it asks (nothing shows while you type; that's normal).

## 4. Lock the doors and download ReelVault

Paste these lines one by one and press Enter after each:
```
apt-get update && apt-get install -y git curl openssl ufw
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
git clone https://github.com/YOUR-GITHUB-NAME/YOUR-REPO.git /root/ReelVault
cd /root/ReelVault/reelvault
```
(Use the address of the GitHub repository with ReelVault. If it is private, GitHub shows you how to sign in.)

## 5. Run the setup

```
./deploy/setup.sh
```
It asks two things:
- **Your ReelVault address**, for example `app.scaleitads.com` (no `https://`).
- **Admin email**, the email you will sign in with.

Then it installs Docker if needed, makes all keys, writes `deploy/.env`, builds ReelVault (10 to 20 minutes the first
time, it downloads the AI models), starts it, and gets the HTTPS certificate. At the end it shows:
```
ReelVault is ready.
Open https://app.scaleitads.com and sign in.
  Email:    you@scaleitads.com
  Password: ********************   (shown once; change it after you sign in)
```
Write the password down now. Then save `deploy/.env` in your password manager:
```
cat /root/ReelVault/reelvault/deploy/.env
```
Copy everything it shows into a secure note called "ReelVault server". Losing it means members re-enter their AI keys.

## 6. Sign in and invite members

Open your address in Chrome and sign in. Then follow "After the first sign-in" in [../DEPLOY.md](../DEPLOY.md).

## Later

- Update: `cd /root/ReelVault/reelvault && ./deploy/setup.sh update`
- Check: `./deploy/setup.sh status`
- Watch the logs: `./deploy/setup.sh logs`
- Backups and restore: see "Backups" in [../DEPLOY.md](../DEPLOY.md).
- Two containers instead of one (web and worker apart): add `COMPOSE_FILE=docker-compose.yml:docker-compose.split.yml`
  to `deploy/.env` (replace the old `COMPOSE_FILE` line), then `./deploy/setup.sh update`.

## If the setup stops

| message | what to do |
|---|---|
| `does not resolve yet` | The A record is missing or not live yet. Check step 2, wait, run `./deploy/setup.sh` again. |
| `does not answer yet` (HTTPS) | Check ports 80 and 443 are open (`ufw status`) and the A record points here. Caddy keeps trying. |
| `Docker is installed but not running` | Run `systemctl start docker`, then the setup again. |
| `The app did not start` | The last log lines are shown. Send them to your developer. Nothing is lost; run the setup again after a fix. |
