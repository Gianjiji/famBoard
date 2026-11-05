#!/usr/bin/env bash
# PostToolUse hook: run the famboard pytest suite whenever a project Python
# file is edited/written. Silent on success; prints the tail and exits 2
# (surfacing the failure to Claude Code) on any test failure.
f=$(python3 -c "import sys,json
d=json.load(sys.stdin)
ti=d.get('tool_input') or {}
tr=d.get('tool_response') or {}
print(ti.get('file_path') or tr.get('filePath') or '')" 2>/dev/null)

case "$f" in
  /opt/piHole/*.py)
    cd /opt/piHole || exit 0
    out=$(.venv/bin/pytest -q 2>&1); rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "PYTEST FAILED after editing $f:"
      echo "$out" | tail -30
      exit 2
    fi
    ;;
esac
exit 0
