#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PATH="$PROJECT_DIR/.tools/node_modules/node/bin:$PATH"
cd "$PROJECT_DIR/bruno"
exec "$PROJECT_DIR/.tools/bruno/node_modules/.bin/bru" "$@"
