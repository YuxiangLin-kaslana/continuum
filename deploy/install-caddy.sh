#!/usr/bin/env bash
# One-time setup, run with sudo: puts Continuum's Caddy site in /etc/caddy/continuum.caddy (owned by ec2-user so
# later edits need no sudo), imports it from the Caddyfile, checks the result, and reloads Caddy.
#   sudo bash deploy/install-caddy.sh continuum.yuxianglin.xyz
set -euo pipefail
domain="${1:?usage: sudo bash deploy/install-caddy.sh <domain>}"
owner="${SUDO_USER:-ec2-user}"
here="$(cd "$(dirname "$0")" && pwd)"
site=/etc/caddy/continuum.caddy
main=/etc/caddy/Caddyfile

sed "s/__DOMAIN__/${domain}/g" "$here/continuum.caddy.template" > "$site"
chown "$owner:$owner" "$site"
chmod 644 "$site"

if ! grep -q "import /etc/caddy/continuum.caddy" "$main"; then
  cp "$main" "$main.bak.$(date +%Y%m%d-%H%M%S)"
  printf '\n# >>> Continuum (deploy/install-caddy.sh) >>>\nimport /etc/caddy/continuum.caddy\n# <<< Continuum <<<\n' >> "$main"
fi

caddy validate --config "$main" --adapter caddyfile
systemctl reload caddy
echo "Caddy now serves https://${domain} -> 127.0.0.1:8170"
