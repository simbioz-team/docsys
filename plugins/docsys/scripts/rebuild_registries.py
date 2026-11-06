#!/usr/bin/env python3
"""Пересборка docs/INDEX.md и docs/TAGS.md из frontmatter документов.

Документы — под `docs_dir` и по шаблонам `extra_roots` (например, `specs/*/spec.md`).

Зачем скриптом, а не инструкцией агенту: реестр обязан отражать источник, а не
догадку о нём. Агент, перестраивающий таблицу вручную, подставляет правдоподобные
значения в пустые поля — и документ выглядит актуальным, не будучи им. Здесь
отсутствующее поле становится прочерком, всегда.

Второе следствие: реестры детерминированы. Одно и то же дерево документов даёт
побайтово одинаковый вывод, поэтому расхождение ловится сравнением, а не
вычиткой.

Usage:
    python3 rebuild_registries.py            # перестроить
    python3 rebuild_registries.py --check    # только сверить, код 1 при расхождении
    python3 rebuild_registries.py --root DIR
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import docs_lib as lib  # noqa: E402  — после правки sys.path


def collect(root: pathlib.Path, cfg: dict) -> list[dict]:
    rows = []
    for rel, path in lib.iter_docs(root, cfg):
        fm = lib.parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        row = {"path": rel}
        for col in lib.COLUMNS:
            row[col] = lib.field(fm, col)
        if row["scope"] == lib.MISSING:
            row["scope"] = lib.default_scope(rel, cfg)
        if row["title"] == lib.MISSING:
            row["title"] = _first_heading(path) or pathlib.PurePosixPath(rel).stem
        rows.append(row)
    return rows


def _first_heading(path: pathlib.Path) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# "):
            return line[2:].strip().replace("|", "\\|")
    return None


def render_tags(rows: list[dict]) -> str:
    head = "| path | " + " | ".join(lib.COLUMNS) + " |"
    sep = "|" + "---|" * (len(lib.COLUMNS) + 1)
    out = [
        "# TAGS.md — машинный индекс документации",
        "",
        "> Собирается скриптом `rebuild_registries.py` из frontmatter документов.",
        "> Руками не править: правка переживёт ровно до следующей пересборки.",
        "> Пустое поле — прочерк, а не догадка.",
        "",
        head,
        sep,
    ]
    for r in rows:
        out.append("| " + " | ".join([r["path"]] + [r[c] for c in lib.COLUMNS]) + " |")
    out.append("")
    return "\n".join(out)


def render_index(rows: list[dict], cfg: dict) -> str:
    docs_dir = cfg["docs_dir"]
    statuses = list(cfg["statuses"])

    # Счётчики по разделу верхнего уровня внутри docs_dir. Документ доп. корня
    # (`specs/001-a/spec.md`) считается в разделе по первому каталогу своего
    # пути — `specs`: спеки отдельной строкой, а не россыпью по номерам.
    sections: dict[str, dict[str, int]] = {}
    for r in rows:
        if r["path"].startswith(docs_dir + "/"):
            parts = r["path"][len(docs_dir) + 1:].split("/")
            section = parts[0] if len(parts) > 1 else "(корень)"
        else:
            section = r["path"].split("/")[0]
        bucket = sections.setdefault(section, {s: 0 for s in statuses} | {lib.MISSING: 0})
        bucket[r["status"] if r["status"] in bucket else lib.MISSING] += 1

    cols = statuses + [lib.MISSING]
    out = [
        "# Индекс документации",
        "",
        "> **Главный инструмент навигации.** Читайте этот файл первым перед любой",
        "> работой с документами.",
        ">",
        "> Порядок: `CLAUDE.md` (всегда) → `INDEX.md` (по задаче) → конкретные файлы",
        "> (только нужные).",
        ">",
        "> Собирается скриптом `rebuild_registries.py`. Руками не править.",
        "",
        "## Счётчики",
        "",
        "| Раздел | " + " | ".join(cols) + " | Всего |",
        "|" + "---|" * (len(cols) + 2),
    ]
    totals = {c: 0 for c in cols}
    for section in sorted(sections):
        b = sections[section]
        n = sum(b.values())
        for c in cols:
            totals[c] += b[c]
        out.append(f"| {section} | " + " | ".join(str(b[c]) for c in cols) + f" | {n} |")
    grand = sum(totals.values())
    out.append(
        "| **Итого** | " + " | ".join(f"**{totals[c]}**" for c in cols) + f" | **{grand}** |"
    )

    limit = int(cfg["registries"].get("index_limit") or 0)
    shown = rows if limit <= 0 else rows[:limit]

    out += [
        "",
        "## Документы",
        "",
        "| Документ | Тип | Tier | Статус | Summary | Теги |",
        "|---|---|---|---|---|---|",
    ]
    for r in shown:
        link = f"[{r['title']}]({_relative_to_index(r['path'], cfg)})"
        out.append(
            f"| {link} | {r['type']} | {r['tier']} | {r['status']} | {r['summary']} | {r['tags']} |"
        )
    if limit > 0 and len(rows) > limit:
        out += [
            "",
            f"> Показано {limit} из {len(rows)}. Полный список — в `{cfg['registries']['tags']}`.",
        ]
    out.append("")
    return "\n".join(out)


def _relative_to_index(doc_path: str, cfg: dict) -> str:
    """Ссылка от каталога INDEX.md, а не от корня: иначе она мертва в просмотрщике."""
    index_dir = pathlib.PurePosixPath(cfg["registries"]["index"]).parent
    try:
        return pathlib.PurePosixPath(doc_path).relative_to(index_dir).as_posix()
    except ValueError:
        # Документ вне каталога реестра — из доп. корня: `../specs/001-a/spec.md`.
        return pathlib.PurePosixPath(os.path.relpath(doc_path, index_dir.as_posix())).as_posix()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--check", action="store_true",
                    help="не писать, а сверить; код 1 при расхождении")
    args = ap.parse_args()

    root = (args.root or lib.find_root()).resolve()
    cfg = lib.load_config(root)
    rows = collect(root, cfg)

    if not rows:
        where = ", ".join([f"{cfg['docs_dir']}/", *lib.extra_globs(cfg)])
        print(f"документов не найдено под {where} — реестры не тронуты")
        return 0

    targets = {
        root / cfg["registries"]["tags"]: render_tags(rows),
        root / cfg["registries"]["index"]: render_index(rows, cfg),
    }

    drift = []
    for path, content in targets.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            drift.append(path.relative_to(root).as_posix())

    if args.check:
        if drift:
            print(f"реестры разошлись с документами: {', '.join(drift)}")
            print("что делать: python3 rebuild_registries.py")
            return 1
        print(f"реестры актуальны ({len(rows)} документов)")
        return 0

    for path, content in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if drift:
        print(f"перестроено ({len(rows)} документов): {', '.join(drift)}")
    else:
        print(f"реестры уже были актуальны ({len(rows)} документов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
