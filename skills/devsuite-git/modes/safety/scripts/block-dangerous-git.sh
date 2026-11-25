#!/bin/bash

# The Python classifier is the single policy implementation. Keep this wrapper
# deliberately small so every dependency or classifier failure blocks the tool.
SCRIPT_PATH=${BASH_SOURCE[0]}
SCRIPT_DIR=${SCRIPT_PATH%/*}
if [[ "$SCRIPT_DIR" == "$SCRIPT_PATH" ]]; then
  SCRIPT_DIR=.
fi
CLASSIFIER="$SCRIPT_DIR/classify_git_command.py"

block() {
  printf 'BLOCKED: %s\n' "$1" >&2
  exit 2
}

command -v python3 >/dev/null 2>&1 || block "python3 is required by the Git safety classifier."
[[ -r "$CLASSIFIER" ]] || block "Git safety classifier is missing or unreadable: $CLASSIFIER"

if python3 "$CLASSIFIER" --hook-payload; then
  exit 0
else
  STATUS=$?
fi
if [[ $STATUS -eq 2 ]]; then
  exit 2
fi

block "Git safety classifier failed with status $STATUS."
