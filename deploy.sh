#!/usr/bin/env bash

usage() {
    echo "Usage: $0 <hostname>" >&2
    echo "  Builds web and nginx images on a remote Docker host and restarts containers." >&2
    echo "" >&2
    echo "  Host-specific env vars (e.g. MEDIA_ROOT_HOST) can be placed in" >&2
    echo "  .env.<hostname> at the project root and will be loaded automatically." >&2
    exit 1
}

HOST="localhost"
if [[ "$1" == "--help" || "$1" == "-?" ]]; then
    usage
elif [[ -n "$1" ]]; then
    HOST="$1"
fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load host-specific env overrides if present.
ENV_FILE="${SCRIPT_DIR}/.env.${HOST}"
if [[ -f "$ENV_FILE" ]]; then
    echo "==> Loading ${ENV_FILE}..."
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

if [[ "$HOST" != "localhost" ]]; then
    export DOCKER_HOST="ssh://${HOST}"
fi

SERVICES=(couchdb web nginx)

echo "==> Building images on ${HOST}..."
docker compose build "${SERVICES[@]}"

echo "==> Deploying to ${HOST}..."
for svc in "${SERVICES[@]}"; do
    if output=$(docker compose up -d --no-deps "$svc" 2>&1); then
        echo "  ${svc}: started"
    else
        # If a container with the same name exists but isn't compose-managed,
        # docker refuses to replace it. Evict it and retry once.
        name=$(echo "$output" | grep -oP '(?<=The container name "/)[^"]+' || true)
        if [[ -z "$name" ]]; then
            echo "$output" >&2
            exit 1
        fi
        echo "  ${svc}: removing existing container '${name}'..."
        docker stop "$name" >/dev/null
        docker rm   "$name" >/dev/null
        docker compose up -d --no-deps "$svc"
        echo "  ${svc}: started"
    fi
done

echo ""
echo "==> Containers on ${HOST}:"
docker ps --format "  {{.Names}}: {{.Status}}"
