# Week 5 — Telegram Interface & Scheduling: TDD Plan

---

## Overview

The frontend layer has two components, each tested in isolation before being wired together:

1. **Telegram Bot** — transport layer between the user's phone and `POST /chat`
2. **APScheduler Jobs** — scrape job (silent ingestion) and digest job (Monday Telegram notification)

The testing philosophy is identical to previous weeks — units first, integration only after units are green. Scheduled jobs are extracted as plain functions and tested by calling them directly. The scheduler wiring is only touched in the integration test.

---

## Component 1 — Telegram Bot

### Unit Tests

**1. Access control — unknown chat ID is ignored**
- Send a message from an unknown chat ID
- Assert the message is never forwarded to `POST /chat`
- Assert no Telegram reply is sent

**2. Access control — configured chat ID is forwarded**
- Send a message from the configured chat ID
- Assert `POST /chat` is called once with the correct payload

**3. Message forwarding — correct payload shape**
- Send a known message text
- Assert `POST /chat` receives the message text and full conversation history in the expected shape

**4. Response delivery — agent reply sent back to correct chat ID**
- Mock `POST /chat` to return a known response string
- Assert the bot sends that string back to the correct chat ID

**5. Empty or malformed response from `POST /chat`**
- Mock `POST /chat` to return an empty body or malformed JSON
- Assert the bot sends a fallback message rather than crashing

**6. `POST /chat` unavailable**
- Mock `POST /chat` to raise a connection error
- Assert the bot catches the exception, notifies the user with a fallback message, and does not crash

---

### Integration Tests

**3a. Mocked round-trip**
- `POST /chat` is mocked, Telegram send is mocked
- Send a message from the configured chat ID
- Assert it flows through the bot, reaches `POST /chat`, and the response is delivered back
- Fast and deterministic — runs in CI on every push

**3b. Live LLM round-trip**
- Real `POST /chat` call with the actual LLM behind it
- Telegram send remains mocked
- Send a known prompt (e.g. "how many jobs am I tracking?")
- Assert the response is a non-empty, coherent string — no assertion on exact wording
- Gated behind `RUN_LIVE_TESTS=true` environment flag — runs manually or in a separate CI stage
- Purpose: confirm the full pipeline holds together end to end with a real model

---

## Component 2 — APScheduler Jobs

> Jobs are tested as plain Python functions called directly. No scheduler is started in unit tests.

---

### Scrape Job Unit Tests

**1. Calls Tavily with the configured search terms**
- Mock Tavily to return an empty list, mock `POST /jobs`
- Call `scrape_job()` directly
- Assert Tavily was called once with `settings.search_terms`

**2. Parsed results are forwarded to `POST /jobs`**
- Mock Tavily to return two fake raw results
- Call `scrape_job()` directly
- Assert `POST /jobs` was called twice with correctly structured `JobCreate`-shaped payloads

**3. Empty Tavily results — no `POST /jobs` calls**
- Mock Tavily to return an empty list
- Call `scrape_job()` directly
- Assert `POST /jobs` is never called

**4. Tavily unavailable — job exits cleanly**
- Mock Tavily to raise an exception
- Call `scrape_job()` directly
- Assert the function returns normally without raising
- Assert `POST /jobs` is never called
- Assert the failure is logged

> Note: An unhandled exception in a scheduled job can cause APScheduler to stop firing that job permanently. Silent graceful exit is required.

---

### Digest Job Unit Tests

**1. Correctly identifies stale applications**
- Mock `GET /jobs` to return three jobs: one updated 20 days ago, one 5 days ago, one exactly 14 days ago
- Call `digest_job()` directly
- Assert the Telegram message contains the 20-day and 14-day jobs
- Assert the 5-day job is absent from the message
- Pins the 14-day boundary decision explicitly

**2. Applications updated within 14 days are excluded**
- Mock `GET /jobs` to return only fresh jobs (under 14 days)
- Call `digest_job()` directly
- Assert Telegram send is never called

**3. No applications in DB — no Telegram message sent**
- Mock `GET /jobs` to return an empty list
- Call `digest_job()` directly
- Assert Telegram send is never called

**4. Telegram message contains correct application details**
- Mock `GET /jobs` to return one stale job with known company and role
- Call `digest_job()` directly
- Assert the sent message contains the company name and role
- No assertion on exact message format — keeps the test stable across wording changes

**5. Telegram send fails — job does not crash**
- Mock `GET /jobs` to return one stale job
- Mock Telegram send to raise an exception
- Call `digest_job()` directly
- Assert the function returns normally without raising
- Assert the failure is logged

---

### Scheduler Integration Tests

**1. Both jobs are registered at startup**
- Start the app via the lifespan hook
- Assert the scrape job and digest job are both registered on the scheduler

**2. Scrape job is configured with a 24-hour interval**
- Inspect the registered scrape job
- Assert its trigger interval is 24 hours

**3. Digest job is configured for Monday mornings**
- Inspect the registered digest job
- Assert its cron trigger is set to Monday

**4. Lifespan teardown shuts the scheduler down cleanly**
- Start the app via the lifespan hook, then trigger shutdown
- Assert the scheduler stops with no hanging threads or errors

**5. Bot polling starts on lifespan startup**
- Mock aiosqlite, create_tables, and the Telegram Application builder
- Enter the lifespan context
- Assert `updater.start_polling()` was called once

**6. Bot polling stops on lifespan teardown**
- Mock as above
- Enter and exit the lifespan context
- Assert `updater.stop()` and `application.stop()` and `application.shutdown()` were called

---

## Test Execution Order

```
1.  Bot — access control unit tests
2.  Bot — message routing unit tests
3.  Bot — error handling unit tests
4.  Bot — 3a mocked round-trip integration test
5.  Bot — 3b live LLM round-trip integration test (RUN_LIVE_TESTS=true)
6.  Scrape job — unit tests (1 through 4)
7.  Digest job — unit tests (1 through 5)
8.  Scheduler — integration tests (1 through 4)
9.  Scheduler — bot polling lifecycle tests (5 through 6)
```

---

## What Is Mocked vs What Is Tested

| Test | Mocked | Tested |
|---|---|---|
| Bot — unknown chat ID | `POST /chat`, Telegram send | Access control rejection |
| Bot — known chat ID | `POST /chat`, Telegram send | Access control pass-through |
| Bot — payload shape | `POST /chat` | Correct message + history structure |
| Bot — response delivery | `POST /chat` | Correct chat ID receives reply |
| Bot — empty response | `POST /chat` | Fallback message handling |
| Bot — endpoint down | `POST /chat` raises | Exception handling, no crash |
| Bot — 3a mocked round-trip | `POST /chat`, Telegram send | Full routing end to end |
| Bot — 3b live round-trip | Telegram send only | Full pipeline with real LLM |
| Scrape — calls Tavily | `POST /jobs` | Correct search terms passed |
| Scrape — posts results | Tavily client | Parsing + POST /jobs call count and shape |
| Scrape — empty results | Tavily client | No spurious POST /jobs calls |
| Scrape — Tavily down | Tavily client raises | Exception handling, no crash |
| Digest — identifies stale | GET /jobs, Telegram | Staleness filtering logic and boundary |
| Digest — excludes fresh | GET /jobs, Telegram | Filtering — no false positives |
| Digest — no jobs | GET /jobs, Telegram | Empty list path |
| Digest — message content | GET /jobs, Telegram | Correct fields present in message |
| Digest — Telegram down | Telegram raises | Exception handling, no crash |
| Scheduler — job registration | None | Both jobs registered at startup |
| Scheduler — intervals | None | Correct trigger configuration |
| Scheduler — teardown | None | Clean shutdown, no hanging threads |
| Scheduler — bot polling start | Telegram Application builder | Bot polling starts on lifespan startup |
| Scheduler — bot polling stop | Telegram Application builder | Bot polling stops cleanly on teardown |

---

## Implementation Checklist

Follow TDD order: write the test, watch it fail, implement the component, watch it pass.

---

### Telegram Bot ✅ DONE

#### Tests
- [x] `test_bot_unknown_chat_id_is_rejected` — unknown chat ID → no `POST /chat` call, no Telegram reply
- [x] `test_bot_known_chat_id_is_forwarded` — configured chat ID → `POST /chat` called once with correct payload
- [x] `test_bot_payload_shape` — message text + history sent in expected structure
- [x] `test_bot_response_delivery` — agent reply delivered to correct chat ID
- [x] `test_bot_empty_response_fallback` — empty/malformed `POST /chat` body → fallback message sent
- [x] `test_bot_endpoint_down_fallback` — connection error from `POST /chat` → fallback sent, no crash
- [x] `test_bot_mocked_round_trip` — full routing integration test with both `POST /chat` and Telegram send mocked
- [x] `test_bot_live_round_trip` — real `POST /chat` call, Telegram send mocked, gated behind `RUN_LIVE_TESTS=true`

#### Component
- [x] Create `bot/bot.py` — initialise `python-telegram-bot` Application with token from settings
- [x] Implement chat ID access control — reject messages from unknown chat IDs silently
- [x] Implement message handler — forward message text + conversation history to `POST /chat`
- [x] Implement response delivery — send agent reply back to the originating chat ID
- [x] Implement fallback handler — catch empty/malformed `POST /chat` responses, send a fallback message
- [x] Implement error handler — catch connection errors from `POST /chat`, notify user, do not raise

---

### Scrape Job 🔲 Tests written — component pending

#### Tests
- [x] `test_scrape_job_calls_tavily_with_search_terms` — Tavily called once with `settings.search_terms`
- [x] `test_scrape_job_posts_parsed_results` — two fake Tavily results → two correctly shaped `POST /jobs` calls
- [x] `test_scrape_job_empty_results_no_post` — empty Tavily list → `POST /jobs` never called
- [x] `test_scrape_job_tavily_down_exits_cleanly` — Tavily raises → function returns normally, `POST /jobs` never called, failure logged

#### Component
- [ ] Create `jobs/scrape_job.py` — plain function `scrape_job()` with no scheduler dependency
- [ ] Call Tavily with `settings.search_terms`
- [ ] Parse raw Tavily results into `JobCreate`-shaped payloads
- [ ] POST each parsed result to `POST /jobs`
- [ ] Handle empty Tavily result list — skip POST calls
- [ ] Handle Tavily exception — log failure, return normally without raising

---

### Digest Job 🔲 Tests written — component pending

#### Tests
- [x] `test_digest_job_identifies_stale_applications` — three jobs (20d, 5d, 14d old) → message contains 20d and 14d jobs, excludes 5d
- [x] `test_digest_job_excludes_fresh_applications` — all jobs under 14 days → Telegram send never called
- [x] `test_digest_job_no_applications_no_message` — empty job list → Telegram send never called
- [x] `test_digest_job_message_contains_correct_fields` — one stale job → message contains company name and role
- [x] `test_digest_job_telegram_down_exits_cleanly` — Telegram send raises → function returns normally, failure logged

#### Component
- [ ] Create `jobs/digest_job.py` — plain function `digest_job()` with no scheduler dependency
- [ ] Query `GET /jobs` to retrieve all applications
- [ ] Filter applications with no status change in the last 14 days (inclusive boundary)
- [ ] Build Telegram message containing company and role for each stale application
- [ ] Send message to configured Telegram chat ID
- [ ] Skip send entirely if no stale applications or no applications at all
- [ ] Handle Telegram send exception — log failure, return normally without raising

---

### Bug Fixes & Hardening ✅ DONE

Discovered and resolved during live testing of the Telegram interface.

#### Tests
- [x] `test_query_jobs_strips_description` — description not present in `_query_jobs` output
- [x] `test_query_jobs_strips_fingerprint` — fingerprint not present in `_query_jobs` output
- [x] `test_query_jobs_retains_id` — id retained for LLM to pass to `update_status`
- [x] `test_query_jobs_retains_company_and_role` — company + role retained for LLM name matching
- [x] `test_query_jobs_retains_status` — status retained
- [x] `test_query_jobs_retains_url_score_date_logged_notes` — remaining useful fields retained
- [x] `test_query_jobs_strips_description_and_fingerprint_across_all_records` — consistent across N records
- [x] `test_agent_sequences_query_then_update` — agent calls query_jobs → identifies → update_status in correct order
- [x] `test_compact_list_in_llm_history_excludes_description` — description never enters LLM context
- [x] `test_compact_list_in_llm_history_excludes_fingerprint` — fingerprint never enters LLM context
- [x] `test_compact_list_in_llm_history_retains_id` — id present in LLM context for tool call
- [x] `test_compact_list_retains_company_role_status_for_llm_matching` — LLM has enough to match by name
- [x] `test_missing_job_id_arg_returns_error_not_crash` — KeyError from missing job_id caught, returned as error JSON
- [x] `test_invalid_status_value_returns_error_not_crash` — ValueError from bad enum value caught, returned as error JSON
- [x] `test_wrong_job_id_returns_not_found_gracefully` — wrong ID → "Job not found" JSON, no crash
- [x] `test_ambiguous_company_name_both_ids_visible_to_llm` — both IDs present when company matches multiple records

#### Component
- [x] `_query_jobs` strips `description` and `fingerprint` — returns compact index only (`_QUERY_FIELDS`)
- [x] `_update_status` catches all exceptions, not just `InvalidTransitionError` — returns error JSON, never raises
- [x] `chat.py` wraps `run()` in `try/except` — returns `{"reply": "", "error": "..."}` on 500 instead of empty crash
- [x] `bot.py` logs `type(exc).__name__` alongside message — empty error strings no longer possible
- [x] `bot.py` logs HTTP status code from `/chat` and surfaces `error` field when present
- [x] `update_profile` tool description includes two examples — prevents LLM from passing fields flat instead of under `updates`

---

### Scheduler Wiring 🔲 Tests written — component partially implemented

#### Tests
- [x] `test_scheduler_both_jobs_registered` — both scrape and digest jobs present after lifespan startup
- [x] `test_scheduler_scrape_job_interval` — scrape job trigger is 24-hour interval
- [x] `test_scheduler_digest_job_cron` — digest job trigger is Monday cron
- [x] `test_scheduler_clean_teardown` — lifespan shutdown stops scheduler with no hanging threads
- [x] `test_bot_polling_starts_on_startup` — `updater.start_polling()` called during lifespan startup
- [x] `test_bot_polling_stops_on_teardown` — `updater.stop()`, `application.stop()`, `application.shutdown()` called on exit

#### Component
- [ ] Register `scrape_job` on the APScheduler with a 24-hour interval trigger
- [ ] Register `digest_job` on the APScheduler with a Monday morning cron trigger
- [ ] Wire scheduler start and stop into the FastAPI lifespan hook
- [x] Build `python-telegram-bot` `Application` in lifespan using token from settings
- [x] Register `handle_message` on the Application with a text message filter
- [x] Start bot polling before `yield` (`initialize` → `start` → `updater.start_polling`)
- [x] Stop bot polling cleanly after `yield` (`updater.stop` → `stop` → `shutdown`)
