---
description: Пересобрать реестры и показать, что изменилось в документах
disable-model-invocation: true
---

Синхронизируй реестры с документами.

## Шаг 1. Что разошлось

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_registries.py" --check
```

Код 0 — реестры актуальны, скажи об этом и остановись.

## Шаг 2. Что менялось руками

```bash
git status --short -- $(python3 -c "import json;c=json.load(open('.claude/doc-config.json'));print(' '.join([c['docs_dir']]+[':(glob)'+(r['glob'] if isinstance(r,dict) else r) for r in c.get('extra_roots',[])]))")
```

Покажи изменённые и новые документы.

## Шаг 3. Проверь метаданные изменённых

Для каждого изменённого документа прочитай frontmatter и сверь:

- `updated` отстаёт от факта правки → обнови на сегодня
- содержание изменилось, а `summary` нет → перепиши summary
- документ новый и без frontmatter → предложи заполнить

Выдумывать поля нельзя. Нет — значит прочерк.

## Шаг 4. Пересобери

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_registries.py"
```

## Итог

Сколько документов затронуто, у каких поправлены метаданные, что осталось
без frontmatter.
