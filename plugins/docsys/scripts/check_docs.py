#!/usr/bin/env python3
"""Единая точка проверки документации — для check.sh, CI и хука.

Порядок проверок — от дешёвых и однозначных к дорогим и спорным:

  1. frontmatter — значения полей допустимы: статус и тип из словарей конфига,
                   дата это дата, документ лежит где предписано (гейт)
  2. registries  — реестры сошлись с документами (детерминированно, гейт)
  3. status      — статус в теле совпадает с frontmatter, active не ссылается
                   на движущееся или мёртвое (гейт)
  4. links       — ссылка открывается; храповик не даёт расти множеству битых (гейт)
  5. freshness   — документ отстал от кода, который описывает (отчёт, не гейт)

Frontmatter идёт первым намеренно: пока значения полей мусорные, остальные
проверки рассуждают о мусоре — для них `status: activ` просто незнакомая строка.

Свежесть по умолчанию не роняет проверку: отставание — повод посмотреть, а не
запрет коммита. `--strict` делает гейтом и её.

Usage:
    python3 check_docs.py                 # всё, свежесть отчётом
    python3 check_docs.py --strict        # свежесть тоже гейт
    python3 check_docs.py --only links    # одна проверка
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import docs_lib as lib  # noqa: E402

# Порядок словаря — порядок прогона: dict сохраняет вставку.
CHECKS = {
    "frontmatter": (["check_frontmatter.py"], True),
    "registries": (["rebuild_registries.py", "--check"], True),
    "status": (["check_doc_status.py"], True),
    "links": (["check_doc_links.py"], True),
    "freshness": (["check_doc_freshness.py"], False),
}


def run(root: pathlib.Path, argv: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(HERE / argv[0]), *argv[1:]],
        cwd=root, capture_output=True, text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--strict", action="store_true", help="свежесть тоже гейт")
    ap.add_argument("--only", choices=sorted(CHECKS), help="одна проверка")
    args = ap.parse_args()

    root = (args.root or lib.find_root()).resolve()
    names = [args.only] if args.only else list(CHECKS)

    failed: list[str] = []
    for name in names:
        argv, gating = CHECKS[name]
        if name == "freshness" and args.strict:
            argv, gating = [*argv, "--strict"], True
        code, out = run(root, argv)
        ok = code == 0
        mark = "✓" if ok else ("✗" if gating else "!")
        print(f"{mark} {name}")
        if out:
            print("\n".join(f"    {l}" for l in out.splitlines()))
        if not ok and gating:
            failed.append(name)

    print()
    if failed:
        print(f"документация: не пройдено — {', '.join(failed)}")
        return 1
    print("документация: проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
