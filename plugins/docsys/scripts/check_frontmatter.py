#!/usr/bin/env python3
"""Допустимость значений во frontmatter — сверка с конфигом проекта.

Остальные проверки смотрят на согласованность и связность: статус в теле против
frontmatter, разрешимость ссылок, актуальность реестров. Ни одна не спрашивает,
осмысленно ли само значение. Из-за этого `status: activ` вместо `active` или
выдуманный `type` проходили молча и попадали в реестр как факт.

Цена опечатки не косметическая: `status` решает, можно ли на документ опираться,
и по нему же отбирают документы фильтром. Значение вне словаря выпадает из любой
выборки, не выглядя при этом сломанным.

Проверяется:

  1. обязательные поля на месте;
  2. `status`, `tier`, `type` — из словарей конфига;
  3. `updated` — дата ГГГГ-ММ-ДД и не из будущего;
  4. `summary` непустой и содержательный;
  5. `tags` — список, а не строка;
  6. документ лежит там, где предписывает `path` его типа.

Usage:
    python3 check_frontmatter.py               # все документы
    python3 check_frontmatter.py docs/a.md     # выборочно
    python3 check_frontmatter.py --root DIR
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import docs_lib as lib  # noqa: E402

REQUIRED = ("title", "type", "status", "updated")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SUMMARY_MIN = 20

# Отвод — как у остальных проверок: с обязательной причиной.
HATCH = re.compile(r"<!--\s*noqa:\s*doc-frontmatter\s+(\S.*?)-->")

# Плейсхолдеры шаблонов. Документ с ними не заполнен, а не сломан, — но в
# реестре он неотличим от настоящего, поэтому это нарушение.
PLACEHOLDER = re.compile(r"\[(TITLE|DATE|SUMMARY|TIER|NNN)\b[^\]]*\]")


class Finding:
    def __init__(self, path: str, field: str, msg: str, fix: str):
        self.path, self.field, self.msg, self.fix = path, field, msg, fix

    def __str__(self) -> str:
        return f"  {self.path} [{self.field}] {self.msg}\n      → {self.fix}"


def type_names(cfg: dict) -> list[str]:
    return [t["name"] for t in cfg.get("types", []) if isinstance(t, dict) and "name" in t]


def expected_dirs(cfg: dict) -> dict[str, str]:
    """Каталог, предписанный типу его `path`, без файловой части и плейсхолдеров."""
    out: dict[str, str] = {}
    for t in cfg.get("types", []):
        if not isinstance(t, dict) or "name" not in t or "path" not in t:
            continue
        parent = pathlib.PurePosixPath(t["path"]).parent.as_posix()
        if "{" not in parent:
            out[t["name"]] = parent
    return out


def check_doc(rel: str, text: str, cfg: dict, today: datetime.date) -> list[Finding]:
    if HATCH.search(text):
        return []

    fm = lib.parse_frontmatter(text)
    if not fm:
        return [Finding(rel, "frontmatter", "нет блока frontmatter",
                        "добавить блок --- ... --- в начало файла")]

    out: list[Finding] = []

    for name in REQUIRED:
        if name not in fm or fm[name] in ("", []):
            out.append(Finding(rel, name, "обязательное поле отсутствует",
                               f"добавить {name}: во frontmatter"))

    statuses = cfg.get("statuses", [])
    st = fm.get("status")
    if st and statuses and st not in statuses:
        out.append(Finding(rel, "status", f"значение {st!r} вне словаря",
                           f"одно из: {', '.join(statuses)}"))

    tiers = cfg.get("tiers", [])
    tier = fm.get("tier")
    if tier and tiers and tier not in tiers:
        out.append(Finding(rel, "tier", f"значение {tier!r} вне словаря",
                           f"одно из: {', '.join(tiers)}, либо убрать поле"))

    names = type_names(cfg)
    tp = fm.get("type")
    if tp and names and tp not in names:
        out.append(Finding(rel, "type", f"значение {tp!r} не объявлено в конфиге",
                           f"одно из: {', '.join(names)}, либо добавить тип в .claude/doc-config.json"))

    upd = fm.get("updated")
    if upd:
        if not isinstance(upd, str) or not DATE.match(upd):
            out.append(Finding(rel, "updated", f"{upd!r} — не дата ГГГГ-ММ-ДД",
                               "записать как 2026-09-19"))
        else:
            try:
                d = datetime.date.fromisoformat(upd)
            except ValueError:
                out.append(Finding(rel, "updated", f"{upd!r} — несуществующая дата",
                                   "проверить месяц и число"))
            else:
                if d > today:
                    out.append(Finding(rel, "updated", f"{upd} из будущего",
                                       "поставить сегодняшнюю дату"))

    summary = fm.get("summary")
    if summary is None:
        out.append(Finding(rel, "summary", "поля нет",
                           "добавить: по summary решают, открывать документ или нет"))
    elif isinstance(summary, str) and len(summary.strip()) < SUMMARY_MIN:
        out.append(Finding(rel, "summary", f"короче {SUMMARY_MIN} символов — это тема, а не содержание",
                           "написать, что внутри и что зафиксировано"))

    tags = fm.get("tags")
    if tags is not None and not isinstance(tags, list):
        out.append(Finding(rel, "tags", "не список",
                           "записать как [тег, тег] или блочным списком"))

    dirs = expected_dirs(cfg)
    if tp in dirs:
        actual = pathlib.PurePosixPath(rel).parent.as_posix()
        if actual != dirs[tp]:
            out.append(Finding(rel, "path", f"тип {tp!r} размещается в {dirs[tp]}/, а файл в {actual}/",
                               f"перенести в {dirs[tp]}/ либо поправить тип"))

    m = PLACEHOLDER.search(text)
    if m:
        out.append(Finding(rel, "шаблон", f"остался плейсхолдер {m.group(0)}",
                           "заполнить или удалить документ"))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", help="конкретные .md; по умолчанию все под docs_dir")
    ap.add_argument("--root", type=pathlib.Path, default=None)
    args = ap.parse_args()

    root = (args.root or lib.find_root()).resolve()
    cfg = lib.load_config(root)
    today = datetime.date.today()

    if args.paths:
        pairs = []
        for a in args.paths:
            p = (root / a).resolve() if not pathlib.Path(a).is_absolute() else pathlib.Path(a)
            if p.is_file():
                pairs.append((p.relative_to(root).as_posix(), p))
    else:
        pairs = list(lib.iter_docs(root, cfg))

    if not pairs:
        print("frontmatter: документов не найдено")
        return 0

    findings: list[Finding] = []
    for rel, path in pairs:
        findings += check_doc(rel, path.read_text(encoding="utf-8", errors="replace"), cfg, today)

    if not findings:
        print(f"frontmatter: OK ({len(pairs)} документов)")
        return 0

    print(f"frontmatter: нарушений {len(findings)} в {len({f.path for f in findings})} "
          f"из {len(pairs)} документов")
    for f in findings:
        print(f)
    print("\n  Отвод с причиной: <!-- noqa: doc-frontmatter <почему> -->")
    return 1


if __name__ == "__main__":
    sys.exit(main())
