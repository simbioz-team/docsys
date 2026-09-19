#!/usr/bin/env bash
# PostToolUse: Edit|Write|NotebookEdit
# Правка документа под docs/ — пересобрать реестры.
#
# Автоматически, а не напоминанием: сборка детерминирована и быстрая, а реестр,
# который обновляют по памяти, рано или поздно расходится с документами.
#
# No-op, если в проекте нет .claude/doc-config.json: плагин обязан молчать
# там, где систему документации не разворачивали.
set -uo pipefail

INPUT=$(cat)
# По строке на поле, а не через пробел: в пути документа пробел допустим,
# и разбор через `read a b` разрезал бы путь посередине.
FIELDS=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print(); print(); raise SystemExit
print(((d.get("tool_input") or {}).get("file_path") or "").replace("\n", " "))
print((d.get("cwd") or "").replace("\n", " "))
')
FILE_PATH=$(printf '%s\n' "$FIELDS" | sed -n 1p)
WORK_DIR=$(printf '%s\n' "$FIELDS" | sed -n 2p)

[ -z "$FILE_PATH" ] && exit 0
case "$FILE_PATH" in *.md) ;; *) exit 0 ;; esac

# cwd, а не CLAUDE_PROJECT_DIR: в worktree-сессии агент работает в своём дереве.
ROOT="${WORK_DIR:-}"
[ -z "$ROOT" ] && ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
[ -f "$ROOT/.claude/doc-config.json" ] || exit 0

DOCS_DIR=$(ROOT="$ROOT" python3 -c "
import json, os
with open(os.path.join(os.environ['ROOT'], '.claude/doc-config.json')) as f:
    print(json.load(f).get('docs_dir', 'docs'))
" 2>/dev/null) || exit 0
[ -z "$DOCS_DIR" ] && exit 0
case "$FILE_PATH" in *"/$DOCS_DIR/"*|"$DOCS_DIR/"*) ;; *) exit 0 ;; esac

OUT=$(cd "$ROOT" && python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_registries.py" 2>&1)
case "$OUT" in
  *перестроено*) echo "$OUT" ;;   # в контекст попадёт только факт пересборки
esac
exit 0
