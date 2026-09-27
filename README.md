# Vera-challenge bot — one strong, deterministic flow

## Approach

No LLM call. `compose()` is built entirely from templates that only ever
insert fields that are literally present in the pushed context. That buys:

- **Determinism** — identical inputs always produce byte-identical output.
- **Zero hallucination** — nothing is said that wasn't in category / merchant /
  trigger / customer payloads (the brief's #1 anti-pattern to avoid).
- **No external dependency / no latency risk** against the 30s per-call budget.

Two trigger kinds are fully tailored — this is the "one strong flow":

| Trigger kind | Direction | Mirrors |
|---|---|---|
| `research_digest` | merchant-facing, external | Brief Appendix A gold example |
| `recall_due` | customer-facing, internal | Brief Appendix B gold example |

Both pull real numbers (trial size, source citation, peer-relevant patient
segment / real catalog price / real open slots) straight out of the pushed
context, and switch between English and Hindi-English code-mix based on the
merchant's `identity.languages` / customer's `language_pref`.

Every other trigger kind falls back to a **safe generic composer**: it only
ever restates literal `payload` key/values, and returns nothing (rather than
inventing content) when there isn't a concrete fact to hang the message on —
restraint over spam, per the brief's guidance.

### Reply-side (`/v1/reply`) — deterministic state machine

1. **Auto-reply detection** — counts verbatim repeats of the incoming message
   in the conversation. 2nd repeat → one probe ("are you seeing this
   yourself?"). 3rd repeat → graceful `end`. (Matches Pattern B in the brief.)
2. **Explicit decline** → `end` immediately, no more nudging.
3. **Intent transition** — "yes" / "let's do it" / "haan" / "karo" etc. routes
   straight to `action` with a concrete next step, never a re-qualifying
   question (the brief's Pattern D anti-pattern).
4. **Hostility** → one polite redirect back to the merchant's account, then
   disengage if it repeats.
5. **Default** → acknowledge + leave one low-friction opening.
6. **Anti-repetition** on every branch: never resends a body verbatim within a
   conversation; falls back to `wait` instead.

## What I deliberately left out (tradeoffs)

- No LLM in the loop yet, so trigger kinds beyond the two flagship ones get a
  functional-but-plain message rather than a polished one. The composer
  functions are structured so a `compose_<kind>` per remaining trigger kind
  (perf_spike, perf_dip, milestone_reached, dormant_with_vera,
  competitor_opened, seasonal_beats, etc.) can be dropped in the same way
  without touching the endpoint/state-machine code.
- No cadence planner for "what's the optimal sequence of nudges this week" —
  out of scope for a first strong flow.
- No persistence beyond process memory (per the testing brief, in-memory is
  explicitly allowed for the test window; add Redis/SQLite before any real
  production use).

## Endpoints implemented

`POST /v1/context` (idempotent by `(scope, context_id, version)`) ·
`POST /v1/tick` · `POST /v1/reply` · `GET /v1/healthz` · `GET /v1/metadata` ·
plus optional `POST /v1/teardown` (wipes in-memory state).

## Verified locally

`integration_test.py` pushes the actual generated 255-context dataset
(`dataset/generate_dataset.py --out ./expanded`), then:

- confirms `/v1/healthz` reports 5/50/200/100 after warmup push
- confirms idempotent re-push and stale-version rejection
- runs the `research_digest` flow for Dr. Meera and diffs favorably against
  Appendix A
- runs the `recall_due` flow for Priya and diffs favorably against Appendix B
- confirms suppression-key dedup (re-ticking the same trigger sends nothing)
- confirms the generic fallback never invents facts
- drives `/v1/reply` through auto-reply (3x), intent-transition, and decline
  scenarios and asserts the expected `action`

Run it yourself:
```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080 &
python3 integration_test.py
```



