# JobFindingAgent

An agentic job application tracker for one person. You talk to it in plain
language over Telegram; it discovers roles, scores them against your
profile, tailors a LaTeX CV, drafts cover letters and follow-ups, and keeps
the status of every application straight.

Two things run side by side in one process:

- **A conversational agent** behind `POST /chat` — a ReAct loop with ten
  tools, driven by whatever you type.
- **A scheduler** running six daily/weekly jobs — scrape, score, expire
  stale records, nudge follow-ups, tailor the top matches, weekly digest.

They never call each other. The scheduler pushes to you; the agent answers
you. Both go through the same `JobService` facade, so neither can corrupt
what the other wrote.

---

## Requirements

- **Conda** (the environment is `job-finder`)
- **tectonic** — the LaTeX engine that compiles tailored CVs to PDF. It
  comes from the same conda environment, so you get it with the install
  below. Without it on `PATH`, `tailor_resume` fails with `render_failed`
  and six tests skip.
- **Two Telegram bots** and your own chat ID
- **An LLM API key** and an **embeddings API key** (they may be different
  providers)

## Setup

```bash
conda env create -f environment.yml
conda activate job-finder

cp .env.example .env
$EDITOR .env                 # fill in the keys — see below
```

Create your profile from the template. This is the source of truth for
scoring and every document the system writes:

```bash
cp profile_template.json profile.json
$EDITOR profile.json
```

### Filling in `.env`

| Variable | What it is |
|---|---|
| `MODEL` / `MODEL_API_KEY` | Chat model, routed by LiteLLM — the prefix picks the provider. |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | Scoring embeddings. Separate key because the provider may differ. |
| `TELEGRAM_CHAT_BOT_TOKEN` | The bot you talk to. From [@BotFather](https://t.me/BotFather). |
| `TELEGRAM_NOTIFICATIONS_BOT_TOKEN` | The bot that pushes scheduled results. A *second* bot. |
| `TELEGRAM_CHAT_ID` | Your own chat ID. Both bots ignore every other sender. |
| `API_BASE_URL` | Where the bots reach the API. `http://localhost:8000` locally. |

Two bots rather than one so a scheduled push at 5am can never land in the
middle of a conversation you are having.

> **`LLM_REASONING_EFFORT` defaults to `"none"`, and that matters.**
> Reasoning models reject function tools on `/v1/chat/completions`, so the
> agent sends this with every tool-calling request. It is a setting, not a
> constant, because a model whose enum starts at `"minimal"` will reject
> `"none"` — set it to `""` to omit the parameter entirely.

## Running it

From the repo root, so the relative paths in `.env` resolve:

```bash
conda activate job-finder
PYTHONPATH=src uvicorn app.main:app --host 0.0.0.0 --port 8000
```

You should see both bots verified, the scheduler register six jobs, and the
agent constructed:

```
Telegram bot verified: @your_chat_bot
Telegram bot verified: @your_notifications_bot
Scheduler started — all six jobs registered
Agent constructed, conversation store wired
Application startup complete.
```

Now message your chat bot. Nothing else needs starting — the bots poll
Telegram from inside this process.

To check the agent without Telegram:

```bash
curl -s -X POST localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"what am I working on right now?"}'
```

### Docker

```bash
docker build -t jobfinder .
docker run --env-file .env -p 8000:8000 jobfinder
```

Note the image does **not** install tectonic, so CV tailoring will fail in
the container until it is added.

## Using it

Talk normally. The agent resolves what you mean to a specific record before
it changes anything — so if two jobs match, it asks rather than guesses.

```
"what am I working on?"                  → lists the active pipeline
"find me robotics jobs"                  → searches, ingests, scores
"tailor my resume for the HTX role"      → generates and sends a PDF
"nice, I'm applying"                     → moves it to applied
"draft a follow-up for the PUB one"      → gives you copy-paste email text
"write a cover letter, less formal"      → drafts, saves, re-drafts on request
"add ROS 2 to my skills"                 → shows a diff, waits for your yes
```

Two things it deliberately will not do: invent a job it has not looked up,
or claim something about you that your profile does not support. The CV and
cover-letter paths both run truthfulness guards, and a refused draft comes
back as a refusal rather than a plausible fabrication.

### Search queries are short

Job sources match keywords literally and require **every** one to appear,
so `robotics` finds far more than `robotics mechatronics embedded systems
Singapore`, which finds nothing. Leave the location out too — the
configured sources are already region-specific.

## The scheduled jobs

Six, in this order so each sees the last one's output. Hours are `.env`
settings; these are the defaults.

| Job | When | LLM? | What it does |
|---|---|---|---|
| `query_regen` | Mon 01:00 | yes | profile → `search_queries.json` |
| `scrape` | 02:00 | no | queries → adapters → ingest → score → SCORED/REJECTED |
| `lifecycle` | 03:00 | no | three time rules: expire, stale, ghost |
| `follow_up` | 04:00 | yes | nudges jobs applied to but silent |
| `tailor` | 05:00 | yes | top-N SCORED → PDF → PENDING_APPROVAL → push |
| `digest` | Mon 06:00 | optional | weekly summary |

## Tests

```bash
conda activate job-finder      # tectonic must be on PATH or 6 tests skip
pytest -q
pytest -q -m "not live"        # skip tests that call a real API
```

`pytest.ini` sets `pythonpath = src`, so no `PYTHONPATH` is needed here.
Tests marked `live` make real API calls and cost money.

## Layout

```
src/
  app/          FastAPI app, routes, config, SQLite repository + JobService
    services/   discovery (scrape+ingest), artifacts (store+backup+register)
  agent/        ReAct loop, tool schemas, handlers, conversation context
  drafting/     cover letters + follow-up emails (LLM + guards, no DB)
  tailoring/    JD + profile → validated selection → LaTeX → PDF
  scoring/      embedding similarity against the profile
  scraper/      job source adapters (Careers@Gov)
  profile/      profile schema, loader, the sole mutator
  scheduler/    the six jobs
  telegram_bot/ both bots
  test/
.agent/         design documents — read these before changing a layer
```

Data written at runtime, all gitignored: `jobs.db`, `conversations.db`,
`transcripts/`, `artifacts/`, `logs/`.

## Design docs

`.agent/` holds the reasoning behind each layer, and is worth reading
before changing one — most of the non-obvious code is non-obvious on
purpose, and the rationale lives there rather than in comments.

`architecture_v2.md` is the system overview. `agent_v2.md`, `backend_v2.md`,
`scoring_v2.md`, `tailoring.md`, `scheduling_v2.md`, `scraper_layer.md`,
`profile.md`, and `telegram_v2.md` cover their layers;
`concurrencyFor_agentV2.md` covers the timeout ladder and locking.

## Known rough edges

- **Scoring is crude.** Cosine similarity × 10000; scores cluster in a
  narrow band, so `SCORE_THRESHOLD` (default 5000) sits close to the median
  and small differences flip the outcome. REJECTED is terminal and scores
  are never recomputed, so the threshold is worth tuning to your own data.
  A proper revamp is planned separately.
- **One source.** Only Careers@Gov is implemented; it has no relevance
  ranking, so a query either matches or returns nothing.
- **No conversation boundary but time.** A session continues until 30 idle
  minutes pass (`SESSION_IDLE_MINUTES`). There is no `/new`.
- **Context is not compacted.** The whole current session is re-sent every
  turn; the seam exists but is the identity function.
