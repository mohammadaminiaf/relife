#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$PROJECT_DIR/.tools/node_modules/node/bin:$PATH"
export REA_JADX_MCP_JAR="${REA_JADX_MCP_JAR:-$PROJECT_DIR/.tools/jadx-headless-mcp-0.7.1-all.jar}"
if [ -z "${JAVA_HOME:-}" ]; then
    if [ -x /usr/libexec/java_home ]; then
        JAVA_HOME=$(/usr/libexec/java_home -v 21 2>/dev/null || /usr/libexec/java_home 2>/dev/null || true)
        if [ -n "$JAVA_HOME" ]; then
            export JAVA_HOME
        fi
    fi
fi
if [ ! -x "$PROJECT_DIR/.tools/node_modules/node/bin/node" ]; then
    printf '%s\n' 'Project tools are missing. Run ./setup.sh first.' >&2
    exit 1
fi
exec "$PROJECT_DIR/.tools/node_modules/node/bin/node" \
    "$PROJECT_DIR/.tools/node_modules/rea-agents/scripts/rea.mjs" "$@"
