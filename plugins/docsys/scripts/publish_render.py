#!/usr/bin/env python3
"""Что выгружать во внешний сервис и в каком виде — без сети.

Отбор документов, имя, шапка, переписывание ссылок, папка, хеш. Всё здесь
детерминировано и не знает, куда текст уйдёт: поставщик (`gdrive.py`) получает
готовый Markdown. Второй поставщик заменил бы только его.

Принцип выгрузки: источник правды — git. Документ во внешнем сервисе —
отображение, перезаписывается целиком; обратно ничего не идёт.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import posixpath
import re
from typing import Any

import docs_lib as lib

DEFAULT_SKIP = ["archived"]

# Метка перед именем файла: по ней в списке папки видно, на что нельзя опираться.
LABELS = {
    "active": "",
    "draft": "[черновик] ",
    "deprecated": "[устарел] ",
    "superseded": "[заменён] ",
    "archived": "[архив] ",
}
LABEL_NO_STATUS = "[без статуса] "

WARNINGS = {
    "draft": "⚠ Черновик: опираться нельзя.",
    "deprecated": "⚠ Устарел: больше не применяется.",
    "superseded": "⚠ Заменён другим документом: опираться нельзя.",
    "archived": "⚠ В архиве: сохранён ради истории.",
}
WARNING_NO_STATUS = "⚠ Статус не указан: опираться нельзя."

NOTICE = (
    "Документ выгружается из репозитория автоматически. "
    "Правки здесь будут затёрты — оставляйте комментарии."
)

# [текст](цель) и ![alt](цель), с необязательным заголовком "…".
LINK = re.compile(r'(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
FENCE = re.compile(r"^\s*(```|~~~)")
EXTERNAL = ("http://", "https://", "mailto:", "tel:", "ftp://", "data:")


class Doc:
    """Документ, подлежащий выгрузке, со всем, что о нём нужно знать."""

    def __init__(self, rel: str, path: pathlib.Path, text: str):
        self.rel = rel
        self.path = path
        self.text = text
        self.fm: dict[str, Any] = lib.parse_frontmatter(text)
        st = self.fm.get("status")
        self.status: str | None = st if isinstance(st, str) and st else None
        self.title = _title(self.fm, text, rel)
        self.name = (LABELS.get(self.status, "") if self.status else LABEL_NO_STATUS) + self.title
        self.folder = [p for p in posixpath.dirname(rel).split("/") if p]
        self.doc_id = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:24]


def _title(fm: dict[str, Any], text: str, rel: str) -> str:
    t = fm.get("title")
    if isinstance(t, str) and t.strip():
        return t.strip()
    for line in body_of(text).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return pathlib.PurePosixPath(rel).stem


def body_of(text: str) -> str:
    m = lib.FRONTMATTER.match(text)
    return text[m.end():] if m else text


def publish_cfg(cfg: dict[str, Any]) -> dict[str, Any] | None:
    p = cfg.get("publish")
    return p if isinstance(p, dict) and p else None


def selected(rel: str, status: str | None, pcfg: dict[str, Any]) -> bool:
    """Подлежит ли документ выгрузке по `include`, `exclude`, `skip_statuses`."""
    include = pcfg.get("include") or []
    exclude = pcfg.get("exclude") or []
    skip = pcfg.get("skip_statuses", DEFAULT_SKIP)
    if include and not any(lib._glob_full(rel, g) for g in include):
        return False
    if any(lib._glob_full(rel, g) for g in exclude):
        return False
    return status not in set(skip or [])


def all_docs(root: pathlib.Path, cfg: dict[str, Any]) -> dict[str, Doc]:
    """Все документы docsys — чтобы отличать ссылку на документ от ссылки на файл."""
    out: dict[str, Doc] = {}
    for rel, path in lib.iter_docs(root, cfg):
        out[rel] = Doc(rel, path, path.read_text(encoding="utf-8", errors="replace"))
    return out


def plan(root: pathlib.Path, cfg: dict[str, Any]) -> list[Doc]:
    """Документы к выгрузке, по пути."""
    pcfg = publish_cfg(cfg) or {}
    return [d for d in all_docs(root, cfg).values() if selected(d.rel, d.status, pcfg)]


def doc_url(file_id: str) -> str:
    return f"https://docs.google.com/document/d/{file_id}/edit"


def header(doc: Doc) -> str:
    def f(name: str) -> str:
        return lib.field(doc.fm, name)

    status = doc.status or lib.MISSING
    lines = [
        f"> Статус: **{status}** · Тип: {f('type')} · Обновлено: {f('updated')} · "
        f"Источник: `{doc.rel}`"
    ]
    if doc.status != "active":
        lines.append("> " + (WARNINGS.get(doc.status, WARNING_NO_STATUS) if doc.status
                             else WARNING_NO_STATUS))
    lines.append("> " + NOTICE)
    # Пустая строка цитаты между абзацами: иначе Markdown склеит их в один.
    return "\n>\n".join(lines) + "\n"


def resolve(target: str, doc_rel: str) -> str | None:
    """Путь цели от корня репозитория; None — цель вне репозитория."""
    path = target.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return None
    if path.startswith("/"):
        joined = path.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(doc_rel), path)
    norm = posixpath.normpath(joined)
    if norm.startswith("../") or norm == ".." or os.path.isabs(norm):
        return None
    return norm


def rewrite_links(body: str, doc: Doc, ids: dict[str, str]) -> str:
    """Ссылки на выгружаемые документы — в сервис; на прочие файлы — текст с путём.

    `ids` — путь документа от корня → ID его копии. Документ без ID (ещё не
    выгружен или не выгружается) — для читателя то же, что файл репозитория:
    открыть его по ссылке нельзя, поэтому ссылки нет, а путь назван.
    """
    out_lines = []
    in_fence = False
    for line in body.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        # Нечётные куски между обратными кавычками — код, его не трогаем.
        parts = line.split("`")
        for i in range(0, len(parts), 2):
            parts[i] = LINK.sub(lambda m: _link(m, doc, ids), parts[i])
        out_lines.append("`".join(parts))
    return "\n".join(out_lines)


def _link(m: re.Match, doc: Doc, ids: dict[str, str]) -> str:
    bang, text, target = m.group(1), m.group(2), m.group(3)
    if target.startswith(EXTERNAL) or target.startswith("#"):
        return m.group(0)
    rel = resolve(target, doc.rel)
    shown = rel or target
    if bang:
        return f"[изображение: {shown}]"
    if rel and rel in ids:
        return f"[{text}]({doc_url(ids[rel])})"
    return f"{text} (в репозитории: {shown})"


def render(doc: Doc, ids: dict[str, str]) -> str:
    return header(doc) + "\n" + rewrite_links(body_of(doc.text).lstrip("\n"), doc, ids)


def content_hash(name: str, text: str) -> str:
    return hashlib.sha256((name + "\n" + text).encode("utf-8")).hexdigest()[:32]
