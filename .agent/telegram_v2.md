# Telegram Bot Layer — Build Plan (v2)

Source of truth for the Telegram bot layer. Companion to `architecture_v2.md`
§ 6–8 (Telegram Bot, Conversation Sessions & History, Push Delivery &
Conversation Coexistence). Read those sections first — this document only
covers the breakdown and build order for the Telegram layer itself.

---

## Step 1 — Scope Boundary

The Telegram bot is a **thin transport layer**. It owns exactly two things:

1. **Inbound** — receive messages, commands, and button presses from the
   user, route them to the right backend endpoint.
2. **Outbound** — send pushes, PDFs, inline keyboards, and nudges to the
   user when the pipeline or backend calls it.

It does **not** own:

- Business logic (no FSM decisions, no scoring, no drafting)
- Database writes (all writes go through the backend API)
- LLM calls (the agent handles those)
- Scheduling (APScheduler and the backend call the bot's send functions,
  not the other way around)
- Conversation history (the agent layer's `ConversationContext` owns this
  entirely — see architecture § 7)
- Content structuring (digest contents, follow-up wording, push copy are
  produced upstream and handed down as finished text; Telegram only does
  Telegram-flavoured *rendering* — markdown, button layout, document
  upload — never *structuring*)
- The decision of *whether* to flush queued pushes or hold them during an
  active conversation (architecture § 8) — Telegram executes what it is
  told, it does not decide timing

**What calls the bot outbound?** Scheduler jobs and the backend's session-
close handler call the bot's send functions directly. The bot is a
dependency injected into those callers, same pattern as the HTTP client or
LiteLLM client.

**What does the bot call inbound?**

- Free-text → `POST /chat`
- Button press → `PATCH /jobs/{id}/status` or `POST /jobs/{id}/follow-up`
- `/start` → `POST /session/start`
- `/end` → `POST /session/end`

---

## Step 2 — Requirements

**Inbound (user → bot):**

1. Free-text messages are forwarded to `POST /chat` with `chat_id`, and the
   agent's response is sent back to the user as-is — no parsing, no
   reformatting.
2. Button presses are routed to the correct backend endpoint based on
   `callback_data` — no other logic. The payload carries `job_id`, so button
   actions are always unambiguous regardless of conversation state.
3. `/start` forwards to `POST /session/start`. Sends a static greeting
   (`"What can I do for you today?"`) immediately, with zero backend or
   agent involvement. Auto-open is the actual default — a message with no
   open session implicitly starts one — so `/start` is for a user who wants
   to explicitly begin fresh, not a precondition for chatting.
4. `/end` forwards to `POST /session/end`. If the response indicates queued
   pushes, deliver them via the outbound interface and clear the tracked
   nudge `message_id`.
5. Idle-timeout session close is detected by the scheduler, not the bot —
   the scheduler calls the bot's flush path the same way `/end` does.
6. Only one authorised user. Any message, command, or callback from an
   unrecognised `chat_id` is logged as a warning and ignored — no reply
   sent.

**Outbound (scheduler/backend → bot):**

7. `send_message(text)` — plain text. Used by digest, ghost notice, and any
   simple notification.
8. `send_document(text, pdf_bytes)` — text + PDF attachment. Used by the
   tailor job for `PENDING_APPROVAL` pushes.
9. `send_message_with_keyboard(text, keyboard)` — text + inline keyboard.
   Used by the tailor job (`[Mark Applied] [Skip]`) and follow-up job
   (`[Sent it] [Skip]`).
10. `send_or_update_nudge(count) -> message_id` — sends a new coalesced
    nudge message ("📥 1 item waiting") if none is tracked for this chat;
    otherwise edits the existing message in place ("📥 2 items waiting").
    Used when a push arrives during an active conversation (architecture
    § 8) instead of injecting the push directly.

**Button callbacks:**

11. `[Mark Applied]` → `PATCH /jobs/{job_id}/status` body `{"status": "APPLIED"}`
12. `[Skip]` (approval) → `PATCH /jobs/{job_id}/status` body `{"status": "USER_SKIPPED"}`
13. `[Sent it]` → `POST /jobs/{job_id}/follow-up`
14. `[Skip]` (follow-up) → acknowledge the callback only, no backend call

**Error handling:**

15. A failed backend call on any inbound path (chat, button, command) logs
    the error via loguru and sends the user a plain text error message —
    never a silent failure.

---

## Step 3 — Data Model & API Contract

The bot has no database of its own. Its only in-memory state is the current
nudge `message_id` per chat, used solely to decide send-vs-edit; durability
of the underlying pending-push queue lives in the repository, not here.

**`callback_data` schema** — every inline keyboard button encodes a JSON
string (well under Telegram's 64-byte `callback_data` limit):

```json
{"action": "mark_applied", "job_id": 42}
{"action": "user_skipped", "job_id": 42}
{"action": "followup_sent", "job_id": 42}
{"action": "followup_skip", "job_id": 42}
```

**Outbound interface (what other layers call):**

```python
class TelegramClient:
    async def send_message(self, text: str) -> None: ...
    async def send_document(self, text: str, pdf_bytes: bytes) -> None: ...
    async def send_message_with_keyboard(
        self, text: str, keyboard: list[list[InlineKeyboardButton]]
    ) -> None: ...
    async def send_or_update_nudge(self, count: int) -> int:
        """Sends a new nudge or edits the existing one. Returns message_id."""
        ...
```

`chat_id` is never a parameter on these methods — it is a config value
(`TELEGRAM_CHAT_ID`) baked into the client at construction. The bot only
ever talks to one user.

**Backend calls the bot makes:**

| Trigger | Method | Endpoint |
|---|---|---|
| Free-text message | POST | `/chat` body `{"message": text, "chat_id": ...}` |
| `/start` | POST | `/session/start` |
| `/end` | POST | `/session/end` |
| `[Mark Applied]` | PATCH | `/jobs/{job_id}/status` body `{"status": "APPLIED"}` |
| `[Skip]` (approval) | PATCH | `/jobs/{job_id}/status` body `{"status": "USER_SKIPPED"}` |
| `[Sent it]` | POST | `/jobs/{job_id}/follow-up` |
| `[Skip]` (follow-up) | — | no call |

**Conversation history ownership:** The bot holds none. `POST /chat` is
stateless from the bot's perspective — it just forwards text and `chat_id`;
the agent layer's `ConversationContext` rehydrates session state from the
DB on every call (architecture § 7).

---

## Step 4 — Work Packages

**WP-T1: Client bootstrap**
Set up `python-telegram-bot`, read `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` from settings, register in the FastAPI lifespan hook
alongside APScheduler. Verify connectivity on startup with `get_me()`.

**WP-T2: Auth guard**
Middleware check on every inbound update (message, callback, command) — if
`chat_id` does not match `TELEGRAM_CHAT_ID`, log a warning and ignore. No
reply sent. Applied before any handler.

**WP-T3: Outbound interface**
Implement `TelegramClient` with four send primitives: `send_message`,
`send_document`, `send_message_with_keyboard`, and `send_or_update_nudge`.
The fourth holds exactly one piece of in-memory state per chat (the current
nudge's `message_id`), cleared on flush. Unit-testable by mocking the
`python-telegram-bot` `Bot` object's `send_message` and
`edit_message_text` calls.

**WP-T4: Inbound chat handler**
Handle free-text messages — forward as-is to `POST /chat` with `chat_id`,
send the agent's response back via `send_message`. No history list, no
state held — pure routing.

**WP-T5: Button callback handler**
Handle `callback_query` updates — parse `callback_data` JSON, route to the
correct backend endpoint via httpx, acknowledge with
`answer_callback_query`, send confirmation or error to the user.

**WP-T6: Error handler**
Centralised error handling for failed backend calls — log via loguru, send
a plain user-facing error message. Covers the chat handler, callback
handler, and command handler.

**WP-T7: Command handler**
Handle `/start` and `/end`.
- `/start` → `POST /session/start`, then send the static greeting
  (`"What can I do for you today?"`) — a constant string in this layer,
  zero backend or agent involvement for the greeting text itself.
- `/end` → `POST /session/end`; if the response indicates queued pushes,
  deliver them via WP-T3's send primitives and clear the tracked nudge
  `message_id`. Telegram does not decide whether to flush — it executes
  what the backend response indicates.

---

## Step 5 — Build & Test Order

1. **WP-T1** — first; nothing else exists without it. Test: mock
   `Bot.get_me()`, assert the lifespan hook registers correctly.

2. **WP-T2** — built immediately after, since every other handler depends
   on it running first. Test: mismatched `chat_id` → short-circuit, logged
   warning, no reply; matching `chat_id` → passes through.

3. **WP-T3** — before any inbound handler, since WP-T5 and WP-T7 both call
   into it. Test: mock `send_message`, `send_document`,
   `edit_message_text`; for `send_or_update_nudge`, test both branches (no
   tracked `message_id` → send; tracked → edit) and the clear-on-flush
   behaviour.

4. **WP-T6** — before T4/T5/T7 so they can use it rather than retrofit it.
   Test: force a backend call to raise, assert it is logged and a plain
   error message is sent.

5. **WP-T4** — straightforward once T2, T3, T6 exist. Test: mock
   `POST /chat`, assert raw text is forwarded with `chat_id`, response sent
   via `send_message`, failure path routes through T6.

6. **WP-T5** — same dependencies as T4. Test: mock each of the four
   `callback_data` actions, assert correct backend endpoint called,
   `answer_callback_query` fires, error path routes through T6.

7. **WP-T7** — last; composes T3 (nudge flush delivery) and T6 (error
   handling), and depends on `POST /session/start` / `POST /session/end`
   existing on the backend (owned by the agent/session layer). Test:
   `/start` sends the static greeting with no backend call for the
   greeting text; `/end` calls the session-close endpoint, and if the
   response indicates queued pushes, asserts delivery via T3 and that the
   nudge `message_id` is cleared.

**Known external dependency:** WP-T7's `/end` flow requires
`POST /session/end` to exist on the backend. Until then, T7 is tested
against a mocked response shape only.

---

## Locked Decisions Log

- `job_id` (not `record_id` or similar) is the field name in `callback_data`
  — refers to the backend's internal primary key, never a portal-native
  job identifier (MCF UUID, Careers@Gov `objectID`, etc.), which live in
  `metadata` on the record instead.
- The bot holds zero conversation history. This is not a simplification of
  convenience — it is architecture invariant 8/9: Telegram is transport
  only, and the DB (not the transcript) is the source of truth.
- `/start` is not required to begin chatting — auto-open session creation
  is the default. `/start` is for explicitly resetting.
- `/end` is the express lane for closing a session; idle-timeout
  (`session_idle_minutes`) is the safety-net fallback, detected and
  triggered by the scheduler, not the bot.
- The greeting string on `/start` is a hardcoded constant in the Telegram
  layer — not agent-generated, not personalised, sent with zero round-trip
  to the backend.
- Push delivery timing (immediate vs. coalesced nudge vs. held) is decided
  above this layer. The bot only ever executes a send or an edit; it never
  evaluates conversation state to make that decision itself.
