# Задачи — 001 Выгрузка в Google Drive

- [x] T1. `publish_render.py`: отбор по `include`/`exclude`/`skip_statuses`
  (R2–R3), имя с меткой (R5), шапка (R6), путь папки (R8), хеш (R10).
- [x] T2. `publish_render.py`: переписывание ссылок и картинок (R7).
- [x] T3. `gdrive.py`: OAuth — `auth` с перенаправлением на 127.0.0.1, хранение
  `~/.config/docsys/google.json` (0600), обмен refresh → access (R18, R22).
- [x] T4. `gdrive.py`: Drive v3 — список по `properties`, папки,
  создание/обновление multipart, перенос между папками (R9, R11, R21).
- [x] T5. `publish.py`: алгоритм из плана — блокировка, два прохода, архив,
  `--dry-run`, `--path`, `status`, журнал и итог (R10, R12, R15–R17).
- [x] T6. `on-doc-edit.sh`: фоновая выгрузка и сообщение о провале (R13–R14).
- [x] T7. `tests/fake_drive.py` + `tests/test_publish.py`; шаг в CI.
- [x] T8. `/doc-publish` (команда), `SKILL.md`, `README.md`, `doc-init.md`
  (раздел `publish` и настройка общего диска), `CLAUDE.md` (команда тестов).
- [x] T9. Регрессия: без `publish` — побайтово на narta и study_mcp; проверки
  репозитория.
- [x] T10. Версия 0.3.1, `CHANGELOG.md`.
- [x] T11. Живой прогон в папке владельца (личный «Мой диск»): 136 документов, повтор без изменений, правка одного, архив.
