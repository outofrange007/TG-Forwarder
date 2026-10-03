# Telegram Forwarder

A Flask-based web application that transfers **photos and videos** from a Telegram group or channel (source) into another group (target), optionally into **forum topics**. It is based on [Telethon](https://docs.telethon.dev) and is operated through a web dashboard or the command line.

Structure and feature set follow [TG-Uploader](https://github.com/outofrange007/TG-Uploader) (Flask dashboard, Telethon, topic management, progress, persistence, Docker). The difference: instead of uploading local folders, this project reads the media directly from another Telegram chat.

## Features

| Feature | Description |
|---|---|
| Web dashboard | Login (phone → code → 2FA if needed), settings, start/stop, progress bars for file and overall, speed, remaining time, event list |
| Two send modes | `copy`: re-send media without "Forwarded from" (no download needed). `forward`: real forward, optionally without sender |
| Albums | Media with the same `grouped_id` are sent together as an album (max. 10 per album) |
| Topics | `none`, `fixed` (everything into one topic) or `mirror` (topics of the source are created in the target with the same name or reused) |
| Content protection | If the source has "Restrict saving content" enabled, files are downloaded and re-uploaded automatically. Video attributes and thumbnail are preserved, progress is shown |
| Persistence | SQLite database with all processed messages, one checkpoint per source (or per source topic) and the topic mapping. A restart continues where the last run stopped. Failed messages are retried on the next run |
| Source topic selection | If the source is a forum with topics, only **one** topic can optionally be processed (history and live mode, incl. albums). Choose it via the dashboard dropdown, `SOURCE_TOPIC_ID` or `run --source-topic` |
| Live mode | First catches up on the history, then transfers new messages in real time |
| Filters | Media types, ID range, date range, maximum file size, a single source topic |
| Captions | Template with the placeholders `{caption}`, `{filename}` (file name), `{source}`, `{link}`, `{id}`, `{date}`. With `{caption}` the original formatting is preserved |
| Robustness | FloodWait is waited out, after connection drops it reconnects and retries; a single broken message does not end the run |
| Dry run | `DRY_RUN=true` only logs what would be sent |
| Docker | `Dockerfile` and `docker-compose.yml`, data directory as a volume |

## Requirements

- Python 3.10+ (or Docker)
- `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org) → *API development tools*
- A **Telegram user account** (not a bot) that is a member of both source and target.
  *Why not a bot?* Bots cannot read the message history of other groups and channels and would have to be admin in the source. A user account ("userbot") sees everything you also see in the Telegram app.
- In the target: write permission. For `TOPIC_MODE=mirror` additionally the admin right "Manage topics".

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill in API_ID, API_HASH, SOURCE_CHAT, TARGET_CHAT

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

In the dashboard, "Source topic" is a dropdown. The default is "all topics". With ⟳ (or via "Source" in the chat list) the topics of the entered source group are loaded. If the group is not a forum, this is shown and all messages are processed.

Notes:

- Filtering uses the topic identifier of the message (`reply_to.forum_topic` with top ID). All parts of an album therefore end up in the result together. For normal topics Telethon reads only the topic history on the server side. For "General" (ID 1) the entire history is read and filtered locally.
- If a topic is selected but the source is not a forum, or the topic does not exist, the run aborts with a clear error message.
- **Checkpoints** are stored separately per source *and* topic. A run over topic 55 only therefore does not move the checkpoint for "all topics" or for other topics. **Duplicates** are still ruled out: already transferred messages are in the database regardless of topic and are skipped on every later run.
- The target topic modes stay unchanged: `none` and `fixed` work as before. With `mirror`, only the selected topic is mirrored in the target (created or reused).
- `python -m forwarder reset` or Reset in the dashboard also deletes the topic checkpoints of the source.

Login also works directly in the dashboard.

## Docker

```bash
cp .env.example .env              # fill in
docker compose up -d --build      # dashboard: http://<host>:5001
```

You can log in via the dashboard or the console:

```bash
docker compose run --rm telegram-forwarder python -m forwarder login
```

For continuous operation without the dashboard, enable this line in `docker-compose.yml`:
`command: ["python", "-m", "forwarder", "run", "--live"]`

All runtime data is in the volume `./data`:

| File/folder | Purpose |
|---|---|
| `forwarder.session` | Telethon session (**equivalent to a login - keep it secret!**) |
| `forwarder.db` | SQLite: processed messages, checkpoints, topic mapping |
| `settings.json` | Settings changed in the dashboard (override `.env`) |
| `logs/forwarder.log` | Rotating log file (5 MB × 3) |
| `tmp/` | Scratch space for the download fallback (cleaned automatically) |

## Configuration (.env)

All variables with explanations are in [`.env.example`](.env.example). The most important ones:

| Variable | Default | Meaning |
|---|---|---|
| `API_ID`, `API_HASH` | – | Credentials from my.telegram.org |
| `SESSION_STRING` | – | Optional instead of a session file (`login --print-session-string`) |
| `SOURCE_CHAT` / `TARGET_CHAT` | – | `-100…` ID, `@name`, `https://t.me/name` or `https://t.me/c/123/45` |
| `SOURCE_TOPIC_ID` | – (all) | Only transfer this topic of the source. Empty, `0` or `all` = all topics, `1` = "General". IDs are shown by `python -m forwarder topics` |
| `TOPIC_MODE` | `none` | `none` \| `fixed` \| `mirror` (`fixed` if `TARGET_TOPIC_ID` is set) |
| `TARGET_TOPIC_ID` | – | Target topic for `fixed` |
| `MODE` | `copy` | `copy` \| `forward` |
| `HIDE_SENDER` | `true` | With `forward`: without "Forwarded from" |
| `MEDIA_TYPES` | `photo,video,image_file,video_file` | Additionally possible: `animation`, `video_note`, or `all` |
| `CAPTION_TEMPLATE` | `{caption}` | Empty = no caption |
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

> You can find the **chat ID** of a group with `python -m forwarder chats` or in the dashboard under "Show my groups/channels". There you can adopt it as source or target with a click.
> The **topic ID** is the number in the topic link: `https://t.me/c/1234567890/`**`55`**.

## How it works

1. **Scan:** The source is read in ascending order from the checkpoint. Messages without photo/video as well as already processed ones are dropped; filters are applied. Failed messages from earlier runs are added to the list.
2. **Transfer:** Single media and albums are sent one after another. After each send, the result (`ok`/`failed`) with target IDs is stored in the database and the checkpoint is advanced.
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
| `/api/chats` | GET | Your own groups/channels with IDs |
| `/api/source_topics` | GET | Topics of the source (`?chat=` for another group): `forum`, `topics: [{id, title}]`, `selected` |
| `/api/history` | GET | Most recently processed messages, topic mapping |
| `/api/reset` | POST | Delete history (`{"topics": true}` incl. topics) |

## Project structure

```
forwarder/
  config.py     load .env, validate, dashboard overrides
  engine.py     scan, send (copy/forward/download fallback), live mode, retry
  media.py      media type, filters, albums, captions (pure logic)
  topics.py     topic modes none/fixed/mirror
  tg_compat.py  API calls that differ between Telethon versions
  store.py      SQLite persistence
  upload_meta.py  metadata checks (32-bit limits) and upload size limits for re-uploading
  state.py      thread-safe progress state with speed/remaining time
  worker.py     Telethon thread for the dashboard (login, jobs)
  web.py        Flask app
  cli.py        command line (web, login, run, chats, topics, inspect, status, reset)
  templates/index.html
tests/          unit tests with a fake client (no Telegram access needed)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests cover configuration, media detection, filters, albums, captions, persistence, progress, topic mirroring, source topic selection (filter incl. albums, General topic, separate checkpoints without duplicates, live filter, mirror with source topic), the send engine (copy, forward with topic, download fallback incl. broken metadata/32-bit limits and size limit, FloodWait, connection errors, stop, dry run) and the web API. Telegram is replaced by a fake client.

## Troubleshooting

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

- **Never** commit or share `.env`, `data/` and `*.session`. A session file is full access to your Telegram account.
- The dashboard is meant for a trusted network. Set `WEB_PASSWORD` and do not expose it to the internet without HTTPS/a reverse proxy.
- Comply with the Telegram Terms of Service and with copyright. Bypassing content protection is technically possible, but only permitted for content you have the rights to.
- Mass transfers can trigger FloodWaits or account bans. A generous `DELAY_SECONDS` (2–5 s) helps.
- `cryptg` considerably speeds up download and upload of large files in the fallback. `hachoir` provides duration and resolution of re-uploaded videos.

## License

MIT
