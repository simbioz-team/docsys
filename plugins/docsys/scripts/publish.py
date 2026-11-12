#!/usr/bin/env python3
"""Выгрузка документов в Google Drive — поток в одну сторону, git → Drive.

Источник правды — git. Drive — отображение: документ перезаписывается целиком,
обратно ничего не идёт. Сотрудники без доступа к репозиторию читают,
скачивают и комментируют; правки в Drive затрёт следующая выгрузка.

Связь «документ ↔ файл Drive» — в свойствах файла (`docsys_root`, `docsys_id`,
`docsys_hash`), а не в локальном файле состояния: две рабочие копии
(worktree) иначе разошлись бы. Выгружается только то, у чего изменился хеш.

Usage:
    python3 publish.py                     # полная выгрузка + архивирование
    python3 publish.py --dry-run           # что будет сделано, без записи
    python3 publish.py --path docs/a.md    # один документ (так зовёт хук)
    python3 publish.py status              # сколько копий отстаёт от репозитория
    python3 publish.py auth --client-id ID --client-secret SECRET

Exit: 0 — готово (или выгрузка не настроена), 1 — ошибка выгрузки,
      2 — не настроено то, без чего нельзя (нет входа, нет folder_id).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import hashlib
import pathlib
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import docs_lib as lib  # noqa: E402
import gdrive  # noqa: E402
import publish_render as pr  # noqa: E402

ARCHIVE = "Архив"
LOCK_WAIT = 60


# ---------------------------------------------------------------------------
# Служебные файлы: журнал, итог, блокировка — вне рабочего дерева
# ---------------------------------------------------------------------------


def state_dir(root: pathlib.Path) -> pathlib.Path:
    """Каталог служебных файлов: `.git/` рабочей копии, без git — кэш пользователя."""
    try:
        r = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=root,
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return pathlib.Path(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    d = pathlib.Path.home() / ".cache" / "docsys" / hashlib.sha256(str(root).encode()).hexdigest()[:16]
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_status(root: pathlib.Path, ok: bool, text: str) -> None:
    sd = state_dir(root)
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    # Сообщение о провале — одно на полосу провалов: повторный провал отметку
    # «показано» не снимает, иначе агент слышал бы об одном и том же на каждой
    # правке. Успех снимает — следующий провал снова будет новостью.
    status = sd / "docsys-publish.status"
    was_fail = status.exists() and status.read_text(encoding="utf-8").startswith("fail")
    if ok or not was_fail:
        (sd / "docsys-publish.status.shown").unlink(missing_ok=True)
    status.write_text(
        f"{'ok' if ok else 'fail'}\t{stamp}\t{text}\n", encoding="utf-8")
    with (sd / "docsys-publish.log").open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {'ok' if ok else 'FAIL'} {text}\n")


@contextlib.contextmanager
def project_lock(root: pathlib.Path):
    """Не больше одной выгрузки на рабочую копию: иначе новый документ задвоится."""
    path = state_dir(root) / "docsys-publish.lock"
    with path.open("w") as f:
        deadline = time.time() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise gdrive.DriveError(f"другая выгрузка идёт дольше {LOCK_WAIT} с")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Выгрузка
# ---------------------------------------------------------------------------


class Publisher:
    def __init__(self, root: pathlib.Path, cfg: dict[str, Any], drive: gdrive.Drive | None,
                 dry: bool = False):
        self.root, self.cfg, self.drive, self.dry = root, cfg, drive, dry
        self.pcfg = pr.publish_cfg(cfg) or {}
        self.root_id = str(self.pcfg.get("folder_id") or "")
        self.drive_id: str | None = None
        self.folders: dict[tuple[str, ...], str] = {(): self.root_id}
        self.actions: list[str] = []

    def log(self, msg: str) -> None:
        self.actions.append(msg)

    # --- папки -------------------------------------------------------------

    def folder(self, parts: list[str]) -> str:
        key: tuple[str, ...] = ()
        for name in parts:
            parent = self.folders[key]
            key = key + (name,)
            if key in self.folders:
                continue
            found = None if self.dry and parent.startswith("(новая)") else (
                self.drive.find_folder(name, parent, self.drive_id))
            if not found:
                if self.dry:
                    found = f"(новая){'/'.join(key)}"
                else:
                    found = self.drive.create_folder(name, parent)
                self.log(f"папка: {'/'.join(key)}/")
            self.folders[key] = found
        return self.folders[key]

    # --- основное ----------------------------------------------------------

    def tracked(self) -> dict[str, list[dict[str, Any]]]:
        by_id: dict[str, list[dict[str, Any]]] = {}
        for f in self.drive.tracked(self.root_id, self.drive_id):
            did = (f.get("properties") or {}).get("docsys_id")
            if did:
                by_id.setdefault(did, []).append(f)
        for files in by_id.values():
            files.sort(key=lambda f: f["id"])
        return by_id

    def run(self, only: list[str] | None = None) -> dict[str, int]:
        """Полная выгрузка (`only` пусто) или выгрузка указанных путей."""
        self.drive_id = self.drive.drive_id(self.root_id)
        docs = pr.all_docs(self.root, self.cfg)
        wanted = {d.rel: d for d in docs.values() if pr.selected(d.rel, d.status, self.pcfg)}
        by_id = self.tracked()
        archive_parts = [ARCHIVE]

        targets = sorted(wanted) if not only else sorted(r for r in only if r in wanted)

        # ID уже выгруженных копий — для ссылок. Копия в архиве тоже годится:
        # документ вернулся из архива — его копия вернётся следом.
        ids: dict[str, str] = {}
        for rel, d in wanted.items():
            if d.doc_id in by_id:
                ids[rel] = by_id[d.doc_id][0]["id"]

        counts = {"created": 0, "updated": 0, "moved": 0, "archived": 0, "same": 0}

        # Проход 1: у каждого документа к выгрузке появляется ID — до рендера ссылок.
        for rel in targets:
            d = wanted[rel]
            if rel in ids:
                continue
            parent = self.folder(d.folder)
            if self.dry:
                ids[rel] = f"(новый){rel}"
            else:
                f = self.drive.create_doc(d.name, parent, self._props(d, ""), self._desc(d), "")
                ids[rel] = f["id"]
                by_id[d.doc_id] = [f]
            counts["created"] += 1
            self.log(f"создать: {rel}")

        # Проход 2: содержимое, имя, место.
        for rel in targets:
            d = wanted[rel]
            text = pr.render(d, ids)
            h = pr.content_hash(d.name, text)
            parent = self.folder(d.folder)
            if self.dry and ids[rel].startswith("(новый)"):
                continue
            f = by_id[d.doc_id][0]
            parents = f.get("parents") or []
            move = parent not in parents
            same = (f.get("properties") or {}).get("docsys_hash") == h and f.get("name") == d.name
            if same and not move:
                counts["same"] += 1
                continue
            if move:
                counts["moved"] += 1
                self.log(f"переместить: {rel}")
            if not same:
                if f.get("properties", {}).get("docsys_hash"):
                    counts["updated"] += 1
                    self.log(f"обновить: {rel}")
            if self.dry:
                continue
            self.drive.update_doc(
                f["id"],
                name=d.name if not same else None,
                properties=self._props(d, h) if not same else None,
                description=self._desc(d) if not same else None,
                markdown=text if not same else None,
                add_parent=parent if move else None,
                remove_parents=[p for p in parents if p != parent] if move else None,
            )

        # Архив: копии без документа к выгрузке. При выгрузке одного пути —
        # только его копия: остальное дерево хук не трогает.
        live_ids = {d.doc_id for d in wanted.values()}
        if only:
            stale_ids = {pr.Doc(r, self.root / r, "").doc_id for r in only if r not in wanted}
        else:
            stale_ids = set(by_id) - live_ids
        # Задвоенные копии одного документа: первая живёт, прочие — в архив.
        extra = [f for did, fs in by_id.items() if did in live_ids for f in fs[1:]]
        if only:
            only_ids = {pr.Doc(r, self.root / r, "").doc_id for r in only}
            extra = [f for f in extra if f["properties"].get("docsys_id") in only_ids]
        to_archive = [f for did in sorted(stale_ids) for f in by_id.get(did, [])] + extra
        if to_archive:
            arch = self.folder(archive_parts)
            for f in to_archive:
                if arch in (f.get("parents") or []):
                    continue
                counts["archived"] += 1
                src = (f.get("description") or "").split(". Выгружено", 1)[0]
                self.log(f"в архив: {f.get('name')}" + (f" ({src})" if src else ""))
                if not self.dry:
                    self.drive.update_doc(f["id"], add_parent=arch,
                                          remove_parents=list(f.get("parents") or []))
        return counts

    def status(self) -> dict[str, list[str]]:
        """Отчёт: что в Drive отстаёт от репозитория. Ничего не пишет."""
        self.drive_id = self.drive.drive_id(self.root_id)
        docs = pr.all_docs(self.root, self.cfg)
        wanted = {d.rel: d for d in docs.values() if pr.selected(d.rel, d.status, self.pcfg)}
        by_id = self.tracked()
        ids = {rel: by_id[d.doc_id][0]["id"] for rel, d in wanted.items() if d.doc_id in by_id}
        out: dict[str, list[str]] = {"актуальны": [], "отстают": [], "нет в Drive": [],
                                     "к архивации": []}
        for rel, d in sorted(wanted.items()):
            if rel not in ids:
                out["нет в Drive"].append(rel)
                continue
            f = by_id[d.doc_id][0]
            h = pr.content_hash(d.name, pr.render(d, ids))
            key = "актуальны" if (f.get("properties") or {}).get("docsys_hash") == h else "отстают"
            out[key].append(rel)
        live = {d.doc_id for d in wanted.values()}
        for did, fs in sorted(by_id.items()):
            if did not in live:
                out["к архивации"] += [f"{f.get('name')} ({f.get('description', '')})" for f in fs]
        return out

    def _props(self, d: pr.Doc, h: str) -> dict[str, str]:
        return {"docsys_root": self.root_id, "docsys_id": d.doc_id, "docsys_hash": h}

    def _desc(self, d: pr.Doc) -> str:
        return f"Источник: {d.rel}. Выгружено docsys из репозитория; правки здесь затираются."


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _relpath(root: pathlib.Path, p: str) -> str:
    path = pathlib.Path(p)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return pathlib.PurePosixPath(p).as_posix()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", nargs="?", choices=["publish", "status", "auth"], default="publish")
    ap.add_argument("--root", type=pathlib.Path, default=None)
    ap.add_argument("--path", action="append", default=[], help="выгрузить только этот документ")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret")
    args = ap.parse_args(argv)

    if args.action == "auth":
        old = gdrive.load_credentials() or {}
        cid = args.client_id or old.get("client_id")
        secret = args.client_secret or old.get("client_secret")
        if not cid or not secret:
            print("нужны --client-id и --client-secret OAuth-клиента «Desktop app»", file=sys.stderr)
            return 2
        try:
            p = gdrive.authorize(cid, secret)
        except gdrive.DriveError as e:
            print(f"вход: {e}", file=sys.stderr)
            return 1
        print(f"вход выполнен, токен сохранён: {p}")
        return 0

    root = (args.root or lib.find_root()).resolve()
    cfg = lib.load_config(root)
    pcfg = pr.publish_cfg(cfg)
    if not pcfg:
        print("выгрузка не настроена: в .claude/doc-config.json нет раздела publish")
        return 0
    if pcfg.get("provider", "gdrive") != "gdrive":
        print(f"поставщик {pcfg.get('provider')!r} не поддерживается; есть только gdrive",
              file=sys.stderr)
        return 2
    if not pcfg.get("folder_id"):
        print("publish.folder_id не задан: ID корневой папки на общем диске", file=sys.stderr)
        return 2
    creds = gdrive.load_credentials()
    if not creds:
        print("нет входа в Google: python3 publish.py auth --client-id … --client-secret …",
              file=sys.stderr)
        return 2

    pub = Publisher(root, cfg, gdrive.Drive(creds), dry=args.dry_run)
    only = [_relpath(root, p) for p in args.path]
    what = ", ".join(only) if only else "все документы"
    try:
        if args.action == "status":
            rep = pub.status()
            for k, v in rep.items():
                print(f"{k}: {len(v)}")
                for x in v[:20]:
                    print(f"  {x}")
                if len(v) > 20:
                    print(f"  … ещё {len(v) - 20}")
            return 0
        with project_lock(root):
            counts = pub.run(only or None)
    except gdrive.DriveError as e:
        if not args.dry_run:
            write_status(root, False, f"{what}: {e}")
        print(f"выгрузка в Drive не удалась: {e}", file=sys.stderr)
        return 1

    summary = (f"создано {counts['created']}, обновлено {counts['updated']}, "
               f"перемещено {counts['moved']}, в архив {counts['archived']}, "
               f"без изменений {counts['same']}")
    if args.dry_run:
        print("пробный прогон, ничего не записано:")
        for a in pub.actions:
            print(f"  {a}")
        print(summary)
        return 0
    write_status(root, True, f"{what}: {summary}")
    for a in pub.actions:
        print(f"  {a}")
    print(f"выгрузка в Drive: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
