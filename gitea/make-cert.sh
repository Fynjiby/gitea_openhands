#!/usr/bin/env bash
#
# One-time setup for this Gitea instance.
#
#   1. Records the server IP in .env -- the single source of truth that
#      docker-compose.yml reads its DOMAIN, ROOT_URL and SSH_DOMAIN from.
#   2. Creates certs/server.crt + certs/server.key with that IP in the SAN.
#
# Usage:
#   sudo ./make-cert.sh 203.0.113.10
#
# Re-running is safe: a certificate that already covers this IP and is not
# about to expire is kept as is, so clients that already trust it do not break.
#
# Why sudo: the Gitea container runs as uid/gid 1000 (USER_UID/USER_GID in
# docker-compose.yml) and must be able to read the private key, so the files
# are chowned to it. Only root can do that, and the key stays mode 640.
set -euo pipefail
cd "$(dirname "$0")"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '  %s\n' "$*"; }

valid_ipv4() {
  local ip=$1 octet
  [[ $ip =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for octet in "${BASH_REMATCH[@]:1}"; do
    (( 10#$octet <= 255 )) || return 1
  done
}

# --- 1. Resolve the IP ----------------------------------------------------
# Read .env without sourcing it, so a typo in the file cannot run commands.
env_get() {
  [ -f .env ] || return 0
  sed -n "s/^$1=\([^#]*\).*$/\1/p" .env | tail -n 1 | tr -d '\r'
}

IP="${1:-$(env_get SERVER_IP)}"
WEB_PORT="$(env_get WEB_PORT)"
WEB_PORT="${WEB_PORT:-3001}"

[ -n "$IP" ] || die "usage: sudo $0 <server-ip>"
valid_ipv4 "$IP" || die "'$IP' is not a valid IPv4 address (expected e.g. 203.0.113.10)"
case "$IP" in
  0.0.0.0|127.*|255.*) die "'$IP' is not an address clients can reach this server on" ;;
esac

command -v openssl >/dev/null 2>&1 || die "openssl is not installed"

# --- 2. Record the IP in .env --------------------------------------------
if [ ! -f .env ]; then
  [ -f .env.example ] || die ".env and .env.example are both missing -- cannot continue"
  cp .env.example .env
fi
tmp=$(mktemp)
if grep -q '^SERVER_IP=' .env; then
  awk -v ip="$IP" '/^SERVER_IP=/{print "SERVER_IP=" ip; next} {print}' .env >"$tmp"
else
  { cat .env; printf 'SERVER_IP=%s\n' "$IP"; } >"$tmp"
fi
mv "$tmp" .env
# Not a secret (just an address), and `docker compose` may run as another
# user than the one running this script, so keep it world-readable.
chmod 644 .env

# --- 3. Create or keep the certificate -----------------------------------
mkdir -p certs

cert_ok=0
if [ -f certs/server.crt ] && [ -f certs/server.key ]; then
  if openssl x509 -in certs/server.crt -noout -checkend $((30 * 86400)) >/dev/null 2>&1 &&
    openssl x509 -in certs/server.crt -noout -text 2>/dev/null | grep -qF "IP Address:${IP}" &&
    [ "$(openssl x509 -in certs/server.crt -noout -pubkey 2>/dev/null)" = \
      "$(openssl pkey -in certs/server.key -pubout 2>/dev/null)" ]; then
    cert_ok=1
  fi
fi

if [ "$cert_ok" -eq 1 ]; then
  note "existing certificate already covers ${IP} and is valid for 30+ days -- keeping it"
else
  note "creating a self-signed certificate for ${IP}"
  # Generate next to the live files, then move into place, so a failure
  # halfway through never leaves a mismatched or truncated pair behind.
  candir=$(mktemp -d)
  trap 'rm -rf "$candir"' EXIT
  tmpkey="$candir/server.key"
  tmpcrt="$candir/server.crt"
  # Make both files up front so openssl does not prompt for a passphrase.
  ( umask 077 && : >"$tmpkey" && : >"$tmpcrt" )
  openssl req -x509 -newkey rsa:4096 -sha256 -days 825 -nodes \
    -keyout "$tmpkey" -out "$tmpcrt" \
    -subj "/CN=${IP}" \
    -addext "subjectAltName=IP:${IP}" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" \
    -addext "subjectKeyIdentifier=hash" \
    2>/dev/null
  mv "$tmpkey" certs/server.key
  mv "$tmpcrt" certs/server.crt
fi

# --- 4. Make the pair readable by the container user ---------------------
chmod 640 certs/server.key
chmod 644 certs/server.crt

# The container reads these as uid 1000. Only root can hand ownership over,
# so if we are not root and we are not uid 1000 ourselves, the container
# will not be able to open the key -- say so instead of failing at startup.
if [ "$(id -u)" -eq 0 ]; then
  chown 1000:1000 certs/server.key certs/server.crt
elif [ "$(id -u)" -ne 1000 ]; then
  echo "warning: running as uid $(id -u), but the container reads the key as uid 1000." >&2
  echo "         Either re-run as root:  sudo $0 ${IP}" >&2
  echo "         or make the files readable: sudo chown 1000:1000 certs/server.key certs/server.crt" >&2
fi

# --- 5. Report -----------------------------------------------------------
echo
echo "SERVER_IP=${IP} written to .env"
openssl x509 -in certs/server.crt -noout -subject -dates -ext subjectAltName 2>/dev/null ||
  openssl x509 -in certs/server.crt -noout -subject -dates
echo

if command -v docker >/dev/null 2>&1; then
  if config_err=$(docker compose config -q 2>&1); then
    note "docker compose config: OK"
  else
    # Show docker's own message: it names the file and line at fault.
    echo >&2
    echo "error: 'docker compose config' rejected the setup:" >&2
    printf '%s\n' "$config_err" | sed 's/^/  /' >&2
    exit 1
  fi
else
  note "warning: docker not found on PATH -- run 'docker compose config -q' on the server"
fi

cat <<EOF

Next:
  docker compose up -d
  docker compose logs -f gitea

The UI will be at  https://${IP}:${WEB_PORT}/
EOF
