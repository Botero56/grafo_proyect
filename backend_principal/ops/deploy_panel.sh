#!/usr/bin/env bash
# Despliega el panel interno `j2ys-panel`: el código de este repo (HEAD,
# nunca el árbol local) + el front de frontend.git (rama FRONT_BRANCH, main
# por defecto). Queda detrás de Cloudflare Access: la primera vez, seguir
# docs/RUNBOOK.md §10.
#
#   CF_ACCESS_TEAM_DOMAIN=<equipo>.cloudflareaccess.com CF_ACCESS_AUD=<aud> ./ops/deploy_panel.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Hay cambios sin commit: haz commit (o stash) antes de desplegar." >&2
  exit 1
fi

REGION="${REGION:-us-central1}"
PROJECT="${PROJECT:-j2ys-tech-chat-automation}"
FRONT_REPO="${FRONT_REPO:-git@gitlab.com:j2ys-tech-chat-automation/frontend.git}"
FRONT_BRANCH="${FRONT_BRANCH:-main}"
WORKER_URL="${WORKER_URL:-https://j2ys-lt-app-ywi3275i5a-uc.a.run.app}"
SA="j2ys-lt-run@${PROJECT}.iam.gserviceaccount.com"
: "${CF_ACCESS_TEAM_DOMAIN:?Falta CF_ACCESS_TEAM_DOMAIN (<equipo>.cloudflareaccess.com)}"
: "${CF_ACCESS_AUD:?Falta CF_ACCESS_AUD (AUD tag de la aplicación en Cloudflare Access)}"

ctx="$(mktemp -d)"
git archive HEAD app pyproject.toml uv.lock .python-version Dockerfile.panel | tar -x -C "$ctx"
git clone -q --depth 1 --branch "$FRONT_BRANCH" "$FRONT_REPO" "$ctx/_front"
front_sha="$(git -C "$ctx/_front" rev-parse --short=8 HEAD)"
cp -r "$ctx/_front/frontend" "$ctx/panel_static"
rm -rf "$ctx/_front"
mv "$ctx/Dockerfile.panel" "$ctx/Dockerfile"

tag="c-$(git rev-parse --short=8 HEAD)-f-${front_sha}"
image="${REGION}-docker.pkg.dev/${PROJECT}/j2ys/panel:${tag}"
gcloud builds submit "$ctx" --project "$PROJECT" --region "$REGION" --tag "$image"

# Público para Cloud Run a propósito: la barrera es el JWT de Cloudflare
# Access, que panel_entry verifica en cada request (sin él, 403).
gcloud beta run deploy j2ys-panel --project "$PROJECT" --region "$REGION" --image "$image" \
  --service-account "$SA" --allow-unauthenticated --no-iap --max-instances 1 \
  --set-env-vars "WORKER_URL=${WORKER_URL},CF_ACCESS_TEAM_DOMAIN=${CF_ACCESS_TEAM_DOMAIN},CF_ACCESS_AUD=${CF_ACCESS_AUD}" \
  --set-secrets "PANEL_API_KEY=admin-api-key:latest"

echo "Panel desplegado (${tag}). URL: $(gcloud run services describe j2ys-panel --project "$PROJECT" --region "$REGION" --format='value(status.url)')"
