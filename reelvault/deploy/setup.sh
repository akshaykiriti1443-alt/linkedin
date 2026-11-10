#!/usr/bin/env bash
# ReelVault Members: one-command server setup (DESIGN-SAAS.md 12.5). Run it from the project folder:
#
#   ./deploy/setup.sh            first install: asks 2 questions, writes deploy/.env, starts everything with HTTPS
#   ./deploy/setup.sh update     get the newest version, rebuild, restart (database migrations run by themselves)
#   ./deploy/setup.sh status     is it running and healthy?
#   ./deploy/setup.sh logs       follow the logs (Ctrl+C to stop watching)
#   ./deploy/setup.sh --local    no domain yet: run on http://localhost:8000 without Caddy (tunnel or testing)
#   --no-build                   use the reelvault-members:latest image that is already built (CI)
#
# Non-interactive (scripts, CI):  RV_DOMAIN=app.example.com ADMIN_EMAIL=you@example.com ./deploy/setup.sh --yes
# It never prints the master key. It prints the admin password once, at the end of the first install.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DEPLOY_DIR/.env"
MODE="install"
LOCAL=0
YES=0
NO_BUILD="${RV_NO_BUILD:-0}"
for arg in "$@"; do
  case "$arg" in
    update|status|logs|install) MODE="$arg" ;;
    --local) LOCAL=1 ;;
    --yes|-y) YES=1 ;;
    --no-build) NO_BUILD=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say() { printf '%s\n' "$*"; }
warn() { printf '\033[33mNote:\033[0m %s\n' "$*"; }
die() { printf '\033[31mStopped:\033[0m %s\n' "$*" >&2; exit 1; }

ask() {  # ask VAR "Question" [default]
  local __var="$1" __q="$2" __def="${3:-}" __ans=""
  if [ -n "${!__var:-}" ]; then return; fi
  if [ "$YES" = 1 ]; then printf -v "$__var" '%s' "$__def"; return; fi
  if [ -n "$__def" ]; then read -r -p "$__q [$__def]: " __ans || true; else read -r -p "$__q: " __ans || true; fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

confirm() {  # confirm "Question" -> 0 yes / 1 no (yes when --yes)
  [ "$YES" = 1 ] && return 0
  local a=""; read -r -p "$1 [Y/n]: " a || true
  case "${a:-y}" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

compose() { (cd "$DEPLOY_DIR" && docker compose "$@"); }

need_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    say "Docker is not installed on this server."
    if confirm "Install Docker now (from docker.com)?"; then
      command -v curl >/dev/null 2>&1 || { apt-get update -y && apt-get install -y curl; }
      curl -fsSL https://get.docker.com | sh
    else
      die "ReelVault needs Docker. Install it, then run this again."
    fi
  fi
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing. Install the docker-compose-plugin package."
  docker info >/dev/null 2>&1 || die "Docker is installed but not running, or you need sudo. Try: sudo ./deploy/setup.sh"
}

check_host() {
  local cpus mem_kb free_kb dockdir fstype
  cpus="$(nproc 2>/dev/null || echo 1)"
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  dockdir="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  free_kb="$(df -Pk "$dockdir" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
  fstype="$(df -PT "$dockdir" 2>/dev/null | awk 'NR==2 {print $2}' || echo unknown)"
  [ "$cpus" -ge 2 ] || warn "This server has $cpus CPU. ReelVault needs at least 2 (see DEPLOY.md, 'Host must have')."
  [ "$mem_kb" -ge 3800000 ] || warn "This server has less than 4 GB of memory. Transcription may run out of memory."
  [ "$free_kb" -ge 80000000 ] || warn "Less than 80 GB free on the Docker disk. Member files and videos need room."
  case "$fstype" in
    nfs*|cifs|smb*|fuse*|9p) die "Docker stores data on a network drive ($fstype). SQLite needs a local disk." ;;
  esac
}

rand_b64() { openssl rand -base64 "$1" | tr -d '\n'; }
rand_pw() { openssl rand -base64 24 | tr -d '\n/+=' | cut -c1-20; }

valid_domain() { [[ "$1" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$ ]]; }
valid_email() { [[ "$1" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; }

write_env() {  # write_env PUBLIC_URL DOMAIN ADMIN_EMAIL PROFILES
  local key pw hook
  key="k1:$(rand_b64 32)"
  pw="$(rand_pw)"
  hook="$(openssl rand -hex 24)"
  umask 077
  local tmp="$ENV_FILE.tmp"
  sed -e "s#^PUBLIC_URL=.*#PUBLIC_URL=$1#" \
      -e "s#^RV_MASTER_KEYS=.*#RV_MASTER_KEYS=$key#" \
      -e "s#^ADMIN_EMAIL=.*#ADMIN_EMAIL=$3#" \
      -e "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=$pw#" \
      -e "s#^RV_DOMAIN=.*#RV_DOMAIN=$2#" \
      -e "s#^COMPOSE_PROFILES=.*#COMPOSE_PROFILES=$4#" \
      -e "s#^SKOOL_HOOK_SECRET=.*#SKOOL_HOOK_SECRET=$hook#" \
      "$DEPLOY_DIR/.env.example" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
  ADMIN_PASSWORD_SHOWN="$pw"
}

env_get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2-; }

wait_for() {  # wait_for URL SECONDS [curl options]
  local url="$1" secs="$2"; shift 2
  local i=0
  while [ "$i" -lt "$secs" ]; do
    if curl -fsS --max-time 5 "$@" "$url" 2>/dev/null | grep -q '"ok": *true'; then return 0; fi
    sleep 3; i=$((i + 3))
  done
  return 1
}

start_and_wait() {
  say "Building and starting ReelVault. The first build downloads the AI models and can take 10-20 minutes."
  if [ "$NO_BUILD" = 1 ]; then say "(using the image that is already built)"; else compose build; fi
  compose up -d
  say "Waiting for the app to answer..."
  wait_for "http://127.0.0.1:8000/healthz" 300 || {
    compose logs --tail 60 app || true
    die "The app did not start. The last log lines are above. Run './deploy/setup.sh logs' to watch."
  }
  say "The app is running."
}

do_install() {
  need_docker
  check_host
  if [ -f "$ENV_FILE" ]; then
    say "deploy/.env already exists, so ReelVault is set up already. Keeping your keys and starting it."
    start_and_wait
    do_status
    return
  fi
  command -v openssl >/dev/null 2>&1 || die "openssl is missing. Install it (apt-get install openssl), then run this again."
  command -v curl >/dev/null 2>&1 || die "curl is missing. Install it (apt-get install curl), then run this again."
  local domain="${RV_DOMAIN:-}" email="${ADMIN_EMAIL:-}" url profiles
  if [ "$LOCAL" = 1 ]; then
    domain="localhost"; url="http://localhost:8000"; profiles=""
    ask email "Admin email (you sign in with it)" "admin@example.com"
  else
    bold "Two questions, then ReelVault sets itself up."
    say "Your domain's A record must already point at this server's IP address."
    ask domain "Your ReelVault address, without https:// (for example app.scaleitads.com)"
    ask email "Admin email (you sign in with it)"
    valid_domain "$domain" || die "'$domain' is not a domain name like app.example.com."
    url="https://$domain"; profiles="https"
    local ip_dns
    ip_dns="$(getent ahostsv4 "$domain" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
    [ -n "$ip_dns" ] || warn "$domain does not resolve yet. Add the A record now; HTTPS starts once it works."
  fi
  valid_email "$email" || die "'$email' is not an email address."
  write_env "$url" "$domain" "$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')" "$profiles"
  say "Wrote deploy/.env (only you can read it)."
  start_and_wait
  if [ "$LOCAL" = 0 ]; then
    say "Getting the HTTPS certificate for $domain..."
    if wait_for "https://$domain/healthz" 180; then
      say "HTTPS works."
    else
      warn "https://$domain does not answer yet. Check the domain's A record and that ports 80 and 443 are open."
      warn "Caddy keeps trying by itself. Check with: ./deploy/setup.sh status"
    fi
  fi
  echo
  bold "ReelVault is ready."
  say "Open $url and sign in."
  say "  Email:    $email"
  say "  Password: $ADMIN_PASSWORD_SHOWN   (shown once; change it after you sign in)"
  echo
  say "Save the file deploy/.env in your password manager now."
  say "Losing it means members re-enter their AI keys."
  say "Next: sign in, copy the community code from the banner, and post the signup link in a hidden Skool lesson."
}

do_update() {
  need_docker
  [ -f "$ENV_FILE" ] || die "ReelVault is not set up here yet. Run ./deploy/setup.sh first."
  if git -C "$DEPLOY_DIR/.." rev-parse --git-dir >/dev/null 2>&1; then
    say "Getting the newest version..."
    git -C "$DEPLOY_DIR/.." pull --ff-only || die "Could not update the code (local changes?). Nothing was restarted."
  fi
  say "Making a backup first..."
  compose exec -T app python -m reelvault.server.storage backup >/dev/null 2>&1 || warn "Backup before update failed."
  start_and_wait
  do_status
}

do_status() {
  [ -f "$ENV_FILE" ] || die "ReelVault is not set up here yet. Run ./deploy/setup.sh first."
  compose ps
  local url
  url="$(env_get PUBLIC_URL)"
  if curl -fsS --max-time 5 "http://127.0.0.1:8000/healthz" 2>/dev/null; then echo; say "App: healthy."; else warn "App: not answering on port 8000."; fi
  case "$url" in
    https://*) if curl -fsS --max-time 8 "$url/healthz" >/dev/null 2>&1; then say "HTTPS: $url works."; else warn "HTTPS: $url does not answer yet."; fi ;;
  esac
}

# Tests source this file with RV_SETUP_SOURCE_ONLY=1 to call the functions one by one.
[ "${RV_SETUP_SOURCE_ONLY:-0}" = 1 ] && return 0

case "$MODE" in
  install) do_install ;;
  update) do_update ;;
  status) do_status ;;
  logs) compose logs -f --tail 100 ;;
esac
