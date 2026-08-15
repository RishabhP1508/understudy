# 0003. Default to gemini-3.1-flash-lite, and treat the free tier as a per-day budget

Status: accepted (Phase 2)

## Context

Discovery needs a model with reliable function calling. The submission has to be reproducible by a
reviewer using a free API key, and a discovery run costs 8 or 9 model requests.

Three things were measured live rather than assumed:

- `gemini-2.5-flash`, the obvious default, returns `404 NOT_FOUND: This model is no longer available
  to new users`. A pinned model version can be withdrawn underneath a submission.
- `gemini-flash-latest` works, and resolves to `gemini-3.7-flash`. Its free-tier quota came back as
  `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`. That is twenty requests per
  DAY, per model, not the 15 per minute assumed while writing the client. Two discovery runs exhaust
  it, after which every request 429s and no backoff can clear it, because the window is a day.
- Four models were given the same task and all four produced a correct tool call, but the checkpoint
  `kind` came back as `answer`, `text_match`, `text`, and `TEXT` respectively. Free-form model
  vocabulary cannot be dispatched on.

## Decision

Default to `gemini-3.1-flash-lite`, overridable with `GEMINI_MODEL`. Constrain `checkpoint.kind`
with a JSON-schema enum, and force a tool call with `tool_config` mode `ANY`. Treat quota exhaustion
as a non-retryable condition that surfaces as a clean one-line CLI error naming the status and the
model, not as a traceback.

The quota is per model, so switching models via `GEMINI_MODEL` also switches to a fresh budget. That
is the documented recovery when a reviewer runs out.

## Tradeoff

A lite model is weaker at reasoning than full flash, and on a harder goal than this one it may need
more steps or fail where flash would succeed. Measured on this task it did not: `gemini-flash-latest`
and `gemini-3.1-flash-lite` both reached `goal_verified` in 8 rounds with 0 rejected turns and
extracted the same `$1,204.55`. If a later phase's goal proves too hard for lite, the fix is one
environment variable, and the run record always states which model was used.

Pinning a name rather than tracking `-latest` means the default can age out the way `2.5-flash` did.
That is accepted deliberately: a moving alias makes a recorded run irreproducible, and provenance
that says `gemini-flash-latest` does not tell a reader which model actually ran.

## Alternatives considered

- **`gemini-flash-latest` as the default.** Rejected on quota (20/day) and on provenance: an alias
  records nothing useful about what actually ran.
- **A paid key.** Out of scope. The submission has to work for a reviewer with a free key.
- **Retrying harder on 429.** Rejected. The exponential backoff already in the client is right for a
  per-minute limit, and useless against a per-day one. Retrying a daily quota just delays the same
  failure, so the client raises and the CLI explains.
