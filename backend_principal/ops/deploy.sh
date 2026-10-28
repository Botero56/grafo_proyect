#!/usr/bin/env bash
# Despliega EXACTAMENTE el commit HEAD (nunca el árbol local): sube un
# `git archive` a Cloud Build, que corre los tests y despliega los dos
# servicios sin tráfico, con la etiqueta c-<sha>. El paso a producción
# (update-traffic) es manual tras la prueba de humo: ver docs/RUNBOOK.md.
#
#   ./ops/deploy.sh            # REGION=us-central1 por defecto
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Hay cambios sin commit: haz commit (o stash) antes de desplegar." >&2
  exit 1
fi

sha=$(git rev-parse --short=8 HEAD)
archive="$(mktemp -d)/j2ys-$sha.tar.gz"
git archive --format=tar.gz -o "$archive" HEAD

gcloud builds submit "$archive" \
  --config cloudbuild.yaml \
  --region "${REGION:-us-central1}" \
  --substitutions "_TAG=c-$sha"

echo "Desplegado sin tráfico con la etiqueta c-$sha. Siguiente: prueba de humo y update-traffic (docs/RUNBOOK.md)."
