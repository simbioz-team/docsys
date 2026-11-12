#!/usr/bin/env python3
"""Google Drive v3 для выгрузки документов: вход и запросы. Только stdlib.

Вход — OAuth 2.0 для настольных приложений: браузер, перенаправление на
127.0.0.1, токен обновления в `~/.config/docsys/google.json` (0600). Сервисный
аккаунт не годится: ему нужна подпись RS256, а её в стандартной библиотеке нет.

Адреса переопределяются окружением — для тестов на поддельном сервере:

    DOCSYS_GDRIVE_API          https://www.googleapis.com
    DOCSYS_GOOGLE_TOKEN_URL    https://oauth2.googleapis.com/token
    DOCSYS_GOOGLE_CREDENTIALS  ~/.config/docsys/google.json
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER = "application/vnd.google-apps.folder"
GDOC = "application/vnd.google-apps.document"
FILE_FIELDS = "id,name,parents,properties,description,mimeType,trashed"


def api_base() -> str:
    return os.environ.get("DOCSYS_GDRIVE_API", "https://www.googleapis.com").rstrip("/")


def token_url() -> str:
    return os.environ.get("DOCSYS_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")


def credentials_path() -> pathlib.Path:
    p = os.environ.get("DOCSYS_GOOGLE_CREDENTIALS")
    return pathlib.Path(p) if p else pathlib.Path.home() / ".config" / "docsys" / "google.json"


class DriveError(Exception):
    pass


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------


def load_credentials() -> dict[str, Any] | None:
    p = credentials_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("refresh_token") else None


def save_credentials(data: dict[str, Any]) -> pathlib.Path:
    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(p, 0o600)
    return p


def authorize(client_id: str, client_secret: str, timeout: int = 300) -> pathlib.Path:
    """Вход через браузер. Печатает адрес, ждёт перенаправления, сохраняет токен."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            ok = "code" in got and got.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "docsys: вход выполнен, окно можно закрыть." if ok else "docsys: вход не удался."
            self.wfile.write(f"<p>{msg}</p>".encode())

        def log_message(self, *a):  # тишина в терминале
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    srv.timeout = timeout
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    print("Откройте в браузере и войдите аккаунтом с доступом к папке выгрузки:\n")
    print(url + "\n", flush=True)
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    srv.handle_request()
    srv.server_close()
    if got.get("state") != state or "code" not in got:
        raise DriveError(f"вход не завершён: {got.get('error', 'нет кода авторизации')}")

    tok = _post_form(token_url(), {
        "code": got["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if not tok.get("refresh_token"):
        raise DriveError("Google не выдал токен обновления")
    return save_credentials({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok["refresh_token"],
    })


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        text = _err_text(e)
        if text.startswith("invalid_grant"):
            # Отозван, просрочен (у приложения в режиме Testing — через 7 дней)
            # или выдан другому клиенту. Повторять бессмысленно — нужен вход.
            raise DriveError(
                "токен входа в Google истёк или отозван — войдите заново: "
                "python3 publish.py auth --client-id <ID> --client-secret <SECRET>"
            ) from e
        raise DriveError(f"токен: HTTP {e.code} {text}") from e
    except urllib.error.URLError as e:
        raise DriveError(f"токен: нет связи ({e.reason})") from e


def _err_text(e: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(e.read().decode())
    except Exception:
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message", "")
    return f"{err} {body.get('error_description', '')}".strip()


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------


class Drive:
    """Тонкая обёртка над нужными методами Drive v3. Без кэша и без догадок."""

    def __init__(self, creds: dict[str, Any]):
        self.creds = creds
        self._token: str | None = None
        self._expires = 0.0
        self.writes = 0  # число запросов на запись — для отчёта и тестов

    # --- транспорт ---------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        tok = _post_form(token_url(), {
            "client_id": self.creds["client_id"],
            "client_secret": self.creds["client_secret"],
            "refresh_token": self.creds["refresh_token"],
            "grant_type": "refresh_token",
        })
        if "access_token" not in tok:
            raise DriveError("токен: в ответе нет access_token")
        self._token = tok["access_token"]
        self._expires = time.time() + float(tok.get("expires_in", 3600))
        return self._token

    def _call(self, method: str, path: str, params: dict[str, Any] | None = None,
              body: bytes | None = None, ctype: str | None = None) -> dict[str, Any]:
        q = dict(params or {})
        q["supportsAllDrives"] = "true"
        url = f"{api_base()}{path}?{urllib.parse.urlencode(q)}"
        if method != "GET":
            self.writes += 1
        delay = 1.0
        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            if ctype:
                headers["Content-Type"] = ctype
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                retry = e.code == 429 or e.code >= 500 or (
                    e.code == 403 and "rate" in _peek(e).lower())
                if e.code == 401 and attempt == 0:
                    self._token = None  # токен доступа протух — взять новый
                    continue
                if retry and attempt < 3:
                    time.sleep(float(e.headers.get("Retry-After") or delay))
                    delay *= 2
                    continue
                raise DriveError(f"{method} {path}: HTTP {e.code} {_peek(e)}") from e
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise DriveError(f"{method} {path}: нет связи ({e.reason})") from e
        raise DriveError(f"{method} {path}: попытки исчерпаны")

    def _multipart(self, meta: dict[str, Any], media: str) -> tuple[bytes, str]:
        boundary = "docsys" + secrets.token_hex(12)
        parts = [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n",
            json.dumps(meta, ensure_ascii=False),
            f"\r\n--{boundary}\r\nContent-Type: text/markdown; charset=UTF-8\r\n\r\n",
            media,
            f"\r\n--{boundary}--\r\n",
        ]
        return "".join(parts).encode("utf-8"), f"multipart/related; boundary={boundary}"

    # --- методы ------------------------------------------------------------

    def drive_id(self, folder_id: str) -> str | None:
        """ID общего диска, в котором лежит папка; None — папка в «Моём диске»."""
        r = self._call("GET", f"/drive/v3/files/{folder_id}", {"fields": "id,driveId,mimeType"})
        if r.get("mimeType") and r["mimeType"] != FOLDER:
            raise DriveError(f"{folder_id} — не папка")
        return r.get("driveId")

    def list(self, q: str, drive_id: str | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "q": q,
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": 1000,
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params.update({"corpora": "drive", "driveId": drive_id})
        else:
            params["corpora"] = "user"
        out: list[dict[str, Any]] = []
        while True:
            r = self._call("GET", "/drive/v3/files", params)
            out += r.get("files", [])
            if not r.get("nextPageToken"):
                return out
            params["pageToken"] = r["nextPageToken"]

    def tracked(self, root_id: str, drive_id: str | None) -> list[dict[str, Any]]:
        """Все копии документов этого проекта — по метке корня, не по месту."""
        q = (f"properties has {{ key='docsys_root' and value='{_q(root_id)}' }}"
             " and trashed = false")
        return self.list(q, drive_id)

    def find_folder(self, name: str, parent: str, drive_id: str | None) -> str | None:
        q = (f"name = '{_q(name)}' and '{_q(parent)}' in parents and "
             f"mimeType = '{FOLDER}' and trashed = false")
        found = self.list(q, drive_id)
        return sorted(f["id"] for f in found)[0] if found else None

    def create_folder(self, name: str, parent: str) -> str:
        body = json.dumps({"name": name, "mimeType": FOLDER, "parents": [parent]},
                          ensure_ascii=False).encode()
        return self._call("POST", "/drive/v3/files", {"fields": "id"}, body,
                          "application/json; charset=UTF-8")["id"]

    def create_doc(self, name: str, parent: str, properties: dict[str, str],
                   description: str, markdown: str) -> dict[str, Any]:
        meta = {"name": name, "mimeType": GDOC, "parents": [parent],
                "properties": properties, "description": description}
        body, ctype = self._multipart(meta, markdown)
        return self._call("POST", "/upload/drive/v3/files",
                          {"uploadType": "multipart", "fields": FILE_FIELDS}, body, ctype)

    def update_doc(self, file_id: str, *, name: str | None = None,
                   properties: dict[str, str] | None = None, description: str | None = None,
                   markdown: str | None = None, add_parent: str | None = None,
                   remove_parents: list[str] | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        if name is not None:
            meta["name"] = name
        if properties is not None:
            meta["properties"] = properties
        if description is not None:
            meta["description"] = description
        params: dict[str, Any] = {"fields": FILE_FIELDS}
        if add_parent:
            params["addParents"] = add_parent
        if remove_parents:
            params["removeParents"] = ",".join(remove_parents)
        if markdown is None:
            return self._call("PATCH", f"/drive/v3/files/{file_id}", params,
                              json.dumps(meta, ensure_ascii=False).encode(),
                              "application/json; charset=UTF-8")
        params["uploadType"] = "multipart"
        body, ctype = self._multipart(meta, markdown)
        return self._call("PATCH", f"/upload/drive/v3/files/{file_id}", params, body, ctype)


def _q(s: str) -> str:
    """Экранирование строки в запросе Drive: обратная косая и апостроф."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _peek(e: urllib.error.HTTPError) -> str:
    if not hasattr(e, "_docsys_text"):
        e._docsys_text = _err_text(e)  # type: ignore[attr-defined]
    return e._docsys_text  # type: ignore[attr-defined]


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
