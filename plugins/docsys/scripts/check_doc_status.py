#!/usr/bin/env python3
"""Doc-status discipline check (governance gate).

Энфорсит три правила дисциплины документации, которые до сих пор были только
текстом в справочником системы документации, но никем не проверялись:

  CHECK 1 — frontmatter ↔ статус в теле.
    Статус в теле — секция `## Status` / `## Статус` либо строка
    `- **status:** <значение>` в начале тела, до первого подзаголовка (так
    пишут спеки). Значение MUST совпадать с frontmatter `status:`: ловит дрейф,
    когда обновили одно, а второе забыли. Строка, совпавшая с frontmatter, —
    предупреждение, не ошибка: статус живёт во frontmatter, копия в теле
    рано или поздно разойдётся.

  CHECK 2 — active-only citation.
    Документ со `status: active` и `type ∈ {adr, spec}` MUST ссылаться только
    на `active`-источники. Ссылка (frontmatter `related_docs:` или markdown-
    линк в теле) на документ со `status ∈ {draft, deprecated, superseded,
    archived}` → нарушение. Это правило «не строить ратифицированное решение
    на движущемся/мёртвом фундаменте».

  CHECK 3 — живой ADR объявляет tier.
    ADR со статусом `active` или `draft` MUST нести
    `tier: foundation|feature|feature-derived`. Поле отделяет решения уровня
    всего проекта от привязанных к одной фиче, и на этом отделении стоит связь
    корпуса решений с манифестом принципов. Замер 10.09.2026: поля не было у 25
    ADR, то есть фильтр не работал вовсе. Мёртвым решениям (superseded,
    archived, deprecated) tier не требуется: вопрос «действует ли правило для
    всего проекта» для отменённого решения пустой.

Escape hatch (per-line): `# noqa: doc-status-bypass <reason>` (в YAML-строке
`related_docs`) либо `<!-- noqa: doc-status-bypass <reason> -->` (в теле, на
строке ссылки или предыдущей). Без `<reason>` hatch не засчитывается.

Pre-commit передаёт ТОЛЬКО staged-файлы → существующий backlog (напр. 15
active-ADR, цитирующих draft ADR-005) не блокирует посторонние коммиты, а
всплывает по одному при правке файла. Полный отчёт: прогнать на `docs/`.

Usage:
  python scripts/check_doc_status.py docs/architecture/decisions/   # report
  python scripts/check_doc_status.py file1.md file2.md              # pre-commit
  python scripts/check_doc_status.py docs/ && echo OK                # CI gate
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import docs_lib as lib  # noqa: E402

# ============================================================================
# Configuration
# ============================================================================

# Канонические значения status (см. справочник doc-system).
KNOWN_STATUSES: frozenset[str] = frozenset(
    {"draft", "active", "deprecated", "superseded", "archived"}
)

#: Значения `tier` (CLAUDE.md, раздел языковой политики; критерий — doc-new §1.5).
KNOWN_TIERS: frozenset[str] = frozenset({"foundation", "feature", "feature-derived"})

#: Статусы, при которых ADR обязан объявить tier. Мёртвые решения не обязаны.
TIER_REQUIRED_STATUSES: frozenset[str] = frozenset({"active", "draft"})

# Статусы, которые НЕ годятся как источник для active-ADR/spec (CHECK 2).
NON_CITABLE_STATUSES: frozenset[str] = frozenset(
    {"draft", "deprecated", "superseded", "archived"}
)

# CHECK 2 применяется только к этим типам документов (ратифицируемый канон).
CITATION_GUARDED_TYPES: frozenset[str] = frozenset({"adr", "spec"})

# Per-line escape hatch marker.
BYPASS_MARKER = "doc-status-bypass"

# Body `## Status` / `## Статус` heading (любой уровень #).
_STATUS_HEADING_RE = re.compile(r"^#{1,6}\s+(?:status|статус)\s*$", re.IGNORECASE)

# Строка статуса в начале тела: `- **status:** active`, `**Статус**: draft`.
_STATUS_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?\*\*\s*(?:status|статус)\s*:?\s*\*\*\s*:?\s*(.+?)\s*$",
    re.IGNORECASE,
)

# Первое слово значения перед пояснением: `active — …`, `draft (ждёт ревью)`.
_LEADING_WORD_RE = re.compile(r"^\**([A-Za-z-]+)\**\s*(?:[—–\-(,;:]|$)")

# Подзаголовок, на котором «начало тела» кончается.
_SUBHEADING_RE = re.compile(r"^#{2,6}\s")

# Markdown inline link: [text](target). Захватываем target.
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# noqa с обязательным reason: `doc-status-bypass <непустой текст>`.
_BYPASS_RE = re.compile(rf"{BYPASS_MARKER}\s+\S")


# ============================================================================
# Frontmatter parsing (без внешних зависимостей — PyYAML не гарантирован)
# ============================================================================


class DocMeta:
    """Минимальный разбор frontmatter + кэш raw-строк документа."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.status: str | None = None
        self.type: str | None = None
        self.tier: str | None = None
        self.tier_line: int = 0
        self.body_status: str | None = None
        self.body_status_line: int = 0
        # Строка `- **status:** …` в начале тела.
        self.line_status: str | None = None
        self.line_status_line: int = 0
        # related_docs: list of (raw_target, line_no, has_bypass)
        self.related_docs: list[tuple[str, int, bool]] = []
        self.lines: list[str] = []
        self.has_frontmatter = False


def _norm_status(raw: str) -> str | None:
    """`**Draft**` / `"active"` / `Статус: draft` → `draft`; иначе None."""
    val = raw.strip().strip("*").strip().strip('"').strip("'").strip().lower()
    # Срезаем возможный префикс `status:` / `статус:` внутри строки тела.
    val = re.sub(r"^(status|статус)\s*[:\-]\s*", "", val).strip()
    return val if val in KNOWN_STATUSES else None


def parse_doc(path: Path) -> DocMeta | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    meta = DocMeta(path)
    meta.lines = text.splitlines()
    lines = meta.lines

    # --- Frontmatter block (между первыми двумя `---`) -----------------------
    fm_end = -1
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i
                break
    if fm_end == -1:
        return meta  # нет frontmatter — для CHECK 1 хватит body-Status

    meta.has_frontmatter = True
    in_related = False
    for idx in range(1, fm_end):
        raw = lines[idx]
        raw.strip()
        # Топ-левел ключи (без отступа).
        m_status = re.match(r"status\s*:\s*(.+)$", raw)
        if m_status and not raw.startswith((" ", "\t")):
            meta.status = _norm_status(m_status.group(1))
            in_related = False
            continue
        m_tier = re.match(r"tier\s*:\s*(.+)$", raw)
        if m_tier and not raw.startswith((" ", "\t")):
            meta.tier = m_tier.group(1).strip().strip('"').strip("'").lower()
            meta.tier_line = idx + 1
            in_related = False
            continue
        m_type = re.match(r"type\s*:\s*(.+)$", raw)
        if m_type and not raw.startswith((" ", "\t")):
            meta.type = m_type.group(1).strip().strip('"').strip("'").lower()
            in_related = False
            continue
        if re.match(r"related_docs\s*:", raw) and not raw.startswith((" ", "\t")):
            in_related = True
            continue
        if in_related:
            # Элемент списка `  - <target>` (возможен inline-комментарий подавления).
            m_item = re.match(r"\s*-\s*(.+)$", raw)
            if m_item:
                item = m_item.group(1).strip().strip('"').strip("'")
                has_bypass = bool(_BYPASS_RE.search(raw)) and "noqa" in raw
                # Отрезаем inline-комментарий от значения.
                target = item.split("#", 1)[0].strip().strip('"').strip("'")
                if target:
                    meta.related_docs.append((target, idx + 1, has_bypass))
                continue
            # Любой другой top-level ключ завершает список.
            if not raw.startswith((" ", "\t")):
                in_related = False

    # --- Body `## Status` секция (первая после frontmatter) ------------------
    for idx in range(fm_end + 1, len(lines)):
        if _STATUS_HEADING_RE.match(lines[idx]):
            # Значение — ближайшая непустая строка после заголовка.
            for j in range(idx + 1, min(idx + 5, len(lines))):
                if lines[j].strip():
                    norm = _norm_status(lines[j])
                    if norm:
                        meta.body_status = norm
                        meta.body_status_line = j + 1
                    break
            break

    # --- Строка `- **status:** …` в начале тела (до первого подзаголовка) -----
    for idx in range(fm_end + 1, len(lines)):
        if _SUBHEADING_RE.match(lines[idx]):
            break
        m_line = _STATUS_LINE_RE.match(lines[idx])
        if m_line:
            # `active — утверждена владельцем 16.10`: статус — первое слово
            # перед пояснением, иначе строка с комментарием проходила бы молча.
            value = m_line.group(1)
            m_word = _LEADING_WORD_RE.match(value)
            norm = _norm_status(value) or (_norm_status(m_word.group(1)) if m_word else None)
            if norm:
                meta.line_status = norm
                meta.line_status_line = idx + 1
            break

    return meta


# ============================================================================
# Violation record
# ============================================================================


class Violation:
    def __init__(self, *, path: Path, line: int, category: str, message: str) -> None:
        self.path = path
        self.line = line
        self.category = category
        self.message = message

    def format(self, repo_root: Path) -> str:
        try:
            disp = self.path.relative_to(repo_root)
        except ValueError:
            disp = self.path
        return f"{disp}:{self.line} [{self.category}] {self.message}"


# ============================================================================
# Citation resolution
# ============================================================================


def _strip_target(target: str) -> str:
    """`./ADR-018-...md#anchor` / `ADR-005:868` → путь без anchor/line."""
    target = target.strip()
    target = target.split("#", 1)[0]
    # `:NNN` line-suffix (но не `://` в URL).
    target = re.sub(r":\d+(-\d+)?$", "", target)
    return target.strip()


def resolve_citation(
    target: str, citing_file: Path, docs_root: Path, repo_root: Path
) -> Path | None:
    """Резолвит markdown/related_docs target в .md-файл под docs/. Иначе None."""
    if target.startswith(("http://", "https://", "mailto:")):
        return None
    clean = _strip_target(target)
    if not clean.endswith(".md"):
        return None
    candidates = [
        (citing_file.parent / clean),  # относительно файла-источника
        (docs_root / clean),  # относительно docs/
        (repo_root / clean),  # относительно repo root
    ]
    # `docs/architecture/...` при docs_root=<repo>/docs.
    if clean.startswith("docs/"):
        candidates.append(repo_root / clean)
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _line_or_prev_has_bypass(lines: list[str], lineno: int) -> bool:
    for cand in (lineno, lineno - 1):
        if 1 <= cand <= len(lines):
            ln = lines[cand - 1]
            if "noqa" in ln and _BYPASS_RE.search(ln):
                return True
    return False


# ============================================================================
# Per-file check
# ============================================================================

# Кэш статусов цитируемых документов, чтобы не парсить повторно.
_status_cache: dict[Path, str | None] = {}

# Предупреждения: печатаются, но проверку не роняют.
_warnings: list["Violation"] = []

# Конфиг и корень проекта — для различения документа и рабочего материала.
_cfg: dict = {}
_repo_root: Path | None = None


def _is_material(path: Path) -> bool:
    """План и задачи рядом со спекой: файлы, но не документы — статуса у них нет."""
    if _repo_root is None:
        return False
    try:
        rel = path.resolve().relative_to(_repo_root).as_posix()
    except ValueError:
        return False
    return lib.is_material(rel, _cfg)


def _cited_status(path: Path) -> str | None:
    if _is_material(path):
        return None
    if path not in _status_cache:
        m = parse_doc(path)
        _status_cache[path] = m.status if m else None
    return _status_cache[path]


def check_file(path: Path, docs_root: Path, repo_root: Path) -> list[Violation]:
    meta = parse_doc(path)
    if meta is None:
        return []
    violations: list[Violation] = []

    # --- CHECK 1: frontmatter ↔ body `## Status` -----------------------------
    if meta.status and meta.body_status and meta.status != meta.body_status:
        violations.append(
            Violation(
                path=path,
                line=meta.body_status_line,
                category="status_mismatch",
                message=(
                    f'frontmatter status="{meta.status}" но body `## Status` = '
                    f'"{meta.body_status}" — выровнять (обновить оба).'
                ),
            )
        )

    if meta.status and meta.line_status and meta.status == meta.line_status:
        _warnings.append(
            Violation(
                path=path,
                line=meta.line_status_line,
                category="status_copy",
                message=(
                    "строка статуса в теле дублирует frontmatter — убрать: "
                    "копия разойдётся при следующей смене статуса."
                ),
            )
        )
    if meta.status and meta.line_status and meta.status != meta.line_status:
        violations.append(
            Violation(
                path=path,
                line=meta.line_status_line,
                category="status_mismatch",
                message=(
                    f'frontmatter status="{meta.status}" но строка статуса в теле = '
                    f'"{meta.line_status}" — убрать строку: статус живёт во frontmatter.'
                ),
            )
        )

    # --- CHECK 3: живой ADR обязан объявить tier -----------------------------
    # Замер 10.09.2026: у 25 ADR поля не было вовсе, и фильтр «фундаментальные
    # решения» по нему не работал — а на этом фильтре стоит связь ADR с манифестом
    # принципов. Мёртвым решениям tier не нужен: поле отвечает на вопрос «действует
    # ли правило для всего проекта», а для отменённого вопрос пустой.
    if meta.type == "adr" and meta.status in TIER_REQUIRED_STATUSES:
        if meta.tier is None:
            violations.append(
                Violation(
                    path=path,
                    line=1,
                    category="tier_missing",
                    message=(
                        "ADR со статусом " + str(meta.status) + " обязан объявить "
                        "`tier: foundation|feature|feature-derived`. Критерий — три теста P "
                        "в .claude/commands/doc-new.md §1.5: стабильность годами, "
                        "применимость к любой фиче, норма а не описание сделанного."
                    ),
                )
            )
        elif meta.tier not in KNOWN_TIERS:
            violations.append(
                Violation(
                    path=path,
                    line=meta.tier_line,
                    category="tier_unknown",
                    message=(
                        'tier="'
                        + meta.tier
                        + '" вне словаря '
                        + "|".join(sorted(KNOWN_TIERS))
                        + "."
                    ),
                )
            )

    # --- CHECK 2: active-only citation (только adr/spec в статусе active) -----
    if meta.status == "active" and (meta.type in CITATION_GUARDED_TYPES):
        # 2a. frontmatter related_docs.
        for target, lineno, has_bypass in meta.related_docs:
            if has_bypass:
                continue
            resolved = resolve_citation(target, path, docs_root, repo_root)
            if resolved is None or resolved == path.resolve():
                continue
            st = _cited_status(resolved)
            if st in NON_CITABLE_STATUSES:
                violations.append(
                    Violation(
                        path=path,
                        line=lineno,
                        category="cites_non_active",
                        message=(
                            f"active {meta.type} ссылается (related_docs) на "
                            f'"{target}" со status="{st}". Промоутить источник в '
                            f"active ИЛИ `# noqa: doc-status-bypass <reason>`."
                        ),
                    )
                )
        # 2b. body markdown-ссылки.
        body_start = 0
        if meta.has_frontmatter:
            # пропускаем frontmatter — индекс второго '---'
            count = 0
            for i, ln in enumerate(meta.lines):
                if ln.strip() == "---":
                    count += 1
                    if count == 2:
                        body_start = i + 1
                        break
        seen: set[tuple[Path, int]] = set()
        for i in range(body_start, len(meta.lines)):
            ln = meta.lines[i]
            for m in _MD_LINK_RE.finditer(ln):
                resolved = resolve_citation(m.group(1), path, docs_root, repo_root)
                if resolved is None or resolved == path.resolve():
                    continue
                st = _cited_status(resolved)
                if st not in NON_CITABLE_STATUSES:
                    continue
                key = (resolved, st is not None)
                if key in seen:  # один draft-источник — одно сообщение на файл
                    continue
                if _line_or_prev_has_bypass(meta.lines, i + 1):
                    continue
                seen.add(key)
                violations.append(
                    Violation(
                        path=path,
                        line=i + 1,
                        category="cites_non_active",
                        message=(
                            f"active {meta.type} ссылается (body) на "
                            f'"{m.group(1)}" со status="{st}". Промоутить источник '
                            f"ИЛИ `<!-- noqa: doc-status-bypass <reason> -->`."
                        ),
                    )
                )
    return violations


# ============================================================================
# Entry point
# ============================================================================


def _find_docs_root(repo_root: Path, cfg: dict) -> Path:
    d = repo_root / cfg["docs_dir"]
    return d if d.is_dir() else repo_root


def collect_files(targets: list[str]) -> list[Path]:
    files: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            files.extend(q for q in sorted(p.rglob("*.md")) if not _is_material(q))
        elif p.is_file() and p.suffix == ".md" and not _is_material(p):
            files.append(p)
    return files


def default_targets(repo_root: Path, cfg: dict) -> list[str]:
    """Весь docs_dir — как прежде — и документы доп. корней поштучно.

    Доп. корень — шаблон файлов, а не каталог: каталог `specs/` целиком
    затащил бы в проверку план и задачи.
    """
    docs_dir = cfg["docs_dir"].rstrip("/")
    out = [docs_dir + "/"]
    for rel, _ in lib.iter_docs(repo_root, cfg):
        if not rel.startswith(docs_dir + "/"):
            out.append(rel)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-commit gate: doc-status consistency + active-only citation.",
    )
    parser.add_argument("paths", nargs="*", help="Files или директории (.md).")
    parser.add_argument("--repo-root", type=Path, default=None)
    args = parser.parse_args(argv)

    global _cfg, _repo_root
    repo_root = (args.repo_root or Path.cwd()).resolve()
    _cfg, _repo_root = lib.load_config(repo_root), repo_root
    docs_root = _find_docs_root(repo_root, _cfg)

    targets = args.paths or default_targets(repo_root, _cfg)
    files = collect_files(targets)
    if not files:
        print("check_doc_status: no markdown files matched.", file=sys.stderr)
        return 0

    all_violations: list[Violation] = []
    for f in files:
        all_violations.extend(check_file(f, docs_root, repo_root))

    if not all_violations:
        print(f"check_doc_status: OK ({len(files)} files scanned, 0 violations)")
        _print_warnings(repo_root, sys.stdout)
        return 0

    print(
        f"check_doc_status: FAIL ({len(all_violations)} violation(s) "
        f"across {len(files)} scanned files):",
        file=sys.stderr,
    )
    for v in all_violations:
        print(f"  {v.format(repo_root)}", file=sys.stderr)
    print(
        "\nStatus discipline (справочник doc-system):"
        "\n  • frontmatter status MUST match body `## Status`"
        "\n  • active adr/spec MUST cite only active sources"
        "\n  • escape hatch (with reason): `# noqa: doc-status-bypass <why>`"
        " or `<!-- noqa: doc-status-bypass <why> -->`",
        file=sys.stderr,
    )
    _print_warnings(repo_root, sys.stderr)
    return 1


def _print_warnings(repo_root: Path, stream) -> None:
    if not _warnings:
        return
    print(f"\nпредупреждения ({len(_warnings)}), проверку не роняют:", file=stream)
    for w in _warnings:
        print(f"  {w.format(repo_root)}", file=stream)


if __name__ == "__main__":
    sys.exit(main())
