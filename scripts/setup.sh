#!/bin/sh
set -eu
SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_RUNTIME=${EGO_BRIDGE_RUNTIME:-"$HOME/apps/ego-chatgpt-bridge"}
export UV_PROJECT_ENVIRONMENT="$APP_RUNTIME/.venv"
uv sync --project "$SOURCE_ROOT" --frozen --no-editable --reinstall-package ego-chatgpt-bridge
"$APP_RUNTIME/.venv/bin/ego-bridge" --runtime "$APP_RUNTIME" init
printf '%s\n' "Source: $SOURCE_ROOT" "Runtime and token: $APP_RUNTIME" "Configure config.json before using browser tools."
