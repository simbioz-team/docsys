#!/usr/bin/env python3
"""Выгрузка в Drive на поддельном сервере: сценарии из спеки 001, «Готовность» п. 2.

Запуск: python3 -m unittest discover -s tests -v   (из корня репозитория)
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "plugins" / "docsys" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

from fake_drive import FakeDrive  # noqa: E402
import publish_render as pr  # noqa: E402

CONFIG = {
    "version": "1.0",
    "docs_dir": "docs",
    "registries": {"index": "docs/INDEX.md", "tags": "docs/TAGS.md", "index_limit": 0},
    "types": [
        {"name": "adr", "template": "adr.md", "path": "docs/decisions/adr-{NNN}-{slug}.md"},
        {"name": "overview", "template": "overview.md", "path": "docs/architecture/{slug}.md"},
        {"name": "spec", "template": "spec.md", "path": "specs/{NNN}-{slug}/spec.md"},
    ],
    "extra_roots": [{"glob": "specs/*/spec.md"}],
    "exclude": ["docs/INDEX.md", "docs/TAGS.md"],
}

DOCS = {
    "docs/decisions/adr-001-base.md": """---
title: Базовое решение
type: adr
tier: foundation
status: active
updated: 2026-11-01
tags: [base]
summary: Базовое решение, на которое опирается спека A.
---

# Базовое решение

Реализуется в [спеке A](../../specs/001-a/spec.md#сценарии). План — [plan](../../specs/001-a/plan.md).
Внешняя: [Google](https://google.com). Якорь: [ниже](#раздел). Схема: ![схема](img/s.png)

```
[в коде](../architecture/note.md) не трогать
```
И `[инлайн-код](x.md)` тоже.
""",
    "docs/architecture/note.md": """---
title: Заметка
type: overview
status: draft
tags: [note]
summary: Черновая заметка об архитектуре без даты обновления.
---

# Заметка

См. [ADR](../decisions/adr-001-base.md).
""",
    "docs/journal/day.md": """---
title: Журнал
type: overview
status: active
updated: 2026-11-01
summary: Журнал дня, не выгружается по exclude.
---
""",
    "specs/001-a/spec.md": """---
title: Спека A
type: spec
status: active
updated: 2026-11-01
summary: Активная спека фичи A.
---

# Спека A

Основание — [ADR-001](../../docs/decisions/adr-001-base.md).
""",
}


def git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        (self.repo / ".claude").mkdir(parents=True)
        git(self.repo, "init", "-q")
        for rel, text in DOCS.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        (self.repo / "specs/001-a/plan.md").write_text("# План\n", encoding="utf-8")
        self.drive = FakeDrive().__enter__()
        self.write_config()
        creds = pathlib.Path(self.tmp.name) / "google.json"
        creds.write_text(json.dumps({"client_id": "c", "client_secret": "s",
                                     "refresh_token": "r"}), encoding="utf-8")
        self.env = dict(os.environ,
                        DOCSYS_GDRIVE_API=self.drive.url,
                        DOCSYS_GOOGLE_TOKEN_URL=self.drive.url + "/token",
                        DOCSYS_GOOGLE_CREDENTIALS=str(creds))

    def tearDown(self) -> None:
        self.drive.__exit__()
        self.tmp.cleanup()

    # --- помощники ---------------------------------------------------------

    def write_config(self, **publish) -> None:
        cfg = dict(CONFIG)
        cfg["publish"] = {"provider": "gdrive", "folder_id": self.drive.root,
                          "exclude": ["docs/journal/**"], **publish}
        (self.repo / ".claude/doc-config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def run_pub(self, *args: str, expect: int = 0) -> str:
        r = subprocess.run([sys.executable, str(SCRIPTS / "publish.py"), *args],
                           cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def docs_by_source(self) -> dict[str, dict]:
        st = self.drive.state
        return {f["description"].split("Источник: ", 1)[1].split(". ", 1)[0]: f
                for f in st.docs()}

    def where(self, f: dict) -> str:
        return self.drive.state.path_of(f["parents"][0])

    # --- сценарии ----------------------------------------------------------

    def test_first_publish_creates_tree_and_properties(self):
        out = self.run_pub()
        self.assertIn("создано 3", out)
        docs = self.docs_by_source()
        self.assertEqual(sorted(docs), ["docs/architecture/note.md",
                                        "docs/decisions/adr-001-base.md", "specs/001-a/spec.md"])
        adr = docs["docs/decisions/adr-001-base.md"]
        self.assertEqual(self.where(adr), "docsys-root/docs/decisions")
        self.assertEqual(adr["mimeType"], "application/vnd.google-apps.document")
        self.assertEqual(adr["properties"]["docsys_root"], self.drive.root)
        self.assertEqual(len(adr["properties"]["docsys_hash"]), 32)
        self.assertEqual(adr["name"], "Базовое решение")
        self.assertEqual(docs["docs/architecture/note.md"]["name"], "[черновик] Заметка")
        self.assertNotIn("tier: foundation", adr["content"])  # frontmatter не уехал

    def test_second_run_writes_nothing(self):
        self.run_pub()
        before = len(self.drive.state.writes)
        out = self.run_pub()
        self.assertEqual(len(self.drive.state.writes), before, self.drive.state.writes[before:])
        self.assertIn("без изменений 3", out)

    def test_edit_one_doc_one_write_same_id(self):
        self.run_pub()
        docs = self.docs_by_source()
        old_id = docs["specs/001-a/spec.md"]["id"]
        p = self.repo / "specs/001-a/spec.md"
        p.write_text(p.read_text(encoding="utf-8") + "\nНовый абзац.\n", encoding="utf-8")
        before = len(self.drive.state.writes)
        self.run_pub()
        self.assertEqual(len(self.drive.state.writes) - before, 1)
        spec = self.docs_by_source()["specs/001-a/spec.md"]
        self.assertEqual(spec["id"], old_id)
        self.assertIn("Новый абзац.", spec["content"])

    def test_links(self):
        self.run_pub()
        docs = self.docs_by_source()
        adr = docs["docs/decisions/adr-001-base.md"]["content"]
        spec_id = docs["specs/001-a/spec.md"]["id"]
        self.assertIn(f"[спеке A](https://docs.google.com/document/d/{spec_id}/edit)", adr)
        self.assertIn("plan (в репозитории: specs/001-a/plan.md)", adr)
        self.assertIn("[Google](https://google.com)", adr)
        self.assertIn("[ниже](#раздел)", adr)
        self.assertIn("[изображение: docs/decisions/img/s.png]", adr)
        self.assertIn("[в коде](../architecture/note.md) не трогать", adr)
        self.assertIn("`[инлайн-код](x.md)`", adr)
        # Ссылка на документ вне выгрузки — текстом.
        note = docs["docs/architecture/note.md"]["content"]
        adr_id = docs["docs/decisions/adr-001-base.md"]["id"]
        self.assertIn(f"[ADR](https://docs.google.com/document/d/{adr_id}/edit)", note)

    def test_header(self):
        self.run_pub()
        docs = self.docs_by_source()
        note = docs["docs/architecture/note.md"]["content"]
        self.assertTrue(note.startswith("> Статус: **draft** · Тип: overview · Обновлено: — · "
                                        "Источник: `docs/architecture/note.md`"), note[:200])
        self.assertIn("⚠ Черновик: опираться нельзя.", note)
        adr = docs["docs/decisions/adr-001-base.md"]["content"]
        self.assertNotIn("⚠", adr)
        self.assertIn("Правки здесь будут затёрты", adr)

    def test_archive_on_status_delete_move_and_exclude(self):
        self.run_pub()
        ids = {k: v["id"] for k, v in self.docs_by_source().items()}
        st = self.drive.state
        # archived
        p = self.repo / "docs/architecture/note.md"
        p.write_text(p.read_text(encoding="utf-8").replace("status: draft", "status: archived"),
                     encoding="utf-8")
        # переезд
        (self.repo / "specs/001-a/spec.md").rename(self.repo / "docs/spec-a.md")
        self.run_pub()
        self.assertEqual(self.where(st.files[ids["docs/architecture/note.md"]]), "docsys-root/Архив")
        self.assertEqual(self.where(st.files[ids["specs/001-a/spec.md"]]), "docsys-root/Архив")
        new = [f for f in st.docs() if "docs/spec-a.md" in f["description"]]
        self.assertEqual(len(new), 1)
        self.assertEqual(self.where(new[0]), "docsys-root/docs")
        # выпал из include
        self.write_config(include=["specs/**"])
        self.run_pub()
        self.assertEqual(self.where(st.files[ids["docs/decisions/adr-001-base.md"]]),
                         "docsys-root/Архив")
        # удалён весь документ и вернулся — копия возвращается из архива
        self.write_config()
        self.run_pub()
        self.assertEqual(self.where(st.files[ids["docs/decisions/adr-001-base.md"]]),
                         "docsys-root/docs/decisions")

    def test_include_exclude(self):
        self.write_config(include=["docs/**"])
        self.run_pub()
        self.assertEqual(sorted(self.docs_by_source()),
                         ["docs/architecture/note.md", "docs/decisions/adr-001-base.md"])

    def test_dry_run_writes_nothing(self):
        out = self.run_pub("--dry-run")
        self.assertEqual(self.drive.state.writes, [])
        self.assertIn("создать: specs/001-a/spec.md", out)
        self.assertIn("пробный прогон", out)

    def test_single_path(self):
        self.run_pub("--path", "docs/architecture/note.md")
        self.assertEqual(sorted(self.docs_by_source()), ["docs/architecture/note.md"])
        note = self.docs_by_source()["docs/architecture/note.md"]["content"]
        # ADR ещё не выгружен — ссылка текстом, не в никуда.
        self.assertIn("ADR (в репозитории: docs/decisions/adr-001-base.md)", note)

    def test_status_report(self):
        self.run_pub()
        p = self.repo / "docs/decisions/adr-001-base.md"
        p.write_text(p.read_text(encoding="utf-8") + "\nещё\n", encoding="utf-8")
        out = self.run_pub("status")
        self.assertIn("отстают: 1", out)
        self.assertIn("актуальны: 2", out)

    def test_not_configured_is_silent(self):
        cfg = dict(CONFIG)
        (self.repo / ".claude/doc-config.json").write_text(json.dumps(cfg), encoding="utf-8")
        out = self.run_pub()
        self.assertIn("не настроена", out)
        self.assertEqual(self.drive.state.writes, [])

    def test_failure_writes_status(self):
        self.drive.state.fail_with = 403
        out = self.run_pub(expect=1)
        self.assertIn("не удалась", out)
        status = (self.repo / ".git/docsys-publish.status").read_text(encoding="utf-8")
        self.assertTrue(status.startswith("fail\t"), status)

    def test_revoked_token_says_login_again(self):
        self.drive.state.token_revoked = True
        out = self.run_pub(expect=1)
        self.assertIn("войдите заново", out)
        self.assertIn("publish.py auth", out)

    # --- хук ---------------------------------------------------------------

    def hook(self, rel: str) -> str:
        payload = json.dumps({"tool_input": {"file_path": str(self.repo / rel)},
                              "cwd": str(self.repo)})
        env = dict(self.env, CLAUDE_PLUGIN_ROOT=str(SCRIPTS.parent))
        r = subprocess.run(["bash", str(SCRIPTS / "on-doc-edit.sh")], input=payload,
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def wait_status(self, timeout: float = 20) -> str:
        p = self.repo / ".git/docsys-publish.status"
        end = time.time() + timeout
        while time.time() < end:
            if p.exists():
                return p.read_text(encoding="utf-8")
            time.sleep(0.2)
        log = self.repo / ".git/docsys-publish.log"
        self.fail("выгрузка из хука не отработала: " +
                  (log.read_text(encoding="utf-8") if log.exists() else "журнала нет"))

    def test_hook_publishes_in_background(self):
        self.hook("specs/001-a/spec.md")
        self.assertTrue(self.wait_status().startswith("ok\t"))
        self.assertEqual(sorted(self.docs_by_source()), ["specs/001-a/spec.md"])

    def test_hook_ignores_plan_and_unconfigured(self):
        self.hook("specs/001-a/plan.md")
        cfg = dict(CONFIG)
        (self.repo / ".claude/doc-config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.hook("docs/architecture/note.md")
        time.sleep(1.5)
        self.assertEqual(self.drive.state.writes, [])
        self.assertFalse((self.repo / ".git/docsys-publish.status").exists())

    def test_hook_reports_failure_once(self):
        self.drive.state.fail_with = 500
        self.hook("docs/architecture/note.md")
        self.assertTrue(self.wait_status(timeout=40).startswith("fail\t"))
        first = self.hook("docs/architecture/note.md")
        self.assertIn("выгрузка в Drive не удалась", first)
        second = self.hook("docs/architecture/note.md")
        self.assertNotIn("не удалась", second)


class RenderTest(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(pr.resolve("../b.md#x", "docs/a/c.md"), "docs/b.md")
        self.assertEqual(pr.resolve("/specs/1/spec.md", "docs/a.md"), "specs/1/spec.md")
        self.assertIsNone(pr.resolve("../../../x.md", "docs/a.md"))

    def test_hash_depends_on_name(self):
        self.assertNotEqual(pr.content_hash("a", "t"), pr.content_hash("b", "t"))


if __name__ == "__main__":
    unittest.main()
