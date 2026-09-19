#!/usr/bin/env python3
"""Разрешимость ссылок в документации и задачах — сверка расхождения.

Ссылка, которая не открывается, — это ровно тот случай, когда агент «не нашёл основание»
и додумал его. Проверка дешёвая и ловит уже накопленное: на 09.09.2026 в репозитории было
113 неразрешимых ссылок, включая три в самом `INDEX.md` и все ссылки в сгенерированном
накануне реестре модулей — то есть врал инструмент, написанный днём раньше.

Что проверяется и как разрешается путь:

  * **markdown-ссылки в теле** — относительно каталога самого файла. Ссылка, которая
    открывается только от корня репозитория, — тоже нарушение (класс `root-only`): в
    любом просмотрщике она мертва, хотя агент её найдёт.
  * **`related_docs:` и `covers:` во frontmatter** — от корня репозитория, от `docs/`
    либо однозначным именем файла: в репозитории живы все три написания. Имя, совпавшее
    с двумя файлами, — нарушение: такая ссылка ведёт неизвестно куда.
  * **`refs:` у задач спринтов** — так же. Идентификаторы (`T-769`, `ADR-053`) не наша
    забота: их связность не наша забота.

Не проверяются: внешние ссылки, якоря без пути, шаблоны с `*`, `<…>`, `{…}`, `$`, а также
всё, что ведёт в `specs/`. Спека — черновик текущей работы, она умирает после реализации и
намеренно не хранится в git. Ссылка на
умершую спеку — след процесса, а не битая ссылка, и чинить её нечем.

Храповик: `.claude/doc-links-baseline.json` — известные нарушения не блокируют
работу, но множество может только сокращаться. Новая битая ссылка — ошибка.

Escape hatch (на строке ссылки или предыдущей):
    <!-- noqa: doc-links <причина> -->
Без причины hatch не засчитывается.

Usage:
    python3 scripts/check_doc_links.py                 # полный обход
    python3 scripts/check_doc_links.py docs/a.md ...   # только указанные (pre-commit)
    python3 scripts/check_doc_links.py --json          # машинный вывод
    python3 scripts/check_doc_links.py --relax         # переустановить храповик

Exit: 0 — чисто, 1 — нарушения, 2 — системная ошибка.
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

import json
import pathlib
import re
import sys

import docs_lib as _docs_lib

# Корень проекта НЕ выводится из места скрипта: внутри плагина он лежит вне
# проверяемого репозитория. Ищем вверх от текущей директории по .git или
# .claude/doc-config.json.
ROOT = _docs_lib.find_root()
BASELINE_REL = ".claude/doc-links-baseline.json"

# Ссылка вида [текст](путь) или [текст](путь#якорь). Пробелы внутри пути не поддержаны
# намеренно: в репозитории их нет, а разбор <…> усложнил бы выражение без пользы.
MD_LINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s#]+)(?:#[^)\s]*)?\s*\)")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
LIST_FIELD = re.compile(r"^(related_docs|covers):\s*$", re.M)
LIST_ITEM = re.compile(r"^\s+-\s+(\S+)\s*$")
REFS_FIELD = re.compile(r"^\s+refs:\s*$")
HATCH = re.compile(r"<!--\s*noqa:\s*doc-links\s+(\S.*?)-->")

SKIP_PREFIX = ("http://", "https://", "mailto:", "tel:", "#", "data:")
SKIP_CHARS = set("*<>{}$|")
PATH_SUFFIXES = {
    ".md",
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".jsonl",
    ".sh",
    ".ts",
    ".tsx",
    ".csv",
    ".sql",
    ".toml",
    ".cfg",
    ".txt",
}

# Каталоги, внутрь которых не ходим: чужой код, копии репозитория, кеши.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".worktrees", "venv"}

_basename_cache: dict[pathlib.Path, dict[str, int]] = {}


def _skip(path: pathlib.Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def sources(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    """Файлы, чьи ссылки проверяются: документация, манифест, паспорта модулей, спринты.

    `docs/TAGS.md` исключён: это машинный индекс, и ссылки внутри его ячеек — цитаты из
    чужих документов, разрешаемые относительно них, а не относительно индекса. Что пути
    первой колонки существуют, проверяет `check_docs_index.py`.
    """
    candidates: list[pathlib.Path] = []
    candidates += sorted(
        p for p in (root / "docs").rglob("*.md") if p.name != "TAGS.md"
    )
    candidates += sorted(root.glob("*.md"))
    candidates += sorted((root / "services").rglob("AGENTS.md"))
    candidates += sorted((root / "tasks").glob("sprint-*.yaml"))

    found: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()
    for p in candidates:
        if _skip(p):
            continue
        # AGENTS.md в корне — симлинк на CLAUDE.md; разбирать один и тот же текст дважды
        # незачем, а нарушение из него было бы приписано не тому файлу.
        real = p.resolve()
        if real in seen:
            continue
        seen.add(real)
        found.append(p)
    return found


def _has_reason(line: str) -> bool:
    m = HATCH.search(line)
    return bool(m and m.group(1).strip())


def _hatched(lines: list[str], idx: int) -> bool:
    """Есть ли на этой или предыдущей строке hatch с непустой причиной.

    Hatch с предыдущей строки не засчитывается, если та строка сама содержит ссылку:
    иначе одно послабление молча накрывало бы соседнее нарушение — правило «на строке
    или предыдущей» превращается в «на две строки вперёд».
    """
    if 0 <= idx < len(lines) and _has_reason(lines[idx]):
        return True
    prev = idx - 1
    if 0 <= prev < len(lines) and _has_reason(lines[prev]):
        return not MD_LINK.search(lines[prev])
    return False


def _targets_from_frontmatter(text: str) -> list[tuple[str, int]]:
    """Пути из списочных полей frontmatter (`related_docs:`, `covers:`) с номером строки."""
    fm = FRONTMATTER.match(text)
    if not fm:
        return []
    out: list[tuple[str, int]] = []
    inside = False
    for i, line in enumerate(fm.group(1).splitlines(), start=2):  # +1 за строку `---`
        if LIST_FIELD.match(line):
            inside = True
            continue
        if inside:
            m = LIST_ITEM.match(line)
            if m:
                out.append((m.group(1).strip("\"'"), i))
                continue
            if line.strip() and not line.startswith((" ", "\t")):
                inside = False
    return out


def _targets_from_sprint(text: str) -> list[tuple[str, int]]:
    """Пути из `refs:` задач спринта. Разбор построчный: файл — многодокументный YAML."""
    out: list[tuple[str, int]] = []
    inside = False
    for i, line in enumerate(text.splitlines(), start=1):
        if REFS_FIELD.match(line):
            inside = True
            continue
        if inside:
            m = LIST_ITEM.match(line)
            if m:
                out.append((m.group(1).strip("\"'"), i))
                continue
            inside = False
    return out


def _checkable(target: str) -> bool:
    if not target or target.startswith(SKIP_PREFIX):
        return False
    if set(target) & SKIP_CHARS:
        return False
    if target.startswith(("specs/", "//")) or "/specs/" in target:
        return False
    return "/" in target or pathlib.PurePath(target).suffix in PATH_SUFFIXES


def _resolves_absolute(target: str, root: pathlib.Path) -> bool:
    """Разрешается ли путь от корня репозитория или от `docs/` — оба написания в ходу."""
    t = target.lstrip("/")
    return (root / t).exists() or (root / "docs" / t).exists()


def _basename_unique(target: str, root: pathlib.Path) -> bool:
    """Разрешается ли голое имя файла однозначно по всему `docs/`.

    Во frontmatter принято третье написание — одно имя файла без пути (`ADR-029-….md`).
    Для метаданных это допустимо: поле не рендерится ссылкой, а потребитель — инструмент,
    ищущий по имени. Условие одно: имя должно быть единственным. Совпало дважды — ссылка
    ведёт неизвестно куда, и это нарушение.
    """
    names = _basename_cache.get(root)
    if names is None:
        names = {}
        for p in (root / "docs").rglob("*.md"):
            if not _skip(p):
                names[p.name] = names.get(p.name, 0) + 1
        _basename_cache[root] = names
    return names.get(pathlib.PurePath(target).name, 0) == 1


def scan(path: pathlib.Path, root: pathlib.Path = ROOT) -> list[dict]:
    """Нарушения одного файла: файл, строка, цель, класс (`missing` / `root-only`)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [
            {"file": str(path), "line": 0, "target": str(exc), "kind": "unreadable"}
        ]

    lines = text.splitlines()
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    problems: list[dict] = []

    # (цель, номер строки, разрешать ли относительно файла)
    items: list[tuple[str, int, bool]] = []
    if path.suffix in {".yaml", ".yml"}:
        items += [(t, ln, False) for t, ln in _targets_from_sprint(text)]
    else:
        items += [(t, ln, False) for t, ln in _targets_from_frontmatter(text)]
        fm = FRONTMATTER.match(text)
        body_start = text[: fm.end()].count("\n") if fm else 0
        for i, line in enumerate(lines[body_start:], start=body_start + 1):
            items += [(m.group(1), i, True) for m in MD_LINK.finditer(line)]

    for target, line_no, relative in items:
        if not _checkable(target) or _hatched(lines, line_no - 1):
            continue
        if relative:
            if (path.parent / target).exists():
                continue
            kind = "root-only" if _resolves_absolute(target, root) else "missing"
        else:
            resolved = _resolves_absolute(target, root) or (
                "/" not in target and _basename_unique(target, root)
            )
            if resolved:
                continue
            kind = "missing"
        problems.append({"file": rel, "line": line_no, "target": target, "kind": kind})
    return problems


def key(problem: dict) -> str:
    """Ключ храповика без номера строки: сдвиг строки не должен воскрешать нарушение."""
    return f"{problem['file']}::{problem['target']}"


def load_baseline(root: pathlib.Path = ROOT) -> set[str]:
    path = root / BASELINE_REL
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"✗ храповик не разбирается: {path} — {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return set(data.get("known", []))


def write_baseline(problems: list[dict], root: pathlib.Path = ROOT) -> pathlib.Path:
    path = root / BASELINE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_note": "Известные нерабочие ссылки. Множество может только сокращаться: "
                "новая битая ссылка — ошибка. Переустановка — осознанным вызовом --relax. "
                "Основание: справочник doc-system",
                "known": sorted({key(p) for p in problems}),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    relax = "--relax" in sys.argv

    if args:
        paths = []
        for a in args:
            p = pathlib.Path(a)
            p = p if p.is_absolute() else ROOT / a
            if p.is_file() and not _skip(p) and p.name != "TAGS.md":
                paths.append(p)
    else:
        paths = sources()

    problems: list[dict] = []
    for p in paths:
        problems += scan(p)

    if relax:
        write_baseline(problems)
        print(f"храповик переустановлен: {len(problems)} известных нарушений")
        return 0

    known = load_baseline()
    fresh = [p for p in problems if key(p) not in known]

    if as_json:
        print(
            json.dumps(
                {"problems": problems, "fresh": fresh}, ensure_ascii=False, indent=2
            )
        )
        return 1 if fresh else 0

    if not fresh:
        tail = f" (в храповике ещё {len(problems)})" if problems else ""
        print(f"✓ новых битых ссылок нет{tail}")
        return 0

    print(f"✗ битых ссылок: {len(fresh)}", file=sys.stderr)
    for p in fresh:
        note = (
            "путь только от корня репозитория"
            if p["kind"] == "root-only"
            else "нет файла"
        )
        print(f"  {p['file']}:{p['line']} → {p['target']}  ({note})", file=sys.stderr)
    print(
        "\nПоправить путь, удалить ссылку или закрыть hatch'ем с причиной:\n"
        "  <!-- noqa: doc-links <причина> -->",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
