#!/usr/bin/env bash
#
# DBBASIC Object Server on a new public server, for a one-click installer (cloud-init.yaml runs this).
#
#   sudo bash scripts/one-click-install.sh 2026.11v1
#
# scripts/install.sh assumes an operator who is there and opens things up step by step. A one-click box
# is on the public internet from its first second with nobody signed up, so this runs install.sh and then
# closes it up before anything outside can reach it:
#
#   1. a setup code. The installer (GitStable) shows the code to the person who pressed the button and
#      puts only its SHA-256 in /etc/dbbasic-setup-code.sha256 (cloud-init). It moves into the data
#      directory, and until someone uses it at /setup the server answers nothing but /health and /setup
#      (object_setup.py). No default login, and no password the box could hand out.
#   2. password login on (setup makes one) and permission enforcement on, with the starter policy. The
#      server only makes enforcement effective once an admin with a password exists, i.e. after setup.
#   3. HTTPS on the server's IP address, from Let's Encrypt, through Caddy: the setup code and the new
#      password never cross the network in the clear. The server itself stays on 127.0.0.1:8001.
#
# Refuses to start without the setup code: a public box with no way to claim it but a race is worse
# than no box. Safe to re-run (a later release, or after a failure); a box already set up stays set up.
#
set -euo pipefail

VERSION="${1:?usage: one-click-install.sh <release, such as 2026.11v1>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_DIR="${DBBASIC_CODE_DIR:-/opt/dbbasic-object-server}"
STATE_DIR="${DBBASIC_STATE_DIR:-/var/lib/dbbasic-object-server}"
ENV_FILE="${DBBASIC_ENV_FILE:-/etc/dbbasic-object-server.env}"
DATA_DIR="${STATE_DIR}/data"
SETUP_HASH_IN="/etc/dbbasic-setup-code.sha256"
SETUP_HASH="${DATA_DIR}/identity/setup_code.sha256"
SERVICE_USER="dbbasic"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

already_set_up() {   # an admin exists: the same test the server uses (object_setup._admin_exists)
  [[ -f "${DATA_DIR}/identity/users.tsv" && -x "${CODE_DIR}/.venv/bin/python" ]] || return 1
  ( cd "${CODE_DIR}" && sudo -u "${SERVICE_USER}" .venv/bin/python -c \
      'import sys, object_setup as s; sys.exit(0 if s._admin_exists(sys.argv[1]) else 1)' "${DATA_DIR}" )
}

if [[ ! -s "${SETUP_HASH_IN}" && ! -s "${SETUP_HASH}" ]] && ! already_set_up; then
  echo "No setup code (${SETUP_HASH_IN}). The installer puts its hash there; without it nobody could" >&2
  echo "become this server's admin except by racing for it. Stopping before anything is installed." >&2
  exit 1
fi
if ! grep -Eqx 'sha256:[0-9a-f]{64}' "${SETUP_HASH_IN}" 2>/dev/null && [[ -e "${SETUP_HASH_IN}" ]]; then
  echo "${SETUP_HASH_IN} isn't a setup code hash (sha256:<64 hex>). Stopping." >&2
  exit 1
fi

bash "${HERE}/install.sh" --version "${VERSION}"

say "Setup code: only its hash, readable by the server alone"
if [[ -s "${SETUP_HASH_IN}" ]]; then
  if already_set_up; then
    rm -f "${SETUP_HASH_IN}"          # someone is admin already: there's nothing left to claim
  else
    install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${DATA_DIR}/identity"
    install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0600 "${SETUP_HASH_IN}" "${SETUP_HASH}"
    rm -f "${SETUP_HASH_IN}"
    FRESH=1
  fi
fi

if [[ "${FRESH:-0}" == 1 ]]; then
  # A new box with no apps has no home page: the owner finished setup and landed on "Not found" (seen on the
  # first test droplet, 2026-11-23). A few apps to start with; more from the packages page. Only on a fresh
  # box, so a re-run never replaces apps the owner may have changed.
  say "Starting apps: home and menu, projects, notes, tasks"
  ( cd "${CODE_DIR}" && sudo -u "${SERVICE_USER}" .venv/bin/python -c '
import sys
from pathlib import Path
import object_packages
for app in ("app-nav", "app-projects", "app-notes", "app-tasks"):
    object_packages.install_package(app, root=Path("packages"), base_dir=Path(sys.argv[1]),
                                    object_roots=[Path(sys.argv[2])], allow_replace=True)
    print("   " + app)
' "${DATA_DIR}" "${STATE_DIR}/objects" )
fi

say "Login and permissions for a public server"
set_env() {   # KEY VALUE: replace the line, or add it
  if grep -q "^$1=" "${ENV_FILE}"; then
    sed -i "s|^$1=.*|$1=$2|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "$1" "$2" >> "${ENV_FILE}"
  fi
}
set_env DBBASIC_ENABLE_PASSWORD_LOGIN true
set_env DBBASIC_ENABLE_PERMISSION_ENFORCEMENT true
set_env DBBASIC_COOKIE_SECURE true
set_env DBBASIC_RATE_LIMIT_TRUST_PROXY_HEADERS true     # Caddy writes X-Forwarded-For itself
if [[ ! -f "${DATA_DIR}/permissions/policy.json" ]]; then
  ( cd "${CODE_DIR}" && sudo -u "${SERVICE_USER}" .venv/bin/python -c \
      'import sys, object_permission_store as s; s.replace_policy(s.starter_policy_payload(), base_dir=sys.argv[1])' \
      "${DATA_DIR}" )
fi
systemctl restart dbbasic-object-server

say "Caddy, for HTTPS on this server's address"
ADDRESS="${DBBASIC_PUBLIC_ADDRESS:-}"
if [[ -z "${ADDRESS}" ]]; then
  # DigitalOcean's metadata service; another cloud sets DBBASIC_PUBLIC_ADDRESS
  ADDRESS="$(curl -fsS --max-time 5 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)"
fi
if ! [[ "${ADDRESS}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "Couldn't tell this server's public IPv4 address; set DBBASIC_PUBLIC_ADDRESS and run again." >&2
  exit 1
fi
if ! command -v caddy >/dev/null 2>&1; then
  # Caddy's own packages: Ubuntu's caddy is too old to ask Let's Encrypt for an IP address certificate
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi
cat > /etc/caddy/Caddyfile <<EOF
# Written by scripts/one-click-install.sh. A certificate for the bare IP address (Let's Encrypt gives
# those only as six-day "shortlived" certificates; Caddy renews them). With a domain, replace the
# address below with the domain name and drop the two global lines.
{
	default_sni ${ADDRESS}
	cert_issuer acme {
		profile shortlived
	}
}

${ADDRESS} {
	reverse_proxy 127.0.0.1:8001
}
EOF
systemctl enable -q caddy
systemctl restart caddy

say "Waiting for https://${ADDRESS}/health (the certificate takes a few seconds)"
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://${ADDRESS}/health" >/dev/null 2>&1; then
    say "Up: https://${ADDRESS}/  (setup at https://${ADDRESS}/setup with the code the installer showed you)"
    exit 0
  fi
  sleep 3
done
echo "HTTPS didn't answer yet; see: journalctl -u caddy -e" >&2
exit 1
