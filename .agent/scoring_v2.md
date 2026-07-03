# Scoring Layer — Planning Document (v2)

Status: planning complete, pre-implementation.
Scope: the scoring layer only. Everything about *calling* the scorer belongs to
the scrape-and-ingest routine and is out of scope here (stated explicitly below).

This document is the source of truth for the scoring layer. It follows the same
five-step planning shape as the other layer specs: scope boundary, requirements,
input/output contract, work packages, build order.

---

## Purpose

The scoring layer answers one question: given a job description and the candidate
profile, how well do they match? It returns that answer as a single whole number
between 0 and 10000, where a higher number means a better match. That number is
what the pipeline uses to decide whether a freshly discovered job is worth
keeping (`DISCOVERED → SCORED`) or dropping (`DISCOVERED → REJECTED`).

The layer is deliberately small. It takes two inputs, returns one number, and
knows nothing about the world around it.

---

## Step 1 — Scope Boundary

### What the layer is

The layer is one component that takes a job description and the candidate profile
and returns a single whole number between 0 and 10000.

Its only public surface — the one thing the rest of the system is allowed to
touch — is the `Scorer` interface:

```python
class Scorer(Protocol):
    async def score(self, jd_text: str, candidate: Profile) -> int: ...
```

"Interface" here means a contract, not a working implementation. It states that
anything calling itself a `Scorer` must offer a `score` method that takes a job
description and a profile and returns an integer. It says nothing about *how* the
score is computed. That is deliberate: it lets a different scoring method be
swapped in later without anything else in the system noticing.

We build exactly one implementation of this contract now: an `EmbeddingScorer`.
That is the version that uses a remote embedding model — it sends text to an
outside service (OpenAI) and gets back a vector (a list of numbers that
represents the meaning of the text), then measures how close the job's vector is
to the profile's vector.

### What the layer is NOT (out of scope)

Each of the following lives in the scrape-and-ingest routine that *uses* the
scorer. None of them belong to this layer.

- The layer does not decide **when** it is called. Whether the caller scores each
  job as it is ingested or in a separate pass afterward is the caller's decision.
- The layer does not decide **what happens to the number** after it returns it.
  Saving the score and moving the job from `DISCOVERED` to `SCORED` or `REJECTED`
  is the caller's job, done through the backend API (invariant 1: all writes go
  through the backend).
- The layer does not own the **threshold**. The cutoff that separates "keep" from
  "reject" — the gate — is applied by the caller, outside the scorer. The scorer
  never says yes or no; it only says how well two things matched.

### Deliberately excluded from this layer's design

- **No `name` / scorer-identity field.** This app has a single user who will not
  run two scorers side by side and compare their outputs, so there is no reason
  to stamp each score with which scorer produced it. The field would be dead
  weight and is not included.
- **No composite-pipeline design.** The interface happens to allow a future
  scorer that blends several methods, because any such scorer only has to satisfy
  the same contract, but this document spends no design effort on it.

---

## Step 2 — Requirements

These are the rules the layer must always obey — the promises it makes about its
behaviour, independent of how it is built inside. They are the checklist the test
suite proves.

1. **The output is always a whole number between 0 and 10000.** Never a decimal,
   never negative, never above 10000. The unit is "basis points": a fraction
   between 0 and 1 multiplied by 10000 so it can be stored and compared as a
   plain integer (0.73 becomes 7300). Whole numbers compare cleanly and avoid
   rounding-difference bugs downstream.

2. **The conversion from raw match value to whole number happens in exactly one
   place.** Inside the scorer the calculation produces a raw similarity value
   between 0 and 1 (a decimal). One line, `round(similarity * 10000)`, turns that
   into the final integer, and that line exists in one spot only. After it, the
   decimal is gone. One conversion point means one source of truth and no risk of
   two scales floating around.

3. **Bad or empty input returns 0, and never crashes.** If the scorer is handed
   something it cannot meaningfully score — an empty job description, or a case
   where the maths would produce "NaN" (short for "not a number", the error value
   from operations like 0 ÷ 0) — it returns 0. Zero is the honest answer for
   input that carries no signal.

4. **The `score` method is asynchronous.** The embedding version sends text over
   the network and waits for a reply. `async` means that while it waits, the rest
   of the program is free to do other work instead of freezing. A scorer that
   does no network work can still be written this way at no cost, so this rule is
   safe for the whole layer and keeps it consistent with the project's async
   stack (`aiosqlite`, `httpx`, `pytest-asyncio`).

5. **The scorer is stateless and deterministic.** It keeps no memory between
   calls and does not change behaviour over time. The same job description and
   the same profile always produce the same number. This is what makes the layer
   testable and predictable.

6. **On embedding-service failure, the scorer throws — it never fakes a score.**
   A network call can fail (network drop, service down, rate limit). The scorer
   does not catch that failure and does not return 0. It lets the error travel up
   to the caller. This matters because the design scores each job once and never
   re-scores it: a job silently turned into a 0 by a one-second network glitch
   would be permanently and wrongly rejected. By throwing, the scorer forces the
   caller to notice there was no score, so the job's status is left unchanged and
   the job is scored again on the next run. A returned number always means a real
   score; the absence of a number is loud, not silent.

7. **Accepted limitation: single-vector dilution.** The embedding scorer turns
   the entire profile into one vector. The profile is a deliberate superset
   covering several directions (robotics, backend, data engineering, and so on).
   Averaging all of that into one vector blurs the distinct strengths together,
   so a job that is a strong match for one specific area can score lower than it
   deserves. This is a known weakness of the single-vector approach. The fix —
   splitting the profile into chunks and matching against the best-fitting chunk
   — is already deferred to 0.7.x in `architecture_v2.md`. We accept this
   limitation on purpose for now and record it so it is a conscious choice.

---

## Step 3 — Input / Output Contract

### Signature

```python
async def score(self, jd_text: str, candidate: Profile) -> int
```

The `score` method receives the text of one job description and the candidate
profile in its normal structured form (the `Profile` object). It returns one
whole number between 0 and 10000.

### The profile is embedded inside the scorer, on every call (accepted trade)

The scorer turns the profile into a vector itself, inside `score`, each time it
is called. There is no prepared vector passed in, no cached vector, and no
change-detection.

This is a deliberate simplification. An earlier design cached the profile vector
and rebuilt it only when the profile changed, detected by hashing the profile
text into a short "fingerprint" and comparing it between runs. That approach kept
tying the input contract in knots — the scorer would receive a vector, yet
detecting a change needs the profile *text*, and reconciling the two pulled
caching state up into the caller. We removed the cache entirely to dissolve that
tangle.

**The cost, stated honestly:** the profile is the same for every job in a run,
so re-embedding it once per job pays for the same answer many times. At a
daily-batch scale (roughly hundreds of jobs per run, once per day) each embedding
call costs a fraction of a cent, so the waste is real but negligible in absolute
terms. It stops being negligible only if job counts climb sharply or runs become
much more frequent.

**Why it is safe:** the outward contract is still "job description and profile in,
number out", so *how* the scorer handles the profile internally can change later —
caching, or a change-notification from the profile-editing tool — behind the
exact same interface, without disturbing any caller. Re-embedding now defers the
optimisation; it does not lock anything out. See § Deferred seams.

### Cost model (informational)

Per `score` call the scorer embeds two pieces of text: the job description and
the profile. Both are sent to the embedding service; batching both into a single
request is one network round trip rather than two (see Work Package 2). Job
descriptions are all different and each job is scored once, so their embeddings
cannot be reused. The profile embedding is the redundant one described above.

---

## Step 4 — Work Packages

A work package is one buildable unit with a clear job that can be written and
tested on its own. The layer breaks into six.

### WP1 — The `Scorer` contract

The one-line Protocol from Step 1. It has no behaviour of its own; it exists so
that the rest of the system depends on this shape rather than on the specific
embedding scorer. Building it is writing the Protocol down. Nothing to test
directly.

Proposed location: `services/scoring/protocol.py`.

### WP2 — The `Embedder` seam

The piece that talks to the cloud embedding service. Its one job is to take text
and return that text's vector by calling OpenAI. It is a separate injected piece
so tests never hit the real network: a test hands the scorer a fake embedder that
returns canned vectors, and the scorer cannot tell the difference (the same
injection-and-mock pattern the project uses for the language model).

Two parts: the interface, and one real OpenAI-backed implementation. The
interface takes several texts at once and returns one vector per text, so the two
texts in a `score` call (job description and profile) go out in a single round
trip.

```python
class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

Testing: mock the HTTP call; assert the OpenAI implementation sends the right
request and reads the vectors out of the response correctly.

Proposed location: `services/scoring/embedder.py`.

### WP3 — Profile-to-text

A pure function (no network, no database — just a data transformation) that takes
the `Profile` and produces one text string for embedding, because the embedding
service takes text and the profile is a structured object.

**Decision: pour everything in.** Every meaningful field goes into the string —
all experiences, all projects, every skill, each item's `demonstrated_skills`,
and `target_tracks` — without curating or dropping anything for being off-topic.
This matches the project's "deliberate superset" philosophy: the scorer sees the
complete picture, and selection happens downstream, not by hiding parts of the
profile. Fields that carry no matching signal (contact details, dates) are left
out because they would only add noise. The exact field order and separators are a
small implementation detail settled when the function is written.

The dilution cost of pouring everything in is Requirement 7's accepted
limitation.

Testing: give it a known profile, assert the expected string.

Proposed location: `services/scoring/profile_text.py`.

### WP4 — Similarity maths

A pure function that turns two vectors into the final score. Three steps in order:

1. Compute the **cosine similarity** between the job vector and the profile
   vector. Cosine similarity is a standard measure of how closely two vectors
   point in the same direction; it lands between roughly 0 and 1, higher meaning
   more alike.
2. Convert that decimal to basis points with `round(similarity * 10000)`. This is
   Requirement 2's single conversion point, and it lives here and nowhere else.
3. Apply Requirement 3's guard: if either vector is all zeros (the degenerate
   case where cosine similarity would produce NaN), return 0 instead.

Keeping this isolated is deliberate: the guard and the conversion are exactly the
things most worth testing without any network in the way.

Proposed location: `services/scoring/similarity.py`.

### WP5 — The `EmbeddingScorer`

The piece that fulfils WP1's contract by wiring the others together. Its `score`
method, in order: take the job description and the profile, turn the profile into
text with WP3, hand both texts to the embedder (WP2) to get their two vectors,
pass those vectors to the maths (WP4), and return the integer.

It also owns the two behaviours that touch the outside world:

- Empty job description → return 0 without calling the embedding service
  (Requirement 3).
- Embedder call fails → do not catch it; let the error surface to the caller
  (Requirement 6).

Testing: give it a fake embedder; assert the chain produces the right number,
that empty input returns 0, and that an embedder failure raises rather than
returning a number.

Proposed location: `services/scoring/embedding_scorer.py`.

### WP6 — Test suite

Not new behaviour; the collection of tests that prove Step 2's requirements. In
the project's test-driven approach these are written first, per piece, as the
specification. The suite uses a fake embedder throughout, so it runs with no
network and is fast and repeatable.

Test case catalog:

| TC ID | Proves | Requirement |
|---|---|---|
| TC-SCORE-01 | A normal job and profile produce a whole number within 0–10000 | 1 |
| TC-SCORE-02 | The same inputs produce the same output (statelessness) | 5 |
| TC-SCORE-03 | Empty job description returns 0 without calling the embedder | 3 |
| TC-SCORE-04 | An all-zero vector returns 0 rather than NaN or an error | 3 |
| TC-SCORE-05 | Conversion is `round(similarity * 10000)`, verified at boundaries | 1, 2 |
| TC-SCORE-06 | An embedder failure raises from `score` rather than returning a number | 6 |
| TC-SCORE-07 | The OpenAI embedder sends the correct request and parses vectors (HTTP mocked) | — |
| TC-SCORE-08 | Profile-to-text includes the intended fields and excludes contact/dates | — |

---

## Step 5 — Build Order

The pieces are built in dependency order, so at every stage the work rests on
something already built and already tested. Dependencies: the similarity maths
and the profile-to-text function depend on nothing; the embedder's real
implementation depends only on its interface; the `EmbeddingScorer` depends on
all three; the contract is the shape the scorer ends up matching.

1. **Similarity maths (WP4).** Depends on nothing — hand it two made-up vectors
   and check the number out. First, so the trickiest correctness details (the
   rounding and the zero-vector guard) are proven before anything builds on them.
2. **Profile-to-text (WP3).** The other standalone pure piece; both pure pieces
   are done before anything touches the network.
3. **Embedder (WP2).** The first piece that involves the network, built on its
   own so network concerns are introduced in one focused package, tested with the
   HTTP call mocked.
4. **`Scorer` contract (WP1).** The tiny interface, written right before the
   scorer that keeps it, so the scorer is built to match an existing contract.
5. **`EmbeddingScorer` (WP5).** Last of the building pieces because it wires all
   the others together; by now every part it reaches for exists and is proven, so
   the only new things tested are the wiring and the two edge behaviours.

Tests are written first for each piece (WP6 runs throughout, not at the end).

---

## Deferred seams (named, not built)

These are recorded so future changes are known trades, not surprises. Each fits
behind the existing `Scorer` contract and requires no change to any caller.

- **Profile-vector caching with fingerprint change-detection.** Cache the profile
  vector and rebuild it only when the profile text changes, detected by hashing
  the text into a fingerprint and comparing between runs. Removes the redundant
  per-job profile embedding. Deferred because the saving (one embedding call on
  days the profile changed) is negligible at current scale and not worth the
  caching state to maintain. Earns its keep only if runs become much more
  frequent.
- **Chunked / max-pool ranking.** Split the profile into chunks and match a job
  against the best-fitting chunk instead of one averaged vector, fixing the
  single-vector dilution of Requirement 7. Already scheduled for 0.7.x in
  `architecture_v2.md`.
- **Composite-pipeline scorer.** A future scorer blending several methods
  (keyword, TF-IDF, formula, embedding, LLM inference) behind the same contract.
  The interface admits it; no design effort spent here.

---

## Commit

Documentation-only change, per the project convention:

```
docs(scoring): add scoring layer planning document (v2)
```
