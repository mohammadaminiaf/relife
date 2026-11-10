#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$PROJECT_DIR/.tools/node_modules/node/bin:$PATH"
export REA_JADX_MCP_JAR="${REA_JADX_MCP_JAR:-$PROJECT_DIR/.tools/jadx-headless-mcp-0.7.1-all.jar}"
if [ -z "${JAVA_HOME:-}" ]; then
    JAVA_HOME=$(/usr/libexec/java_home -v 21)
    export JAVA_HOME
fi
exec "$PROJECT_DIR/.tools/node_modules/node/bin/node" \
    "$PROJECT_DIR/.tools/node_modules/rea-agents/scripts/rea.mjs" "$@"
