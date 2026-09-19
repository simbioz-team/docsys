---
description: Развернуть систему документации в проекте — конфиг, каталоги, реестры
disable-model-invocation: true
---

Разверни систему документации в текущем проекте.

## Шаг 1. Конфиг

Если `.claude/doc-config.json` уже есть — покажи его и спроси, что менять.
Иначе создай, предварительно спросив: корневой каталог документации (по умолчанию
`docs`) и какие типы документов нужны.

```json
{
  "version": "1.0",
  "docs_dir": "docs",
  "registries": { "index": "docs/INDEX.md", "tags": "docs/TAGS.md", "index_limit": 0 },
  "statuses": ["active", "draft", "deprecated", "superseded", "archived"],
  "tiers": ["foundation", "feature", "feature-derived"],
  "types": [
    { "name": "adr",         "template": "adr.md",         "path": "docs/decisions/ADR-{NNN}-{slug}.md" },
    { "name": "overview",    "template": "overview.md",    "path": "docs/architecture/{slug}.md" },
    { "name": "design",      "template": "design.md",      "path": "docs/architecture/{slug}.md" },
    { "name": "requirement", "template": "requirement.md", "path": "docs/requirements/{slug}.md" },
    { "name": "research",    "template": "research.md",    "path": "docs/research/{slug}.md" },
    { "name": "reference",   "template": "reference.md",   "path": "docs/reference/{slug}.md" },
    { "name": "explainer",   "template": "explainer.md",   "path": "docs/explainers/{slug}.md" },
    { "name": "source",      "template": "source.md",      "path": "docs/sources/{slug}.md" },
    { "name": "frame",       "template": "frame.md",       "path": "docs/{slug}.md" },
    { "name": "product",     "template": "frame.md",       "path": "docs/product/{slug}.md" }
  ],
  "exclude": ["docs/INDEX.md", "docs/TAGS.md", "docs/decisions/adr-0000-template.md"]
}
```

`index_limit: 0` — показывать в INDEX.md все документы. Поставь число, когда их
станет столько, что таблица перестанет читаться.

Типы `frame` и `product` — документы рамок: конституция, стек, раскладка,
миссия, дорожная карта, словарь. Они отличаются от остальных тем, что
**поставляются каркасом уже созданными**, со слотами вместо содержания.
`/doc-new` для них не нужен — их заполняют на месте, по вопросам этапа.
Различаются только местом: `product` лежит в `docs/product/`, `frame` — в
корне `docs/`.

Тип `source` — принесённые снаружи материалы: бриф, ТЗ, расшифровка встречи.
Он не такой, как остальные: документ этого типа **не редактируется после
создания**, а поле `origin` обязательно — источник без указания, чей он и
когда написан, через полгода анонимка. Если проекту такое не нужно, тип можно
не заводить.

## Шаг 2. Каталоги и реестры

Создай каталоги под типы из конфига. Собери реестры:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_registries.py"
```

## Шаг 3. Подключение к гейту

Если в проекте есть `.claude/check.sh` — добавь в него строку:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/check_docs.py" || exit 1
```

Нет `check.sh` — скажи об этом и предложи завести: без гейта проверки
документации никто не запустит.

## Шаг 4. Ссылка из свода правил

Допиши в `AGENTS.md` (или `CLAUDE.md`) раздел:

```markdown
## Документация
Навигация: `docs/INDEX.md` → конкретный документ. Полный список — `docs/TAGS.md`.
Реестры собираются скриптом, руками не править.
Новый документ — `/doc-new`, изменение — `/doc-update`.
```

## Итог

Покажи: что создано, сколько документов попало в реестры, подключён ли гейт.
