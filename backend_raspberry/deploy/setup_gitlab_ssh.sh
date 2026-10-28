#!/usr/bin/env bash
# Deja lista la conexión SSH de la Raspberry hacia GitLab.
# Idempotente: si ya puede leer el repo por SSH, no hace nada.
# Si no, genera una llave propia y la registra como deploy key (solo lectura)
# usando GITLAB_TOKEN una única vez.
set -euo pipefail

GITLAB_PROJECT=${GITLAB_PROJECT:-boterosamuel3/backend_rasperry}
KEY="$HOME/.ssh/gitlab_deploy"
REMOTE="git@gitlab.com:${GITLAB_PROJECT}.git"
# Huella oficial ED25519 de gitlab.com (docs.gitlab.com/user/gitlab_com)
GITLAB_FP="SHA256:eUXGGm1YGsMAS7vkcx6JOJdOGHPem5gQp4taiCfCLB8"

mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"

# 1. Host key de gitlab.com, verificada contra la huella oficial
if ! ssh-keygen -F gitlab.com >/dev/null 2>&1; then
  tmp=$(mktemp)
  ssh-keyscan -t ed25519 gitlab.com 2>/dev/null > "$tmp"
  if ! ssh-keygen -lf "$tmp" | grep -q "$GITLAB_FP"; then
    rm -f "$tmp"; echo "La huella de gitlab.com no coincide, abortando" >&2; exit 1
  fi
  cat "$tmp" >> "$HOME/.ssh/known_hosts"; rm -f "$tmp"
fi

# 2. Llave propia de esta Raspberry
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "raspberry-$(hostname)" -f "$KEY"

# 3. Usar esa llave para gitlab.com
if ! grep -q "^Host gitlab.com" "$HOME/.ssh/config" 2>/dev/null; then
  printf 'Host gitlab.com\n  IdentityFile %s\n  IdentitiesOnly yes\n' "$KEY" >> "$HOME/.ssh/config"
fi
chmod 600 "$HOME/.ssh/config"

can_read() { GIT_SSH_COMMAND="ssh -o BatchMode=yes" git ls-remote "$REMOTE" HEAD >/dev/null 2>&1; }

if can_read; then echo "SSH a GitLab OK"; exit 0; fi

# 4. Registrar la llave como deploy key con el token
: "${GITLAB_TOKEN:?La llave no está registrada y falta GITLAB_TOKEN en /etc/raspberry-backend.env}"
body=$(mktemp)
code=$(curl -sS -o "$body" -w '%{http_code}' -X POST \
  -H @<(printf 'PRIVATE-TOKEN: %s' "$GITLAB_TOKEN") \
  --data-urlencode "title=raspberry-$(hostname)" \
  --data-urlencode "key=$(cat "$KEY.pub")" \
  --data "can_push=false" \
  "https://gitlab.com/api/v4/projects/${GITLAB_PROJECT//\//%2F}/deploy_keys")
if [ "$code" != 201 ]; then
  echo "GitLab respondió $code: $(cat "$body")" >&2; rm -f "$body"; exit 1
fi
rm -f "$body"

for _ in 1 2 3 4 5; do
  if can_read; then echo "Deploy key registrada, SSH a GitLab OK"; exit 0; fi
  sleep 3
done
echo "La llave se registró pero SSH aún no responde" >&2
exit 1
