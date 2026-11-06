#!/usr/bin/env bash
# PostToolUse: Edit|Write|NotebookEdit
# Правка документа — под docs/ или по шаблону extra_roots — пересобрать реестры.
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

# Документ ли это: под docs_dir либо по шаблону extra_roots (`specs/*/spec.md`).
# Решает docs_lib — тот же разбор шаблонов, что у сборки реестров: два
# разных разбора однажды разошлись бы, и хук молчал бы о правке спеки.
IS_DOC=$(ROOT="$ROOT" FILE_PATH="$FILE_PATH" python3 -c "
import os, sys
sys.path.insert(0, os.path.join(os.environ.get('CLAUDE_PLUGIN_ROOT', ''), 'scripts'))
import docs_lib as lib
root = os.path.realpath(os.environ['ROOT'])
cfg = lib.load_config(lib.pathlib.Path(root))
fp = os.environ['FILE_PATH']
fp = fp if os.path.isabs(fp) else os.path.join(root, fp)
rel = os.path.relpath(os.path.realpath(fp), root).replace(os.sep, '/')
d = cfg.get('docs_dir', 'docs').rstrip('/')
print('1' if rel.startswith(d + '/') or lib.matches_extra(rel, cfg) else '')
" 2>/dev/null) || exit 0
[ -n "$IS_DOC" ] || exit 0

OUT=$(cd "$ROOT" && python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_registries.py" 2>&1)
case "$OUT" in
  *перестроено*) echo "$OUT" ;;   # в контекст попадёт только факт пересборки
esac
exit 0
