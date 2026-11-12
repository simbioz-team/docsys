"""`exclude` и хук: что исключено — не документ, и правка его реестры не трогает."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN = HERE.parent / "plugins" / "docsys"
sys.path.insert(0, str(PLUGIN / "scripts"))

import docs_lib as lib  # noqa: E402

DOC = """---
title: A
type: overview
status: active
updated: 2026-11-01
tags: [x]
summary: Справочник, в котором есть содержание
---
body
"""


class ExcludeGlob(unittest.TestCase):
    def cfg(self, *pats: str) -> dict:
        return {"docs_dir": "docs", "exclude": list(pats), "extra_roots": []}

    def test_double_star_any_depth(self):
        cfg = self.cfg("docs/journal/**")
        for rel in ("docs/journal/a.md", "docs/journal/2026/a.md", "docs/journal/2026/11/a.md"):
            self.assertFalse(lib.is_doc(rel, cfg), rel)
        self.assertTrue(lib.is_doc("docs/journalist.md", cfg))
        self.assertTrue(lib.is_doc("docs/notes/journal/a.md", cfg))

    def test_old_right_anchored_match_kept(self):
        # Прежний разбор сравнивал с правого края: `*.draft.md` исключал файл
        # на любой глубине. Исправление не должно это сузить.
        cfg = self.cfg("*.draft.md")
        self.assertFalse(lib.is_doc("docs/a/b/x.draft.md", cfg))
        self.assertTrue(lib.is_doc("docs/a/b/x.md", cfg))


class HookSkipsExcluded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "doc-config.json").write_text(json.dumps({
            "docs_dir": "docs",
            "types": [{"name": "overview", "template": "overview.md", "path": "docs/notes/{slug}.md"}],
            "exclude": ["docs/INDEX.md", "docs/TAGS.md", "docs/journal/**"],
        }), encoding="utf-8")
        (self.root / "docs" / "notes").mkdir(parents=True)
        (self.root / "docs" / "journal" / "2026").mkdir(parents=True)
        (self.root / "docs" / "notes" / "a.md").write_text(DOC, encoding="utf-8")
        (self.root / "docs" / "journal" / "2026" / "b.md").write_text("запись\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, rel: str) -> None:
        payload = json.dumps({"tool_input": {"file_path": str(self.root / rel)}, "cwd": str(self.root)})
        env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(PLUGIN))
        subprocess.run(["bash", str(PLUGIN / "scripts" / "on-doc-edit.sh")],
                       input=payload, text=True, env=env, check=True, capture_output=True)

    def test_excluded_edit_does_not_rebuild(self):
        self.hook("docs/journal/2026/b.md")
        self.assertFalse((self.root / "docs" / "INDEX.md").exists())

    def test_doc_edit_rebuilds_without_excluded(self):
        self.hook("docs/notes/a.md")
        tags = (self.root / "docs" / "TAGS.md").read_text(encoding="utf-8")
        self.assertIn("docs/notes/a.md", tags)
        self.assertNotIn("journal", tags)


if __name__ == "__main__":
    unittest.main()
