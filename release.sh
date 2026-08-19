#!/bin/bash
# Release con versionado para ArgoCD.
# Uso: ./release.sh [patch|minor|major] "mensaje del commit"
#
# argocd-image-updater esta desplegado pero escalado a 0 replicas, asi que el
# rollout depende de que el tag de k8s/deployment.yaml cambie en git. Ademas k3s
# no re-descarga un tag que no ha cambiado: por eso nunca se usa :latest aqui.
set -e

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
print_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

IMAGE_NAME="tapo-guardian"
REGISTRY="ghcr.io/3kn4ls"
DEPLOYMENT_FILE="k8s/deployment.yaml"
VERSION_FILE="VERSION"

BUMP="${1:-patch}"
MESSAGE="${2:-release}"

[ -f "$VERSION_FILE" ] || echo "1.0.0" > "$VERSION_FILE"
CURRENT_VERSION=$(tr -d '[:space:]' < "$VERSION_FILE")
print_info "Version actual: v$CURRENT_VERSION"

IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"
case "$BUMP" in
    major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
    minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
    patch) PATCH=$((PATCH + 1)) ;;
    *) print_error "Uso: ./release.sh [patch|minor|major] \"mensaje\""; exit 1 ;;
esac
NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
print_info "Nueva version: v$NEW_VERSION"

echo "$NEW_VERSION" > "$VERSION_FILE"
sed -i "s|image: ${REGISTRY}/${IMAGE_NAME}:v.*|image: ${REGISTRY}/${IMAGE_NAME}:v${NEW_VERSION}|g" "$DEPLOYMENT_FILE"

if ! grep -q "image: ${REGISTRY}/${IMAGE_NAME}:v${NEW_VERSION}" "$DEPLOYMENT_FILE"; then
    print_error "No se pudo actualizar el tag en $DEPLOYMENT_FILE"
    exit 1
fi

git add "$VERSION_FILE" "$DEPLOYMENT_FILE"
git commit -m "release: v${NEW_VERSION} - ${MESSAGE}"
git tag -a "v${NEW_VERSION}" -m "v${NEW_VERSION} - ${MESSAGE}"
git push origin HEAD
git push origin "v${NEW_VERSION}"

print_info "Publicado v${NEW_VERSION}."
print_warning "GitHub Actions construye la imagen; ArgoCD sincroniza en ~3 min."
print_info "Seguimiento: kubectl -n tapo-guardian get pods -w"
