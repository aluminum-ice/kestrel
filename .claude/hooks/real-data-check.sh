#!/bin/bash
# PreToolUse (Bash): before a `git commit`, run the real-data check over
# everything that could end up in it (tracked files plus untracked files git
# would add), and block the command if it finds anything (CLAUDE.md, "Nothing
# real in the repo"). Exit 2 blocks the call and shows Claude the findings.
set -uo pipefail

cmd=$(python3 -c 'import json, sys; print(json.load(sys.stdin).get("tool_input", {}).get("command", ""))' 2>/dev/null) || exit 0
# Anything that looks like a commit runs the check; a false match only costs a
# quick scan, since the hook blocks on findings, not on the command.
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_./-])git[[:space:]].*commit' || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
out=$(python3 tools/real-data-check/real_data_check.py --include-untracked 2>&1)
if [ $? -eq 1 ]; then
    printf '%s\n' "$out" >&2
    echo "Commit blocked by .claude/hooks/real-data-check.sh: replace the findings with placeholders, then commit again." >&2
    exit 2
fi
exit 0
