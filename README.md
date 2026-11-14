# Telegram Forwarder

Transfers **photos and videos** from a Telegram group or channel (source) into another group (target), optionally into **forum topics**. It is based on [Telethon](https://docs.telethon.dev) and is operated through a web dashboard or the command line.

Structure and feature set follow [TG-Uploader](https://github.com/outofrange007/TG-Uploader) (Flask dashboard, Telethon, topic management, progress, persistence, Docker). The difference: instead of uploading local folders, this project reads the media directly from another Telegram chat.

The transfer history and all state live in **PostgreSQL**. Several forwarder instances can therefore run in parallel (each with its own Telegram session) without anything being transferred twice.

## Features

| Feature | Description |
|---|---|
| Web dashboard | Login (phone → code → 2FA if needed), source and target chat selectable via dropdown from your groups/channels, topic dropdowns for source and target, settings, start/stop, progress bars for file and overall, speed, remaining time, event list |
| Two send modes | `copy`: re-send media without "Forwarded from" (no download needed). `forward`: real forward, optionally without sender |
| Albums | Media with the same `grouped_id` are sent together as an album (max. 10 per album) |
| Topics | `none`, `fixed` (everything into one topic) or `mirror` (topics of the source are created in the target with the same name or reused) |
| Content protection | If the source has "Restrict saving content" enabled, files are downloaded and re-uploaded automatically. Video attributes and thumbnail are preserved, progress is shown |
| Persistence | PostgreSQL database with all processed messages, one checkpoint per source (or per source topic), the topic mapping and transfer reservations. A restart continues where the last run stopped. Failed messages are retried on the next run |
| Multiple instances | Any number of forwarder containers can share one PostgreSQL database. Each instance keeps its own session, settings and logs; before every send a batch is reserved in the database, so two instances never transfer the same message |
| Source topic selection | If the source is a forum with topics, only **one** topic can optionally be processed (history and live mode, incl. albums). Choose it via the dashboard dropdown, `SOURCE_TOPIC_ID` or `run --source-topic` |
| Live mode | First catches up on the history, then transfers new messages in real time |
| Filters | Media types, ID range, date range, maximum file size, a single source topic |
| Captions | Template with the placeholders `{caption}`, `{filename}` (file name), `{source}`, `{link}`, `{id}`, `{date}`. With `{caption}` the original formatting is preserved |
| Robustness | FloodWait is waited out, after connection drops it reconnects and retries; a single broken message does not end the run |
| Dry run | `DRY_RUN=true` only logs what would be sent |
| Docker | `Dockerfile` and `docker-compose.yml` (PostgreSQL 16 + forwarder, Portainer-friendly); each instance keeps its data (session, settings, logs) in its own volume |

## Requirements

- Python 3.10+ (or Docker)
- **PostgreSQL 16** - as a container in the same stack (see Docker) or on an existing server. The connection is configured via `DATABASE_URL`; schema and tables are created automatically on the first start
- `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org) → *API development tools*
- A **Telegram user account** (not a bot) that is a member of both source and target.
  *Why not a bot?* Bots cannot read the message history of other groups and channels and would have to be admin in the source. A user account ("userbot") sees everything you also see in the Telegram app.
- In the target: write permission. For `TOPIC_MODE=mirror` additionally the admin right "Manage topics".

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill in API_ID, API_HASH, SOURCE_CHAT, TARGET_CHAT

# PostgreSQL connection (schema and tables are created automatically)
export DATABASE_URL=postgresql://forwarder:PASSWORD@localhost:5432/forwarder

python -m forwarder login   # once: phone number, code, 2FA password if needed
python -m forwarder chats   # lists your groups/channels with IDs
python -m forwarder web     # dashboard at http://localhost:5000
```

Without the dashboard:

```bash
python -m forwarder run --dry-run   # dry run: what would be transferred?
python -m forwarder run             # transfer the history and exit
python -m forwarder run --live      # history + continuous operation afterwards (Ctrl+C stops cleanly)
python -m forwarder inspect 37-45   # diagnostics: shows how source messages 37-45 are classified
```

### Transfer only one topic of the source

```bash
python -m forwarder topics                       # topics of the source group with IDs (marked: currently selected)
python -m forwarder topics --chat @other_group   # query another group
python -m forwarder run --source-topic 55        # only topic 55 (overrides SOURCE_TOPIC_ID)
python -m forwarder run --source-topic all       # all topics, even if SOURCE_TOPIC_ID is set
```

In the dashboard, "Source topic" is a dropdown. The default is "all topics". The topics of the selected chat load automatically - via ⟳ (or by picking "Source"/"Target" in the chat list) they refresh; the same works for the target topic. If the group is not a forum, this is shown and all messages are processed.

Notes:

- Filtering uses the topic identifier of the message (`reply_to.forum_topic` with top ID). All parts of an album therefore end up in the result together. For normal topics Telethon reads only the topic history on the server side. For "General" (ID 1) the entire history is read and filtered locally.
- If a topic is selected but the source is not a forum, or the topic does not exist, the run aborts with a clear error message.
- **Checkpoints** are stored separately per source *and* topic. A run over topic 55 only therefore does not move the checkpoint for "all topics" or for other topics. **Duplicates** are still ruled out: already transferred messages are in the database regardless of topic and are skipped on every later run.
- The target topic modes stay unchanged: `none` and `fixed` work as before. With `mirror`, only the selected topic is mirrored in the target (created or reused).
- `python -m forwarder reset` or Reset in the dashboard also deletes the topic checkpoints of the source.

Login also works directly in the dashboard.

## Docker

The `docker-compose.yml` starts **PostgreSQL 16** and the forwarder. Set the variables as environment variables (in Portainer under "Environment variables") - no `.env` file is needed. At minimum: `PG_PASSWORD`, `API_ID`, `API_HASH` and optionally `SOURCE_CHAT`, `TARGET_CHAT`, `WEB_PASSWORD`.

```bash
docker compose up -d --build      # dashboard: http://<host>:5020
```

`PG_PASSWORD` is used both for the database container and inside `DATABASE_URL`; use only characters that need no URL escaping (avoid `:` `/` `@` `?` `#`). The password only takes effect when the database volume is created for the **first time** - changing it later does not alter the existing database (see Troubleshooting).

You can log in via the dashboard or the console:

```bash
docker compose run --rm telegram-forwarder python -m forwarder login
```

For continuous operation without the dashboard, enable this line in `docker-compose.yml`:
`command: ["python", "-m", "forwarder", "run", "--live"]`

All instance data is in the volume `./data` (give **each instance its own directory**):

| File/folder | Purpose |
|---|---|
| `forwarder.session` | Telethon session (**equivalent to a login - keep it secret!**) - one per instance |
| `forwarder.db` | Legacy SQLite database. No longer used at runtime; only serves as the import source for the one-time migration to PostgreSQL (see below) |
| `settings.json` | Settings changed in the dashboard (override the environment variables) |
| `logs/forwarder.log` | Rotating log file (5 MB × 3) - per instance |
| `tmp/` | Scratch space for the download fallback (cleaned automatically) |

The transfer history itself lives in PostgreSQL (tables `processed`, `topics`, `kv`) and is shared by all instances.

### Multiple instances

Any number of instances can run in parallel. They share the PostgreSQL history and reserve every batch before sending, so nothing is transferred twice. Each instance needs its own container name, its own data directory (session, settings, logs) and its own dashboard port. Example (Portainer/Compose, remaining variables as in the single-instance stack):

```yaml
x-forwarder-common: &forwarder-common
  image: ghcr.io/outofrange007/tg-forwarder:latest
  restart: unless-stopped
  depends_on:
    postgres:
      condition: service_healthy

x-forwarder-env: &forwarder-env
  TZ: Europe/Berlin
  DATA_PATH: /app/data
  WEB_HOST: 0.0.0.0
  WEB_PORT: "5000"
  DATABASE_URL: postgresql://forwarder:${PG_PASSWORD}@postgres:5432/forwarder
  API_ID: ${API_ID}
  API_HASH: ${API_HASH}
  WEB_PASSWORD: ${WEB_PASSWORD:-}
  TOPIC_MODE: ${TOPIC_MODE:-none}
  TARGET_TOPIC_ID: ${TARGET_TOPIC_ID:-}
  MODE: ${MODE:-copy}
  HIDE_SENDER: ${HIDE_SENDER:-true}
  MEDIA_TYPES: ${MEDIA_TYPES:-photo,video,image_file,video_file}
  COPY_TEXT: ${COPY_TEXT:-false}
  DELAY_SECONDS: ${DELAY_SECONDS:-2}
  DOWNLOAD_FALLBACK: ${DOWNLOAD_FALLBACK:-true}
  MAX_FILE_SIZE_MB: ${MAX_FILE_SIZE_MB:-2000}
  RESUME: ${RESUME:-true}
  DRY_RUN: ${DRY_RUN:-false}
  LOG_LEVEL: ${LOG_LEVEL:-INFO}

services:
  postgres:
    image: postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: forwarder
      POSTGRES_PASSWORD: ${PG_PASSWORD}
      POSTGRES_DB: forwarder
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U forwarder -d forwarder"]
      interval: 5s
      timeout: 5s
      retries: 10

  telegram-forwarder:
    <<: *forwarder-common
    container_name: telegram_forwarder
    environment:
      <<: *forwarder-env
      SOURCE_CHAT: ${SOURCE_CHAT:-}
      TARGET_CHAT: ${TARGET_CHAT:-}
      SESSION_STRING: ${SESSION_STRING:-}
      SOURCE_TOPIC_ID: ${SOURCE_TOPIC_ID:-}
    ports:
      - "127.0.0.1:5030:5000"
    volumes:
      - /home/TG-Forwarder/forwarder/data:/app/data

  telegram-forwarder-2:
    <<: *forwarder-common
    container_name: telegram_forwarder_2
    environment:
      <<: *forwarder-env
      SOURCE_CHAT: ${SOURCE_CHAT_2:-}
      TARGET_CHAT: ${TARGET_CHAT_2:-}
      SESSION_STRING: ${SESSION_STRING_2:-}
      SOURCE_TOPIC_ID: ${SOURCE_TOPIC_ID_2:-}
    ports:
      - "127.0.0.1:5031:5000"
    volumes:
      - /home/TG-Forwarder/forwarder/data_2:/app/data

volumes:
  pgdata:
```

The database container exists only once. Additional instances can live in the same stack or in separate stacks - additional stacks then point `DATABASE_URL` at the existing database host instead of starting their own postgres. Each instance must log in to Telegram once (own session), for example via its own dashboard.

### Migrating from SQLite (one-time)

Installations from before the PostgreSQL migration can import their old data once while the postgres container is running:

```bash
docker run --rm \
  --network container:telegram_forwarder_postgres \
  --env "DATABASE_URL=postgresql://forwarder:${PG_PASSWORD}@127.0.0.1:5432/forwarder" \
  --mount type=bind,src=/home/TG-Forwarder/forwarder/data,dst=/app/data,readonly \
  --entrypoint python \
  ghcr.io/outofrange007/tg-forwarder:latest \
  -m forwarder.store /app/data/forwarder.db
```

This imports processed messages, the topic mapping and saved settings from the SQLite file and verifies the row counts. Afterwards the file is no longer used (keep it as a backup). Adapt the container name, data path and image to your setup.

## Configuration (.env)

All variables with explanations are in [`.env.example`](.env.example). The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | – | **Required.** PostgreSQL connection, e.g. `postgresql://forwarder:PASSWORD@postgres:5432/forwarder`. All instances share this database |
| `PG_PASSWORD` | – | Docker/Portainer only: password of the postgres container; the compose uses it in `POSTGRES_PASSWORD` and inside `DATABASE_URL` |
| `API_ID`, `API_HASH` | – | Credentials from my.telegram.org |
| `SESSION_STRING` | – | Optional instead of a session file (`login --print-session-string`) |
| `SOURCE_CHAT` / `TARGET_CHAT` | – | `-100…` ID, `@name`, `https://t.me/name` or `https://t.me/c/123/45`. Both can be selected comfortably in the dashboard dropdowns |
| `SOURCE_TOPIC_ID` | – (all) | Only transfer this topic of the source. Empty, `0` or `all` = all topics, `1` = "General". IDs are shown by `python -m forwarder topics` or in the dashboard dropdown |
| `TOPIC_MODE` | `none` | `none` \| `fixed` \| `mirror` (`fixed` if `TARGET_TOPIC_ID` is set) |
| `TARGET_TOPIC_ID` | – | Target topic for `fixed` (selectable in the dashboard dropdown) |
| `MODE` | `copy` | `copy` \| `forward` |
| `HIDE_SENDER` | `true` | With `forward`: without "Forwarded from" |
| `MEDIA_TYPES` | `photo,video,image_file,video_file` (Docker: `all`) | Additionally possible: `animation`, `video_note`, `audio`, `voice`, `sticker`, `document` (any other file such as PDF/ZIP/APK), or `all` = every file format |
| `CAPTION_TEMPLATE` | `{caption}` | Empty = no caption |
| `IGNORE_HISTORY` | `false` | `true` = ignore the transfer history: already transferred messages are transferred again (checkpoint and the list of transferred messages are ignored, not deleted). In the dashboard: "Ignore history" |
| `COPY_TEXT` | `false` | `true` = also copy plain text messages (without photo/video); in the dashboard: "Also copy text-only messages" |
| `DELAY_SECONDS` | `2` | Pause between two sends |
| `START_FROM_ID`, `END_AT_ID` | – | ID range |
| `DATE_FROM`, `DATE_TO` | – | Date range (`2026-01-31` or `2026-01-31T18:00`) |
| `MAX_FILE_SIZE_MB` | `2000` | Larger files are skipped |
| `DOWNLOAD_FALLBACK` | `true` | With content protection, download and re-upload |
| `RESUME` | `true` | Continue from the checkpoint instead of scanning the whole source again |
| `DRY_RUN` | `false` | Dry run |
| `WEB_PORT` / `WEB_PASSWORD` | `5000` / – | Dashboard port and optional password protection (Basic Auth, any username) |
| `DATA_PATH` | `./data` | Data directory |

> You can find the **chat ID** of a group with `python -m forwarder chats` or in the dashboard under "Show my groups/channels". The chat dropdowns in the dashboard are filled automatically (⟳ reloads); a click on "Source"/"Target" in the chat list adopts the chat.
> The **topic ID** is the number in the topic link: `https://t.me/c/1234567890/`**`55`**.

## How it works

1. **Scan:** The source is read in ascending order from the checkpoint. Messages without photo/video as well as already processed ones are dropped; filters are applied. Failed messages from earlier runs are added to the list.
2. **Transfer:** Single media and albums are sent one after another. Before each send, the batch is reserved in the database (`claim`) - if another instance already holds or has finished it, it is skipped. After each send, the result (`ok`/`failed`) with target IDs is stored and the checkpoint is advanced.
3. **Live (optional):** New messages in the source are detected via Telethon events (`NewMessage` and `Album`) and processed the same way.

`Stop` (dashboard) or `Ctrl+C` ends the run after the current file. The next start continues seamlessly.

`python -m forwarder reset` deletes the history. The next run then transfers everything again.

## Web API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard |
| `/api/auth/status` | GET | Configured/logged in? Account name |
| `/api/auth/send_code` | POST | `{"phone": "+49…"}` – request login code |
| `/api/auth/sign_in` | POST | `{"code": "12345"}` – returns `password_required` if needed |
| `/api/auth/password` | POST | `{"password": "…"}` – 2FA |
| `/api/auth/logout` | POST | Log out the session |
| `/api/status` | GET | Progress, statistics, latest events |
| `/api/start` | POST | `{"live": false}` – start a run |
| `/api/stop` | POST | Stop after the current file |
| `/api/settings` | GET/POST | Read/change settings (without secrets) |
| `/api/chats` | GET | Your own groups/channels with IDs (fills the chat dropdowns) |
| `/api/source_topics` | GET | Topics of the source (`?chat=` for another group): `forum`, `topics: [{id, title}]`, `selected` |
| `/api/target_topics` | GET | Topics of the target (`?chat=` for another group): `forum`, `topics: [{id, title}]`, `selected` |
| `/api/history` | GET | Most recently processed messages, topic mapping |
| `/api/reset` | POST | Delete history (`{"topics": true}` incl. topics) |

## Project structure

```
forwarder/
  config.py     load .env, validate, dashboard overrides
  engine.py     scan, send (copy/forward/download fallback), live mode, retry, chat/topic listings
  media.py      media type, filters, albums, captions (pure logic)
  topics.py     topic modes none/fixed/mirror
  tg_compat.py  API calls that differ between Telethon versions
  store.py      PostgreSQL persistence: processed messages, checkpoints, topic mapping, claims; one-time SQLite import (python -m forwarder.store FILE)
  upload_meta.py  metadata checks (32-bit limits) and upload size limits for re-uploading
  state.py      thread-safe progress state with speed/remaining time
  worker.py     Telethon thread for the dashboard (login, jobs, chat/topic queries)
  web.py        Flask app (dashboard API incl. source and target topics)
  cli.py        command line (web, login, run, chats, topics, inspect, status, reset)
  templates/index.html
tests/          unit tests with a fake client (no Telegram access needed)
```

## Tests

```bash
pip install -r requirements-dev.txt
export TEST_DATABASE_URL=postgresql://forwarder_test:test_password@localhost:5432/forwarder_test
python -m pytest -q
```

The suite needs a dedicated PostgreSQL database named `forwarder_test`; every test runs in its own schema, which is created and dropped automatically. GitHub Actions starts a `postgres:16` service for this (see `.github/workflows/docker.yml`).

The tests cover configuration, media detection, filters, albums, captions, persistence, progress, topic mirroring, source topic selection (filter incl. albums, General topic, separate checkpoints without duplicates, live filter, mirror with source topic), the send engine (copy, forward with topic, download fallback incl. broken metadata/32-bit limits and size limit, FloodWait, connection errors, stop, dry run) and the web API (login, settings, chat list, source and target topic endpoints). Telegram is replaced by a fake client.

## Troubleshooting

**`DATABASE_URL is not set. This Store version requires PostgreSQL.`**

The forwarder keeps all state in PostgreSQL. Set `DATABASE_URL` - in Docker the compose builds it from `PG_PASSWORD`; for local runs export the variable before starting (see Quick start).

**`password authentication failed for user "forwarder"`**

The postgres container applies `POSTGRES_PASSWORD` only when its data volume is created for the **first time**. If you changed `PG_PASSWORD` afterwards, the database still knows the old password: set `PG_PASSWORD`/`DATABASE_URL` back to the old value, or delete the volume (this deletes the database; it is then re-initialized with the new password). Also make sure `PG_PASSWORD` is identical in both places and contains no characters that need URL escaping (`:`, `/`, `@`, `?`, `#`).

**`'i' format requires -2147483648 <= number <= 2147483647`** (older versions)

The most common cause is an **invalid target topic ID** (`reply_to_msg_id` in the traceback). A chat ID such as `-1001234567890` was entered in `TARGET_TOPIC_ID` or in the dashboard field "Target topic ID". The topic ID is the small number in the topic link, for example `55` in `https://t.me/c/1234567890/55`. You can also paste the link directly; the ID is then extracted automatically. Invalid values are now rejected with a clear message when saving or starting. An already saved wrong ID aborts the run before anything is sent.

A second, rarer cause is broken video metadata:
Python reports this error when Telethon is asked to write a value into a 32-bit field of the Telegram protocol that does not fit. With the download fallback (source with content protection), this was the video metadata of the re-uploaded video: width, height or duration that `hachoir` reads from the file or that come from the original message. For broken or unusually encoded files this yields absurdly large values.
Since this version:

- Width and height are clamped to valid ranges (invalid becomes `1`). An invalid duration becomes `0`. Attributes that cannot be serialized are discarded.
- If sending still fails on a 32-bit value, the file is sent again without metadata (only file name and default video attribute). The already uploaded file is reused. A warning with the affected field appears in the dashboard.
- If the metadata detection crashes, the attributes of the original message are used.

**Files over 2 GB:** When re-uploading, Telegram allows at most 2000 MB, with Premium 4000 MB. Larger files are skipped before the download with a clear message and marked as failed. Forwarding and copying without content protection are not affected, because nothing is uploaded. `MAX_FILE_SIZE_MB` (default 2000) additionally filters during the scan.

**`API_ID is invalid`:** The `API_ID` from my.telegram.org always fits in 32 bits. If another value is set there (e.g. the phone number or a chat ID), this is reported at startup.

## Security and notes

- **Never** commit or share `.env`, `data/`, `*.session` or the database credentials (`PG_PASSWORD`, `DATABASE_URL`). A session file is full access to your Telegram account.
- The dashboard is meant for a trusted network. Set `WEB_PASSWORD` and do not expose it to the internet without HTTPS/a reverse proxy.
- Comply with the Telegram Terms of Service and with copyright. Bypassing content protection is technically possible, but only permitted for content you have the rights to.
- Mass transfers can trigger FloodWaits or account bans. A generous `DELAY_SECONDS` (2–5 s) helps.
- `cryptg` considerably speeds up download and upload of large files in the fallback. `hachoir` provides duration and resolution of re-uploaded videos.

## License

MIT
