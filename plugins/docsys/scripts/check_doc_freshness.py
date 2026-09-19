#!/usr/bin/env python3
"""Отставание документа от кода, который он описывает — сверка расхождения.

Единственный механический способ поймать документ, который **не врёт по форме, но
описывает прошлое**. Именно так разошлись манифест и его копия (семь недель), карта
сервисов и `docker-compose.yml`, раздел «Recent Changes» и ветка, которую он описывает.
Ни проверка ссылок, ни статус-дисциплина такого не видят: документ цел, ссылки живы,
статус `active`.

Что с чем сравнивается — обе стороны факты из git, а не заявленные даты:

  * **дата документа** — последний коммит, тронувший файл;
  * **дата предмета** — последний коммит, тронувший то, что документ описывает.

Откуда берётся предмет:

  * `AGENTS.md` в каталоге модуля покрывает **свой каталог** — объявлять нечего, это
    следует из его места;
  * любой другой документ объявляет предмет сам, полем `related_code:` во frontmatter.
    Поле не заводилось под эту сверку — оно уже стоит у 138 документов; заводить рядом
    второе значило бы развести источники. Документ без него для сверки невидим, и это
    честнее ложной тревоги по догадке о том, что он описывает.

Отчёт, не гейт: отставание — повод посмотреть, а не запрет коммита. `--strict` делает
превышение порога ошибкой; порог — `max_lag_days` в `.claude/doc-freshness.json`.

Usage:
    python3 scripts/check_doc_freshness.py           # отчёт (только отставшие)
    python3 scripts/check_doc_freshness.py --all     # весь список, включая свежие
    python3 scripts/check_doc_freshness.py --json    # машинный вывод
    python3 scripts/check_doc_freshness.py --strict  # превышение порога = ошибка

Exit: 0 — отчёт напечатан, 1 — --strict и есть отставшие, 2 — системная ошибка.
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

import datetime as dt
import json
import pathlib
import re
import subprocess
import sys

import docs_lib as _docs_lib

# Корень проекта НЕ выводится из места скрипта: внутри плагина он лежит вне
# проверяемого репозитория. Ищем вверх от текущей директории по .git или
# .claude/doc-config.json.
ROOT = _docs_lib.find_root()
CONFIG_REL = ".claude/doc-freshness.json"

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
COVERS = re.compile(r"^related_code:\s*$", re.M)
ITEM = re.compile(r"^\s+-\s+(\S+)\s*$")
SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv"}


def load_config(root: pathlib.Path = ROOT) -> dict:
    path = root / CONFIG_REL
    if not path.is_file():
        return {"max_lag_days": 30}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"✗ конфигурация не разбирается: {path} — {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def last_commit(paths: list[str], root: pathlib.Path = ROOT) -> dt.datetime | None:
    """Дата последнего коммита, тронувшего любой из путей. None — путей нет в истории."""
    if not paths:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%cI", "--", *paths],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"✗ git недоступен: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    return dt.datetime.fromisoformat(out) if out else None


def _scalars(path: pathlib.Path) -> dict[str, str]:
    """Скалярные поля frontmatter, нужные для отбора: `type`, `status`."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = FRONTMATTER.match(text)
    if not fm:
        return {}
    out = {}
    for m in re.finditer(r"^(type|status):\s*(\S.*)$", fm.group(1), re.M):
        out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def declared_covers(path: pathlib.Path) -> list[str]:
    """Пути из поля `related_code:` во frontmatter документа."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = FRONTMATTER.match(text)
    if not fm:
        return []
    lines = fm.group(1).splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if COVERS.match(line):
            inside = True
            continue
        if inside:
            m = ITEM.match(line)
            if m:
                out.append(m.group(1).strip("\"'"))
                continue
            if line.strip():
                break
    return out


def subjects(cfg: dict, root: pathlib.Path = ROOT) -> list[dict]:
    """Пары «документ → что он описывает»: паспорта модулей и документы с `related_code:`."""
    out: list[dict] = []

    # Паспорт и README описывают один и тот же каталог с разных сторон: первый — границы,
    # второй — устройство. Устареть могут оба, и мерить их надо одинаково. README попал
    # сюда 10.09.2026 после замера: из 18 модульных README 12 отставали от своего кода,
    # медиана 37 дней, максимум 113 — и не видела этого ни одна проверка.
    doc_files = cfg.get("doc_files") or ["AGENTS.md", "README.md"]
    passports: list[pathlib.Path] = []
    for rel_root in cfg.get("passport_roots", []):
        base = root / rel_root
        if not base.is_dir():
            continue
        for name in doc_files:
            passports += [
                p
                for p in sorted(base.rglob(name))
                if not any(part in SKIP_DIRS for part in p.parts)
            ]
    passports.sort()

    for passport in passports:
        own = passport.parent
        # Паспорт описывает то, чего не описывают паспорта вложенных модулей. Иначе
        # верхний (`services/AGENTS.md`) считался бы отставшим от любой правки в дереве
        # и вечно горел бы, ничего не сообщая.
        covers = [str(own.relative_to(root))]
        covers += [
            f":(exclude){child.parent.relative_to(root)}/*"
            for child in passports
            if child != passport and child.parent != own and own in child.parents
        ]
        out.append(
            {
                "doc": str(passport.relative_to(root)),
                "covers": covers,
                "source": "паспорт модуля",
            }
        )

    skip_types = set(cfg.get("skip_types", []))
    skip_statuses = set(cfg.get("skip_statuses", []))
    for p in sorted((root / "docs").rglob("*.md")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        # Документ, записывающий момент, устареть не может: исследование, аудит и
        # отменённое решение описывают прошлое по своей природе, и требовать от них
        # свежести значит завалить отчёт тем, что чинить нечем.
        meta = _scalars(p)
        if meta.get("type") in skip_types or meta.get("status") in skip_statuses:
            continue
        covers = declared_covers(p)
        if covers:
            out.append(
                {
                    "doc": str(p.relative_to(root)),
                    "covers": covers,
                    "source": "related_code:",
                }
            )
    return out


def audit(cfg: dict, root: pathlib.Path = ROOT) -> dict:
    max_lag = int(cfg.get("max_lag_days", 30))
    rows = []
    for item in subjects(cfg, root):
        doc_date = last_commit([item["doc"]], root)
        # Каталог документа исключается из предмета: правка самого паспорта не должна
        # считаться изменением модуля, иначе документ вечно свеж относительно себя.
        code_paths = [c for c in item["covers"] if c != item["doc"]]
        code_date = last_commit(code_paths, root)
        if not doc_date or not code_date:
            rows.append({**item, "lag_days": None, "note": "нет истории"})
            continue
        lag = (code_date - doc_date).days
        rows.append(
            {
                **item,
                "doc_date": doc_date.date().isoformat(),
                "code_date": code_date.date().isoformat(),
                "lag_days": lag,
            }
        )
    behind = [r for r in rows if (r.get("lag_days") or 0) > max_lag]
    return {"rows": rows, "behind": behind, "max_lag_days": max_lag}


def main() -> int:
    as_json = "--json" in sys.argv
    strict = "--strict" in sys.argv

    cfg = load_config()
    result = audit(cfg)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if strict and result["behind"] else 0

    tracked = len(result["rows"])
    if not tracked:
        print(
            "Под сверкой свежести нет ни одного документа: паспортов нет, "
            "поле related_code: никем не проставлено"
        )
        return 0

    print(
        f"Под сверкой документов: {tracked}; отстают дольше "
        f"{result['max_lag_days']} дн.: {len(result['behind'])}"
    )

    # По умолчанию печатаются только отставшие, и не все: отчёт на сто строк в каждом
    # прогоне CI никто не читает, а значит он ничего не сообщает.
    show_all = "--all" in sys.argv
    shown = sorted(
        result["rows"] if show_all else result["behind"],
        key=lambda x: -(x.get("lag_days") or 0),
    )
    limit = len(shown) if show_all else 15
    for r in shown[:limit]:
        if r.get("lag_days") is None:
            print(f"  · {r['doc']}: {r.get('note', 'нет данных')}")
            continue
        mark = "✗" if r in result["behind"] else "✓"
        print(
            f"  {mark} {r['doc']}: код от {r['code_date']}, документ от "
            f"{r['doc_date']} (отставание {r['lag_days']} дн.)"
        )
    if len(shown) > limit:
        print(f"  … ещё {len(shown) - limit}; весь список — --all или --json")

    sys.stdout.flush()
    if result["behind"] and strict:
        print(
            f"\n✗ --strict: отставших документов {len(result['behind'])}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
