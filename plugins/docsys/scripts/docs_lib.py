#!/usr/bin/env python3
"""Общее для скриптов системы документации: конфиг и разбор frontmatter.

Конфиг — JSON, а не YAML, как было в первой версии системы.
Причина: YAML не в стандартной библиотеке, а зависимость PyYAML пришлось бы
ставить в каждый проект и в каждый CI. Конфиг маленький, JSON его держит.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

CONFIG_REL = ".claude/doc-config.json"

FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
SCALAR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)$")
LIST_ITEM = re.compile(r"^[ \t]+-[ \t]+(.*)$")

# Поля, которые реестры выводят по одной колонке на каждое.
COLUMNS = ["scope", "type", "tier", "status", "updated", "tags", "title", "summary"]
MISSING = "—"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": "1.0",
    "docs_dir": "docs",
    "registries": {
        "index": "docs/INDEX.md",
        "tags": "docs/TAGS.md",
        "index_limit": 0,
    },
    "statuses": ["active", "draft", "deprecated", "superseded", "archived"],
    "tiers": ["foundation", "feature", "feature-derived"],
    "types": [],
    "extra_roots": [],
    "exclude": ["docs/INDEX.md", "docs/TAGS.md"],
}


def find_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """Корень проекта: ближайший каталог вверх, где есть конфиг или .git."""
    cur = (start or pathlib.Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / CONFIG_REL).exists() or (d / ".git").exists():
            return d
    return cur


def load_config(root: pathlib.Path) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия
    p = root / CONFIG_REL
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Плоский разбор frontmatter: скаляры, инлайновые и блочные списки.

    Не полноценный YAML и не притворяется им. Чего не понял — того не выдумывает:
    поле просто отсутствует, и реестр покажет прочерк.
    """
    m = FRONTMATTER.match(text)
    if not m:
        return {}
    out: dict[str, Any] = {}
    key: str | None = None
    for raw in m.group(1).splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = LIST_ITEM.match(raw)
        if item and key:
            out.setdefault(key, [])
            if isinstance(out[key], list):
                out[key].append(_clean(item.group(1)))
            continue
        sm = SCALAR.match(raw)
        if not sm:
            continue
        key, value = sm.group(1), sm.group(2).strip()
        if value == "":
            out[key] = []          # блочный список начнётся со следующей строки
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            out[key] = [_clean(x) for x in inner.split(",") if x.strip()] if inner else []
        else:
            out[key] = _clean(value)
            key = None
    return {k: v for k, v in out.items() if v != []} | {
        k: v for k, v in out.items() if isinstance(v, list) and v
    }


def _clean(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def field(fm: dict[str, Any], name: str) -> str:
    """Значение поля для реестра. Отсутствует — прочерк, а не догадка."""
    v = fm.get(name)
    if v is None or v == "" or v == []:
        return MISSING
    if isinstance(v, list):
        if name == "tags":
            return " ".join(t if t.startswith("#") else f"#{t}" for t in v)
        return ", ".join(str(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ").strip()


def extra_globs(cfg: dict[str, Any]) -> list[str]:
    """Шаблоны дополнительных корней из `extra_roots`.

    Корень — шаблон файлов, а не каталог: в `specs/NNN-slug/` рядом со `spec.md`
    лежат план и задачи, и документами они не являются. Элемент — объект
    `{"glob": "..."}` или просто строка-шаблон.
    """
    out: list[str] = []
    for item in cfg.get("extra_roots") or []:
        g = item.get("glob") if isinstance(item, dict) else item
        if isinstance(g, str) and g.strip():
            out.append(g.strip().strip("/"))
    return out


def extra_bases(cfg: dict[str, Any]) -> list[str]:
    """Неизменная часть каждого шаблона: `specs/*/spec.md` → `specs`.

    Всё, что лежит под базой, но шаблону не отвечает, — рабочие материалы
    рядом с документами (`plan.md`, `tasks.md`): файлы, но не документы.
    """
    out: list[str] = []
    for g in extra_globs(cfg):
        parts: list[str] = []
        for part in pathlib.PurePosixPath(g).parts[:-1]:
            if any(c in part for c in "*?["):
                break
            parts.append(part)
        base = "/".join(parts)
        if base and base not in out:
            out.append(base)
    return out


def _excluded(rel: str, cfg: dict[str, Any]) -> bool:
    """Исключён ли путь шаблоном из `exclude`.

    Два разбора, объединением. `PurePosixPath.match` — прежний: сравнивает с
    правого края, и `**` в нём не спускается глубже одного каталога —
    `docs/fsi/**` пропускал `docs/fsi/a/b.md`. `_glob_full` — от корня, `**` —
    любая глубина. Объединение чинит `**`, не сужая того, что исключалось
    раньше: проект, который был зелёным, зелёным и останется.
    """
    excluded = {str(x) for x in cfg.get("exclude", [])}
    return rel in excluded or any(
        _glob_match(rel, pat) or _glob_full(rel, pat)
        for pat in excluded if "*" in pat
    )


def matches_extra(rel: str, cfg: dict[str, Any]) -> bool:
    """Отвечает ли путь от корня шаблону какого-нибудь дополнительного корня."""
    return any(_glob_full(rel, g) for g in extra_globs(cfg))


def is_doc(rel: str, cfg: dict[str, Any]) -> bool:
    """Документ ли это: под docs_dir или по шаблону доп. корня, и не исключён."""
    if not rel.endswith(".md") or _excluded(rel, cfg):
        return False
    return rel.startswith(cfg["docs_dir"].rstrip("/") + "/") or matches_extra(rel, cfg)


def is_material(rel: str, cfg: dict[str, Any]) -> bool:
    """Файл под базой доп. корня, который документом не является (план, задачи)."""
    if is_doc(rel, cfg):
        return False
    return any(rel.startswith(b + "/") for b in extra_bases(cfg))


def iter_docs(root: pathlib.Path, cfg: dict[str, Any]):
    """Документы под docs_dir и по шаблонам extra_roots, кроме исключённых.

    Отсортированы по пути. Без extra_roots — ровно прежний обход docs_dir.
    """
    found: dict[str, pathlib.Path] = {}
    docs_dir = root / cfg["docs_dir"]
    if docs_dir.is_dir():
        for p in docs_dir.rglob("*.md"):
            found[p.relative_to(root).as_posix()] = p
    for g in extra_globs(cfg):
        for p in root.glob(g):
            if p.is_file() and p.suffix == ".md":
                found.setdefault(p.relative_to(root).as_posix(), p)
    for p in sorted(found.values()):
        rel = p.relative_to(root).as_posix()
        if _excluded(rel, cfg):
            continue
        yield rel, p


def _glob_full(rel: str, pattern: str) -> bool:
    """Совпадение по шаблону целиком, от корня, а не с правого края пути.

    `PurePosixPath.match` сравнивает с конца: `a/specs/x/spec.md` отвечал бы
    шаблону `specs/*/spec.md`. Здесь `*` и `?` не переходят `/`, `**` — любое
    число каталогов.
    """
    return re.fullmatch(_glob_regex(pattern), rel) is not None


def _glob_regex(pattern: str) -> str:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def _glob_match(rel: str, pattern: str) -> bool:
    return pathlib.PurePosixPath(rel).match(pattern)


def default_scope(rel: str, cfg: dict[str, Any]) -> str:
    """Scope по пути, если во frontmatter его нет.

    Правило детерминированное: всё под docs_dir/meta/ — meta, остальное product.
    """
    meta_prefix = f"{cfg['docs_dir']}/meta/"
    return "meta" if rel.startswith(meta_prefix) else "product"
