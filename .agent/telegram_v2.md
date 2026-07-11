# Telegram Bot Layer — Build Plan (v2)

Source of truth for the Telegram bot layer. Companion to `architecture_v2.md`
§ 6–8 (Telegram Bot, Conversation Sessions & History, Push Delivery). Read
those sections first — this document only covers the breakdown and build
order for the Telegram layer itself.

This layer is **two separate bots**, each with its own token and its own
polling loop:

- **Chat bot** — the agent's channel. Receives free-text from the user,
  calls `POST /chat`, and delivers the agent's reply back — text, plus any
  attachment (typically a PDF) the agent returned in the same response.
  Nothing else.
- **Notifications bot** — the scheduler's outbound channel. Pushes approval
  cards, follow-up cards, digests, and notices, and receives the button
  taps from those cards (the `action` and `follow-up` endpoints). No
  free-text.

Splitting the two bots onto separate channels removes the push-coexistence
problem entirely: a notification never has to yield to an in-progress chat,
because they are physically different channels. That deletes the nudge /
coalesce / hold / flush machinery that earlier drafts carried.

---

## Step 1 — Scope Boundary

The Telegram layer is a **thin transport layer**, split across two bots. It
owns exactly two things:

1. **Inbound** — receive free-text (chat bot) and button taps (notifications
   bot) from the user, and route each to the correct backend endpoint over
   HTTP.
2. **Outbound** — send pushes, PDFs, and inline keyboards to the user
   through the notifications bot. That bot is handed to the scheduler as a
   client instance; the scheduler invokes its send methods.

It does **not** own:

- Business logic (no FSM decisions, no scoring, no drafting)
- Database writes (all writes happen behind the backend endpoints)
- LLM calls (only `POST /chat` triggers one, inside the agent, downstream)
- Scheduling (APScheduler and the backend call the send functions, not the
  other way around)
- Conversation history or sessions (the backend's session manager and the
  agent's conversation store own all of this — see below)
- Session boundaries. The bot sends **no** session-open, session-close, or
  idle-timeout signals. Every user message is just `POST /chat`; the backend
  infers open / continue / timeout from that traffic. Idle-timeout lives in
  the backend's session manager, not in this layer and not in the scheduler.
- Content structuring (digest contents, follow-up wording, push copy are
  produced upstream and handed down as finished text; Telegram only does
  Telegram-flavoured *rendering* — markdown, button layout, document upload)
- The FSM. On a button tap the bot carries an opaque `UserAction` string
  from the button into the request body; it does not import the enum,
  validate membership, or map actions to edges. All server-side.

### The two dependency arrows (they point opposite ways and never cross)

- **Push path (outbound):** the backend constructs the
  `NotificationTelegramClient` and hands it to the scheduler. The scheduler
  invokes `send_*` on it → the user. It only ever pushes; it never pulls,
  and it holds no state.
- **Receive path (inbound):** user → Telegram servers → a bot's polling loop
  → a handler → the handler makes an **HTTP call to the backend**. Neither
  bot is handed a return-path client by the scheduler. Handlers only ever
  react to incoming updates; they never push proactively.

### Backend endpoints this layer depends on

- `POST /chat` — the agent's channel (chat bot). In: `ChatRequest { message }`
  (required, non-empty) — nothing else; the backend's session manager and
  the agent's conversation store rebuild all context, so there is no session
  field to pass. Out: `200` `ChatResponse { reply, attachments? }` — the
  reply text plus, when the agent produced a file, its bytes as a base64
  string per attachment (typically a tailored PDF). Errors: `422`
  missing/empty `message`; `500` if the agent raises (thin transport does
  not interpret agent failures). The only path that originates an LLM call.
  Because the agent never speaks unprompted — every utterance is the return
  value of an open `/chat` call — this response carries everything the agent
  produces. The agent therefore holds no Telegram client; the chat bot, not
  the backend, forwards any attachment on to Telegram.
- `POST /jobs/{job_id}/action` — button tap on the notifications bot. Path
  param `job_id: integer`. In: `ActionRequest { action: UserAction }`
  (required). Out: `200` → the updated `Job` (post-transition). Errors:
  `422` body invalid or `action` not a `UserAction` member; `404` no job
  with `job_id` (`JobNotFoundError`); `409` the mapped transition is illegal
  from the job's current state (`InvalidTransitionError`). Calls
  `transition_status(job_id, target)` after mapping `action` → target
  `Status`. No agent, no LLM.
- `POST /jobs/{job_id}/follow-up` — "Sent it" tap on the notifications bot.
  Path param `job_id: integer`. In: `FollowUpRequest { note: string | null =
  null }`; the body may be empty (`{}`), and this layer always sends `{}`
  (note deferred). Out: `200` → the updated `Job` (`follow_up_count`
  incremented, `last_follow_up_at` stamped, `status_changed_at` unchanged).
  Errors: `422` `note` present but not a string; `404` no job with `job_id`
  (`JobNotFoundError`); `409` job is not `APPLIED` (`InvalidStateError`).
  Calls `record_follow_up(job_id, note)`. No agent, no LLM.

---

## Step 2 — Requirements

**Chat bot — inbound:**

1. Free-text messages are sent to `POST /chat` with body `{ message }` —
   only the message, no context or session field. An empty message is
   guarded locally before sending. On `200` the bot delivers the response
   to the user: it sends `reply` as text, then, if `attachments` is present
   and non-empty, base64-decodes each attachment and sends it as a document
   (the expected file type is PDF). The reply text is sent as-is — no
   parsing, no reformatting. Errors `422` and `500` route through the error
   handler.
2. `/start` is Telegram's conventional first-contact command. It sends the
   static greeting (`"What can I do for you today?"`) locally and signals
   nothing to the backend. There is no `/end` command and no session
   endpoint — session boundaries are the backend's inference from `/chat`
   traffic.

**Notifications bot — inbound (button taps):**

3. Button presses are routed by the `kind` discriminator in `callback_data`:
   - `kind: "action"` → `POST /jobs/{job_id}/action` with body
     `{ action: <UserAction string> }`, read verbatim from `callback_data`;
     the bot never maps or interprets it.
   - `kind: "followup"` → `POST /jobs/{job_id}/follow-up` with an empty body
     (`{}`); `note` is never sent.
   - `kind: "dismiss"` → acknowledge the callback, no backend call.
   `job_id` comes from the same `callback_data`.
4. The bot surfaces the backend's response on both posting paths. On success
   (`200`, updated `Job`) it sends a confirmation. Each rejection routes
   through the error handler as a readable message:
   - action path: `422` (bad action), `404` (no job), `409` (illegal
     transition).
   - follow-up path: `422` (bad body), `404` (no job), `409` (job not
     `APPLIED`).

**Chat bot — outbound send surface (`ChatTelegramClient`):**

5. `send_message(text)` — plain text; the agent's `reply`.
6. `send_document(text, pdf_bytes)` — a decoded attachment (PDF) from the
   `/chat` response, delivered as a document.

**Notifications bot — outbound send surface (`NotificationTelegramClient`,
called by the scheduler):**

7. `send_message(text)` — plain text. Used by digest, ghost notice, and any
   simple notification.
8. `send_document(text, pdf_bytes)` — text + PDF attachment. Used by the
   tailor job for `PENDING_APPROVAL` pushes.
9. `send_message_with_keyboard(text, keyboard)` — text + inline keyboard.
   Used by the tailor job (`[Mark Applied] [Skip]`) and follow-up job
   (`[Sent it] [Skip]`).

**Button vocabulary:** every button encodes a `kind` plus `job_id`, and
`kind: "action"` buttons also carry a `UserAction` string the bot treats as
opaque. Phase 1 `UserAction` values, matching backend `enums.py`: `APPLIED`,
`USER_SKIPPED`, `INTERVIEWING`, `OFFER`, `ACCEPTED`, `DECLINED`, `REJECTED`.
The approval card uses `[Mark Applied]` → `APPLIED`, `[Skip]` →
`USER_SKIPPED`. The follow-up card uses `[Sent it]` → `kind: followup` and
`[Skip]` → `kind: dismiss`.

**Both bots:**

10. Only one authorised user. Any update (message, callback, command) from
    an unrecognised `chat_id` is logged as a warning and ignored — no reply
    sent. Same guard on both bots.
11. A failed backend HTTP call on any inbound path logs the error via loguru
    and sends the user a plain text error message — never a silent failure.
    This includes surfacing `422`, `404`, `409`, and `500` as readable
    messages rather than raw status codes.

---

## Step 3 — Data Model & API Contract

Neither bot has a database, and after the two-bot split neither holds any
in-memory state — both are fully stateless.

**`callback_data` schema** (notifications bot) — every inline keyboard button
encodes a JSON string (well under Telegram's 64-byte limit). A `kind`
discriminator selects the endpoint; `kind: "action"` buttons also carry a
`UserAction` string copied verbatim into the request body.

```json
{"kind": "action", "action": "APPLIED", "job_id": 42}
{"kind": "action", "action": "USER_SKIPPED", "job_id": 42}
{"kind": "followup", "job_id": 42}
{"kind": "dismiss", "job_id": 42}
```

**`ChatResponse` shape** (chat bot) — the `/chat` response body:

```json
{
  "reply": "Here is your tailored resume.",
  "attachments": [
    {"filename": "resume.pdf", "content_b64": "<base64 string>", "mime": "application/pdf"}
  ]
}
```

`attachments` is optional and may be absent, empty, or carry one or more
files. Each file's bytes are a base64 string the chat bot decodes before
sending as a document. Expected type is PDF.

**Outbound interfaces — one client class per bot, no shared class.**

Chat bot (`ChatTelegramClient`) — delivers the agent's reply and any
decoded attachment:

```python
class ChatTelegramClient:
    async def send_message(self, text: str) -> None: ...
    async def send_document(self, text: str, pdf_bytes: bytes) -> None: ...
```

Notifications bot (`NotificationTelegramClient`) — the scheduler's push
surface:

```python
class NotificationTelegramClient:
    async def send_message(self, text: str) -> None: ...
    async def send_document(self, text: str, pdf_bytes: bytes) -> None: ...
    async def send_message_with_keyboard(
        self, text: str, keyboard: list[list[InlineKeyboardButton]]
    ) -> None: ...
```

`chat_id` is never a parameter — it is a config value baked into each client
at construction. Each bot talks to one user. No nudge method, no tracked
`message_id`, no `clear_nudge` — the two-bot split removed all of it. Both
clients are stateless.

**Backend calls each bot makes (inbound path):**

| Bot | Trigger | Method | Endpoint | Body / result |
|---|---|---|---|---|
| Chat | free-text | POST | `/chat` | in `{ message }` → out `{ reply, attachments? }`; `422` empty, `500` agent raised |
| Notifications | `kind: action` | POST | `/jobs/{job_id}/action` | in `{ action }` → `Job`; `422`/`404`/`409` |
| Notifications | `kind: followup` | POST | `/jobs/{job_id}/follow-up` | in `{}` → `Job`; `422`/`404`/`409` |
| Notifications | `kind: dismiss` | — | — | acknowledge, no call |

The bot holds no `UserAction` type, no status strings, and no FSM logic. It
copies the action string from `callback_data` into the body and surfaces
whatever the backend returns.

**Conversation history / sessions:** owned entirely by the backend session
manager and the agent's conversation store. `POST /chat` carries only
`{ message }`; the bot sends no session signals at all (architecture § 7).

---

## Step 4 — Work Packages

**WP-T1: Bootstrap (both bots)**
Read the two tokens (`TELEGRAM_CHAT_BOT_TOKEN`,
`TELEGRAM_NOTIFICATIONS_BOT_TOKEN`) and `TELEGRAM_CHAT_ID` from settings.
Build two `Application` instances with their own polling loops, wire the
chat handlers onto one and the notification handlers onto the other, both
behind the shared auth guard. Construct both send clients
(`ChatTelegramClient`, `NotificationTelegramClient`) and both backend HTTP
clients, stashing each on the right app's `bot_data`. Register in the
FastAPI lifespan hook alongside APScheduler; verify each bot with
`get_me()`. Returns the `NotificationTelegramClient` for the scheduler's
push path.

**WP-T2: Auth guard (shared)**
One guard, applied on every inbound update of both bots — if `chat_id` does
not match `TELEGRAM_CHAT_ID`, log a warning and ignore. No reply sent.

**WP-T3: Outbound interfaces (both bots)**
Implement `ChatTelegramClient` (`send_message`, `send_document`) and
`NotificationTelegramClient` (`send_message`, `send_document`,
`send_message_with_keyboard`). Two distinct classes, no shared base; each
constructed against its own bot and token. Both stateless. Unit-testable by
mocking the `python-telegram-bot` `Bot` object's send calls.

**WP-T4: Chat handler (chat bot)**
Handle free-text — guard non-empty, POST `{ message }` to `/chat`. On `200`,
send `reply` via `ChatTelegramClient.send_message`; then, if `attachments`
is present and non-empty, base64-decode each and send it via
`send_document` (expected type PDF). Also handle `/start`: send the static
greeting locally, no backend call. No history, no session, no state.
`422`/`500`/transport failures route through WP-T6.

**WP-T5: Button callback handler (notifications bot)**
Handle `callback_query` updates — parse `callback_data`, branch on `kind`
(`action` → action endpoint, `followup` → follow-up endpoint, `dismiss` →
no-op). Always `answer_callback_query`. On `200`, send a confirmation; on
`422`/`404`/`409` or transport failure, route through WP-T6. Does not
interpret the `UserAction` — copies the string through.

**WP-T6: Error handler (shared)**
Centralised handling for failed backend HTTP calls — log via loguru, send a
plain user-facing message. Maps `422`/`404`/`409`/`500` to readable text
rather than raw codes. Used by both bots' handlers.

---

## Step 5 — Build & Test Order

1. **WP-T2 (auth guard)** — first; both bots' handlers depend on it. Test:
   mismatched `chat_id` → short-circuit, logged warning, no reply; matching
   → passes through.

2. **WP-T6 (error handler)** — before the handlers so they use it rather
   than retrofit it. Test: force a backend call to raise and to return
   `422`/`404`/`409`/`500`; assert each is logged and a plain readable
   message is sent.

3. **WP-T3 (outbound interfaces)** — before the handlers and before wiring
   the scheduler's push path. Test both clients by mocking the `Bot` object:
   `ChatTelegramClient` (`send_message`, `send_document`) and
   `NotificationTelegramClient` (all three primitives).

4. **WP-T4 (chat handler)** — once T2, T3, T6 exist. Test: mock `POST /chat`,
   assert `{ message }` is forwarded (no extra fields); on a reply-only
   response, `reply` is sent via `send_message` and no document is sent; on
   a response with `attachments`, each is base64-decoded and sent via
   `send_document` after the text; assert an empty message is guarded before
   sending; assert `/start` sends the greeting with no backend call;
   `422`/`500` route through T6.

5. **WP-T5 (button handler)** — once T2, T3, T6 exist. Test each `kind`
   branch: `action` posts `{ action }` to `/jobs/{id}/action` (body matches
   the `callback_data` string verbatim) and confirms on `200`; `followup`
   posts `{}` to `/jobs/{id}/follow-up` and confirms on `200`; `dismiss`
   makes no backend call. Assert `422`/`404`/`409` on both posting branches
   route through T6, and `answer_callback_query` fires in every case.

6. **WP-T1 (bootstrap)** — last; it composes everything. Test: mock
   `Bot.get_me()` for both tokens, assert two `Application` instances are
   built, the right handlers and clients are registered on each, the auth
   guard is attached to both, and the `NotificationTelegramClient` is
   returned for the scheduler.

**No external dependency remains open.** All three backend contracts
(`/chat`, `/jobs/{job_id}/action`, `/jobs/{job_id}/follow-up`) are locked.
There are no session endpoints to wait on.

---

## Locked Decisions Log

- **Two separate bots, two tokens, two polling loops.** Chat bot owns
  `/chat`; notifications bot owns the outbound push surface plus the
  `action` / `follow-up` button endpoints. The split is physical, not
  logical.
- **Separating the bots eliminated the push-coexistence problem.** No
  nudge, no coalescing, no hold/deliver gating, no flush path, no tracked
  `message_id`. Both bots are stateless.
- **The bot sends no session signals.** No `/end`, no session-open /
  session-close endpoints, no idle-timeout involvement. Every user message
  is just `POST /chat`; the backend's session manager infers open /
  continue / timeout. Idle-timeout is the session manager's job, not the
  scheduler's and not this layer's.
- **`/start` is a local greeting only.** It sends the static constant and
  signals nothing to the backend. Auto-open (the backend inferring a new
  session from the first `/chat`) is the real mechanism.
- **The agent holds no Telegram client, and the chat bot forwards
  attachments.** The agent never speaks unprompted — every utterance is the
  return value of an open `/chat` call — so `POST /chat` returns everything
  the agent produces: `{ reply, attachments? }`. Attachments are base64
  strings (expected type PDF); the chat bot decodes them and sends them as
  documents. The backend does not touch Telegram; the transport layer does
  all sending.
- **Two send-client classes, one per bot, no shared class.**
  `ChatTelegramClient` (`send_message`, `send_document`) and
  `NotificationTelegramClient` (`send_message`, `send_document`,
  `send_message_with_keyboard`). The chat bot needs `send_document` because
  of the `/chat` attachment; the split keeps the two bots physically
  separate.
- **Inbound routes to the backend over HTTP, not through the scheduler.**
  The scheduler receives the `NotificationTelegramClient` for the push path
  but hands neither bot a return-path client.
- **A `kind` discriminator in `callback_data` selects the endpoint.**
  `action` (carries a `UserAction`, → action endpoint), `followup` (no
  action, → follow-up endpoint), `dismiss` (no-op). Needed because
  `[Sent it]` and the approval buttons hit different endpoints.
- **The `UserAction` value lives in `callback_data`, copied verbatim into
  the request body.** The scheduler bakes the target-state string into the
  button at push time. The bot does not import `UserAction`, validate it, or
  map it — opaque string to this layer.
- **The bot knows nothing about the FSM.** `UserAction` names a target
  state; the backend validates the `(current → target)` pair. `422` bad
  action, `404` no job, `409` illegal transition — the bot only surfaces
  these.
- **The follow-up path is a distinct endpoint** — `[Sent it]` posts `{}` to
  `/jobs/{job_id}/follow-up`. `note` deferred, never sent. `409` if the job
  is not `APPLIED`.
- `job_id` in `callback_data` is the backend's internal primary key, never a
  portal-native identifier (MCF UUID, Careers@Gov `objectID`, …), which live
  in `metadata`.
- The greeting string is a hardcoded constant in this layer — not
  agent-generated, not personalised.
- `NotificationTelegramClient` is push-only and never pulls;
  `ChatTelegramClient` only ever sends in direct response to an inbound
  `/chat` turn. Inbound arrives through each bot's own polling loop, handled
  by separate handler objects.

---

## Skeleton — Files & Functions

Two sub-packages under `telegram/`, one per bot, plus a `shared/` package for
what both use. Each function is tagged with its work package.

```
telegram/
  __init__.py
  shared/
    __init__.py
    auth.py          # WP-T2: auth guard (both bots)
    errors.py        # WP-T6: error handler (both bots)
    bootstrap.py     # WP-T1: builds BOTH Application instances + clients
  chat/
    __init__.py
    client.py        # WP-T3: ChatTelegramClient (send_message, send_document)
    handlers.py      # WP-T4: free-text → /chat, deliver reply + attachments;
                     #   local /start greeting
    agent_client.py  # AgentBackendClient for POST /chat
  notifications/
    __init__.py
    client.py        # WP-T3: NotificationTelegramClient (push surface)
    handlers.py      # WP-T5: button taps → action / follow-up endpoints
    notify_client.py # NotifyBackendClient for action / follow-up endpoints
```

### `telegram/shared/auth.py` — WP-T2

```python
def is_authorised(update: Update, allowed_chat_id: int) -> bool:
    """True if the update's chat_id matches the single allowed user.
    Covers message, callback_query, and command updates. Same guard on
    both bots; unauthorised updates are logged (warning) and ignored."""
    ...
```

### `telegram/shared/errors.py` — WP-T6

```python
async def handle_backend_error(
    client, exc: Exception, context: str
) -> None:
    """Log via loguru and send the user a plain error message via whichever
    send-client the calling bot uses (ChatTelegramClient or
    NotificationTelegramClient). Called from either bot's handlers when a
    backend HTTP call fails or returns 422 / 404 / 409 / 500. Maps status
    codes to readable text."""
    ...
```

### `telegram/shared/bootstrap.py` — WP-T1

```python
def build_applications(
    chat_bot_token: str,
    notifications_bot_token: str,
    chat_id: int,
    backend_base_url: str,
) -> tuple[Application, Application, NotificationTelegramClient]:
    """Build both Application instances (chat + notifications), each with
    its own polling loop and the shared auth guard. Wire chat.handlers onto
    the chat app and notifications.handlers onto the notifications app.
    Construct both send clients (ChatTelegramClient,
    NotificationTelegramClient) and both backend clients (AgentBackendClient,
    NotifyBackendClient), stashing each on the right app.bot_data. Return
    (chat_app, notifications_app, notification_client) — the last for the
    scheduler's push path."""
    ...

async def start_bots(chat_app: Application, notifications_app: Application) -> None:
    """Verify each with get_me(), start both polling loops.
    Called from the FastAPI lifespan hook alongside APScheduler."""
    ...

async def stop_bots(chat_app: Application, notifications_app: Application) -> None:
    """Graceful shutdown of both, called on lifespan teardown."""
    ...
```

### `telegram/chat/agent_client.py`

```python
class AgentBackendClient:
    """Thin httpx wrapper for the agent's chat endpoint."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None: ...

    async def post_chat(self, message: str) -> dict:
        """POST /chat with {"message": message} — nothing else.
        Returns the response body { reply, attachments? }, where each
        attachment carries base64-encoded bytes (expected type PDF).
        Surfaces 422 (empty message) / 500 (agent raised)."""
        ...
```

### `telegram/chat/client.py` — WP-T3

```python
class ChatTelegramClient:
    """Send surface for the chat bot. chat_id baked in at construction.
    Stateless — only ever sends in response to an inbound /chat turn."""

    def __init__(self, bot: Bot, chat_id: int) -> None: ...

    async def send_message(self, text: str) -> None: ...

    async def send_document(self, text: str, pdf_bytes: bytes) -> None: ...
```

### `telegram/chat/handlers.py` — WP-T4

```python
async def handle_message(update: Update, ctx: Context) -> None:
    """Free-text → guard non-empty → AgentBackendClient.post_chat.
    On 200: send `reply` via ChatTelegramClient.send_message; then for each
    item in `attachments` (if present), base64-decode and send via
    send_document (expected PDF). No history, no session, no state.
    Errors route through shared.errors.handle_backend_error."""
    ...

async def handle_start(update: Update, ctx: Context) -> None:
    """Send the static GREETING locally. No backend call."""
    ...

GREETING = "What can I do for you today?"  # WP-T4 static constant
```

### `telegram/notifications/client.py` — WP-T3

```python
class NotificationTelegramClient:
    """Outbound push surface for the notifications bot. chat_id is baked in
    at construction. Stateless — push-only, never pulls."""

    def __init__(self, bot: Bot, chat_id: int) -> None: ...

    async def send_message(self, text: str) -> None: ...

    async def send_document(self, text: str, pdf_bytes: bytes) -> None: ...

    async def send_message_with_keyboard(
        self, text: str, keyboard: list[list[InlineKeyboardButton]]
    ) -> None: ...
```

### `telegram/notifications/notify_client.py`

```python
class NotifyBackendClient:
    """Thin httpx wrapper for the notification channel's button endpoints."""

    def __init__(self, base_url: str, http: httpx.AsyncClient) -> None: ...

    async def post_action(self, job_id: int, action: str) -> dict:
        """POST /jobs/{job_id}/action with {"action": action}.
        Returns the updated Job on 200. Surfaces 422 / 404 / 409."""
        ...

    async def post_followup(self, job_id: int) -> dict:
        """POST /jobs/{job_id}/follow-up with an empty body {} (note omitted).
        Returns the updated Job on 200. Surfaces 422 / 404 /
        409 (job not APPLIED)."""
        ...
```

### `telegram/notifications/handlers.py` — WP-T5

```python
async def handle_callback(update: Update, ctx: Context) -> None:
    """Parse callback_data, branch on kind:
      action   -> NotifyBackendClient.post_action(job_id, action)
      followup -> NotifyBackendClient.post_followup(job_id)
      dismiss  -> no backend call
    Always answer_callback_query; confirm on 200; 422/404/409/transport
    errors route through shared.errors.handle_backend_error. Does NOT
    interpret the UserAction — the string is copied through."""
    ...

def _parse_callback_data(raw: str) -> dict:
    """Return {"kind": str, "job_id": int, "action": str | None} — raises on
    malformed payload. `action` is present only for kind == "action" and is
    an opaque UserAction string the bot never maps."""
    ...
```

**Test files** mirror the two sub-packages plus `shared/`:

```
tests/telegram/
  shared/
    test_auth.py       # WP-T2: match / mismatch, both bots
    test_errors.py     # WP-T6: log + user message on 422/404/409/500
    test_bootstrap.py  # WP-T1: two apps built, handlers + guard wired,
                       #   notifications client returned
  chat/
    test_client.py        # WP-T3: ChatTelegramClient send_message/send_document
    test_agent_client.py  # post_chat: 200 (reply, reply+attachments) / 422 / 500
    test_handlers.py      # WP-T4: forwarding, empty guard, /start,
                          #   reply-only vs reply+decoded-attachment delivery
  notifications/
    test_client.py        # WP-T3: NotificationTelegramClient, three primitives
    test_notify_client.py # post_action / post_followup: 200 + 422/404/409
    test_handlers.py      # WP-T5: kind routing (action/followup/dismiss),
                          #   action passthrough verbatim
```