#!/bin/sh
# Double-click this file in Finder, or use ./library.sh from a terminal.
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$PROJECT_DIR/library.sh" "$@"
