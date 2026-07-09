#!/bin/sh
#
# DBBASIC Object Server — container entrypoint.
#
# Mirrors scripts/install.sh's first-boot admin-token behavior for the
# container path: if DBBASIC_ADMIN_TOKEN is not set in the environment,
# generate one once, persist it under the mounted data directory so it
# survives restarts and image rebuilds, and print it clearly to stdout on
# that first boot only. An existing persisted token is never overwritten,
# and no token is ever baked into the image — only into the runtime-only
# data volume.
#
# On later boots, if DBBASIC_ADMIN_TOKEN still is not set explicitly, the
# persisted token file is read instead of generating a new one.
set -eu

DATA_DIR="${DBBASIC_DATA_DIR:-/data/state}"
TOKEN_FILE="${DATA_DIR}/admin_token"

mkdir -p "${DATA_DIR}"

if [ -z "${DBBASIC_ADMIN_TOKEN:-}" ]; then
  if [ -f "${TOKEN_FILE}" ]; then
    DBBASIC_ADMIN_TOKEN="$(cat "${TOKEN_FILE}")"
  else
    DBBASIC_ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
    umask 077
    printf '%s' "${DBBASIC_ADMIN_TOKEN}" >"${TOKEN_FILE}"
    printf '\n'
    printf '============================================================\n'
    printf ' DBBASIC: generated a new admin token on first boot.\n'
    printf ' Save it now -- it is only printed here, once:\n'
    printf '\n   %s\n\n' "${DBBASIC_ADMIN_TOKEN}"
    printf ' Persisted at (inside the container): %s\n' "${TOKEN_FILE}"
    printf ' To pin it explicitly (recommended once you have a real\n'
    printf ' deployment, e.g. Coolify-managed environment variables),\n'
    printf ' set DBBASIC_ADMIN_TOKEN yourself and it will be used instead.\n'
    printf '============================================================\n\n'
  fi
fi

export DBBASIC_ADMIN_TOKEN

exec "$@"
