# Telegram Bot Layer

Status: implemented. Package lives at `src/telegram_bot/`. Companion to
`architecture_v2.md` § 6–8 (Telegram Bot, Conversation Sessions & History,
Push Delivery) — read those for the design rationale (why two bots, why the
push-coexistence problem disappears). This document covers only what was
built and how to use it.

**Deviation from the original plan:** the package is `telegram_bot/`, not
`telegram/` — `telegram/` collides with the installed `python-telegram-bot`
library (`import telegram`), which sits on the same `sys.path` (`pythonpath
= src` in `pytest.ini`). Everything else matches the design as planned.

---

## Summary

Two independent bots, each its own token, each its own polling loop, wired
into the FastAPI lifespan alongside APScheduler:

- **Chat bot** — free-text → `POST /chat` → relays `reply` (+ any decoded
  PDF attachment) back. Also handles `/start` (static greeting, no backend
  call).
- **Notifications bot** — receives button taps (`callback_data` JSON) →
  routes to `POST /jobs/{id}/action` or `POST /jobs/{id}/follow-up`, or
  no-ops on `dismiss`. Its send surface (`NotificationTelegramClient`) is
  exposed via `app.state.notification_client` for the scheduler to push
  through — the scheduler is not part of this layer.

Both bots are stateless (no DB, no in-memory session/history) and share one
authorised `chat_id`. Every backend HTTP failure (422/404/409/500 or a
transport error) is surfaced as a distinct, readable Telegram message —
never a silent failure, and a 404 ("job not found") never reads the same as
a 409 ("wrong state for that action").

```
src/telegram_bot/
  shared/
    auth.py        is_authorised(update, allowed_chat_id) -> bool
    errors.py       BackendError, extract_detail(resp), handle_backend_error(...)
    bootstrap.py    build_applications(...), start_bots(...), stop_bots(...)
  chat/
    client.py       ChatTelegramClient  — send_message, send_document
    agent_client.py AgentBackendClient  — post_chat(message) -> dict
    handlers.py      handle_message, handle_start, GREETING
  notifications/
    client.py        NotificationTelegramClient — send_message, send_document,
                      send_message_with_keyboard
    notify_client.py NotifyBackendClient — post_action, post_followup
    handlers.py       handle_callback, _parse_callback_data
```

Tests mirror this tree under `src/test/telegram_bot/` (53 tests). Note:
files that would collide on basename across `chat/`/`notifications/` are
disambiguated by name (`test_chat_client.py` / `test_notifications_client.py`,
etc.) rather than by adding `__init__.py` — the latter reproduces the same
`telegram_bot` package-name collision inside the test tree.

---

## How to use this layer

**1. Configure two bot tokens** (`app/config.py` / `.env`) — get each from
`@BotFather`, one shared `chat_id`:

```
TELEGRAM_CHAT_BOT_TOKEN=
TELEGRAM_NOTIFICATIONS_BOT_TOKEN=
TELEGRAM_CHAT_ID=0
```

**2. It's already wired into `app/main.py`'s lifespan** — nothing to call
manually in normal operation:

```python
chat_app, notifications_app, notification_client = build_applications(
    chat_bot_token=settings.telegram_chat_bot_token,
    notifications_bot_token=settings.telegram_notifications_bot_token,
    chat_id=settings.telegram_chat_id,
    backend_base_url=settings.api_base_url,
)
app.state.notification_client = notification_client
await start_bots(chat_app, notifications_app)
...
await stop_bots(chat_app, notifications_app)  # on teardown
```

**3. The scheduler pushes through `app.state.notification_client`** (a
`NotificationTelegramClient`) once the scheduling layer is built:

```python
await notification_client.send_message("weekly digest text")
await notification_client.send_document(caption_text, pdf_bytes, filename="resume.pdf")
await notification_client.send_message_with_keyboard(text, keyboard)
```

**4. Build approval/follow-up keyboards** with `callback_data` as a JSON
string carrying a `kind` discriminator, read verbatim by the notifications
bot — it never imports `UserAction` or validates membership:

```python
import json
from telegram import InlineKeyboardButton

keyboard = [[
    InlineKeyboardButton("Mark Applied", callback_data=json.dumps({"kind": "action", "action": "APPLIED", "job_id": job.id})),
    InlineKeyboardButton("Skip", callback_data=json.dumps({"kind": "action", "action": "USER_SKIPPED", "job_id": job.id})),
]]
# follow-up card:
# {"kind": "followup", "job_id": job.id}  ->  [Sent it]
# {"kind": "dismiss",  "job_id": job.id}  ->  [Skip]
```

**5. The `/chat` response contract** the chat bot expects (`ChatResponse` in
`app/routes/chat.py`):

```json
{
  "reply": "Here is your tailored resume.",
  "attachments": [
    {"kind": "cv_pdf", "filename": "resume.pdf", "mime_type": "application/pdf", "content_b64": "<base64>"}
  ]
}
```

`attachments` may be absent or empty; each entry's `content_b64` is
base64-decoded and sent as a document after the reply text.

**6. Error messages** are centralised in `shared/errors.py` — `BackendError`
carries `(status_code, detail)`, and `handle_backend_error` maps it to
distinct copy per code (422 bad request, 404 job missing, 409 illegal
transition/state, 500 generic) plus a separate "backend unreachable" message
for transport failures. Both bots' handlers route every failure through it —
extend `_STATUS_MESSAGES` there if a new status code needs its own wording.

---

## Deferred / out of scope for this layer

- **Agent wiring.** `POST /chat` currently raises `NotImplementedError`
  (`app/dependencies.py::get_agent` returns `None`) — unrelated pre-existing
  gap, doesn't block this layer's own tests, but blocks a real end-to-end
  chat smoke test until the agent lands.
- **Scheduler.** APScheduler starts with no jobs registered; nothing yet
  calls `notification_client`'s send methods for real.
- **`note` on follow-up.** `NotifyBackendClient.post_followup` always sends
  `{}` — the `note` field is deferred per architecture_v2.md.
