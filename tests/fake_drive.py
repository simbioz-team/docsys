#!/usr/bin/env python3
"""Поддельный Google Drive v3 и токен-сервер — для тестов выгрузки.

Держит ровно то подмножество, которым пользуется `gdrive.py`: список с
запросом `q`, создание папки, создание и обновление документа multipart,
перенос между папками, свойства с поштучным слиянием, как в Drive. Запросы на
запись считаются; можно попросить отвечать ошибкой.

Конвертация Markdown → Google Doc не имитируется: содержимое документа —
присланный текст как есть. Проверяется наша сторона, а не Google.
"""

from __future__ import annotations

import http.server
import itertools
import json
import re
import threading
import urllib.parse
from typing import Any

FOLDER = "application/vnd.google-apps.folder"
DRIVE_ID = "shared-drive-1"


class State:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.ids = itertools.count(1)
        self.writes: list[str] = []
        self.fail_with: int | None = None  # отвечать этим кодом на всё, кроме токена
        self.token_revoked = False  # токен-сервер отвечает invalid_grant
        self.lock = threading.Lock()

    def new_id(self, prefix: str = "f") -> str:
        return f"{prefix}{next(self.ids):04d}"

    def add_folder(self, name: str, parent: str | None) -> str:
        fid = self.new_id("d")
        self.files[fid] = {"id": fid, "name": name, "mimeType": FOLDER,
                           "parents": [parent] if parent else [], "properties": {},
                           "description": "", "trashed": False, "driveId": DRIVE_ID}
        return fid

    def docs(self) -> list[dict[str, Any]]:
        return [f for f in self.files.values() if f["mimeType"] != FOLDER]

    def path_of(self, fid: str) -> str:
        parts = []
        cur = self.files.get(fid)
        while cur:
            parts.append(cur["name"])
            par = cur["parents"][0] if cur["parents"] else None
            cur = self.files.get(par) if par else None
        return "/".join(reversed(parts))


PROP = re.compile(r"properties has \{ key='((?:[^'\\]|\\.)*)' and value='((?:[^'\\]|\\.)*)' \}")


def _unq(s: str) -> str:
    return re.sub(r"\\(.)", r"\1", s)


def match(f: dict[str, Any], q: str) -> bool:
    for k, v in PROP.findall(q):
        if f["properties"].get(_unq(k)) != _unq(v):
            return False
    rest = PROP.sub("", q)
    for clause in [c.strip() for c in rest.split(" and ") if c.strip()]:
        if clause == "trashed = false":
            if f["trashed"]:
                return False
        elif m := re.fullmatch(r"name = '((?:[^'\\]|\\.)*)'", clause):
            if f["name"] != _unq(m.group(1)):
                return False
        elif m := re.fullmatch(r"'((?:[^'\\]|\\.)*)' in parents", clause):
            if _unq(m.group(1)) not in f["parents"]:
                return False
        elif m := re.fullmatch(r"mimeType = '([^']*)'", clause):
            if f["mimeType"] != m.group(1):
                return False
        else:
            raise ValueError(f"поддельный Drive не понимает условие: {clause!r}")
    return True


def parse_multipart(body: bytes, ctype: str) -> tuple[dict[str, Any], str]:
    boundary = re.search(r"boundary=([^;]+)", ctype).group(1)
    parts = body.decode("utf-8").split(f"--{boundary}")
    meta, media = {}, ""
    for part in parts:
        if "\r\n\r\n" not in part:
            continue
        head, content = part.split("\r\n\r\n", 1)
        content = content[:-2] if content.endswith("\r\n") else content
        if "application/json" in head:
            meta = json.loads(content)
        elif "text/markdown" in head:
            media = content
    return meta, media


def make_handler(st: State):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: Any) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _route(self, method: str) -> None:
            u = urllib.parse.urlparse(self.path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            body = self._body()
            if u.path == "/token":
                if st.token_revoked:
                    return self._send(400, {"error": "invalid_grant",
                                            "error_description": "Token has been expired or revoked."})
                return self._send(200, {"access_token": "fake-access", "expires_in": 3600})
            if st.fail_with:
                return self._send(st.fail_with, {"error": {"message": "поддельный отказ"}})
            if self.headers.get("Authorization") != "Bearer fake-access":
                return self._send(401, {"error": {"message": "нет токена"}})
            if qs.get("supportsAllDrives") != "true":
                return self._send(400, {"error": {"message": "нет supportsAllDrives"}})
            with st.lock:
                if method != "GET":
                    st.writes.append(f"{method} {u.path}")
                return self._dispatch(method, u.path, qs, body)

        def _dispatch(self, method, path, qs, body):
            m = re.fullmatch(r"/(upload/)?drive/v3/files(?:/([^/]+))?", path)
            if not m:
                return self._send(404, {"error": {"message": path}})
            upload, fid = m.group(1), m.group(2)
            if method == "GET" and fid:
                f = st.files.get(fid)
                return self._send(200, f) if f else self._send(404, {"error": {"message": "нет"}})
            if method == "GET":
                files = [f for f in st.files.values() if match(f, qs.get("q", ""))]
                if qs.get("corpora") == "drive":
                    files = [f for f in files if f.get("driveId") == qs.get("driveId")]
                return self._send(200, {"files": sorted(files, key=lambda f: f["id"])})
            ctype = self.headers.get("Content-Type", "")
            if upload:
                meta, media = parse_multipart(body, ctype)
            else:
                meta, media = json.loads(body.decode() or "{}"), None
            if method == "POST":
                nid = st.new_id()
                f = {"id": nid, "name": meta.get("name", ""), "mimeType": meta.get("mimeType", ""),
                     "parents": list(meta.get("parents", [])),
                     "properties": dict(meta.get("properties") or {}),
                     "description": meta.get("description", ""), "trashed": False,
                     "driveId": DRIVE_ID, "content": media or ""}
                st.files[nid] = f
                return self._send(200, f)
            if method == "PATCH":
                f = st.files.get(fid)
                if not f:
                    return self._send(404, {"error": {"message": "нет"}})
                for k in ("name", "description"):
                    if k in meta:
                        f[k] = meta[k]
                for k, v in (meta.get("properties") or {}).items():
                    if v is None:
                        f["properties"].pop(k, None)
                    else:
                        f["properties"][k] = v
                if media is not None:
                    f["content"] = media
                if qs.get("removeParents"):
                    rm = set(qs["removeParents"].split(","))
                    f["parents"] = [p for p in f["parents"] if p not in rm]
                if qs.get("addParents"):
                    f["parents"].append(qs["addParents"])
                return self._send(200, f)
            return self._send(405, {"error": {"message": method}})

        def do_GET(self):  # noqa: N802
            self._route("GET")

        def do_POST(self):  # noqa: N802
            self._route("POST")

        def do_PATCH(self):  # noqa: N802
            self._route("PATCH")

    return H


class FakeDrive:
    """Сервер в отдельном потоке. `root` — корневая папка на общем диске."""

    def __init__(self) -> None:
        self.state = State()
        self.root = self.state.add_folder("docsys-root", None)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_port}"

    def __enter__(self) -> "FakeDrive":
        self.thread.start()
        return self

    def __exit__(self, *a) -> None:
        self.srv.shutdown()
        self.srv.server_close()
