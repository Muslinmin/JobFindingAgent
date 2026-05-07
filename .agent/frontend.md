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
