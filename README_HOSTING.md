# Репозиторий для деплоя Telegram-бота

## Что положить в репозиторий
В этот репозиторий нужно добавить:
- `main.py` — уже готовый launcher;
- основной файл бота, например `guild_s9_2_hosting_ready.py`;
- `guild_s9_core.py`, если основной файл его импортирует;
- файлы из этого набора: `requirements.txt`, `Procfile`, `Dockerfile`, `.env.example`, `.gitignore`, `.dockerignore`.

## Последовательность работы на хостинге
1. Хостинг забирает код из GitHub.
2. Устанавливает зависимости из `requirements.txt`.
3. Подставляет environment variables.
4. Подключает постоянный диск для SQLite.
5. Запускает один worker-командой `python main.py`.
6. `main.py` смотрит на переменную `BOT_ENTRY` и запускает нужный файл бота.
7. Сам бот читает env, открывает SQLite, делает миграции и стартует polling.

## Что обязательно настроить
В панели хостинга задай переменные из `.env.example`.
Особенно важны:
- `TG_BOT_TOKEN`
- `ADMIN_ID`
- `GUILD_CHAT_ID`
- все `*_THREAD_ID`
- `DB_PATH`
- `BOT_ENTRY`

## Важные условия
Бот должен работать как `worker/background service`, а не как web service.
Должен быть только один инстанс.
SQLite-база должна лежать на постоянном диске.

## Build и Start
Если хостинг спрашивает команды вручную:

Build:
`pip install -r requirements.txt`

Start:
`python main.py`

## Docker
Если хостинг сам собирает контейнер, ему хватит `Dockerfile` из этого набора.
В нём нет странных entrypoint-скриптов, из-за которых обычно всё и ломается.

## Важно
В репозитории не должно быть живой базы `.db` и файла `.env`.
