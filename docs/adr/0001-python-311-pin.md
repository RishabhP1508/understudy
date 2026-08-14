# 0001. Pin development and CI to Python 3.11

Status: accepted (Phase 0)

## Context

`pyproject.toml` declares `requires-python = ">=3.11"`. The development machine's default
interpreter is CPython 3.14.3, and a uv-managed CPython 3.11.15 is also installed. This stack pulls
in three packages with compiled or browser-bound components: pydantic-core, Playwright, and
google-genai. New CPython minor versions routinely lead wheel availability for those by months, and
a missing wheel means a local source build against a toolchain that has nothing to do with this
project.

## Decision

Develop against a `.venv` created with `uv venv --python 3.11`, and pin CI to 3.11 through
`actions/setup-python`. Keep `requires-python = ">=3.11"` rather than capping the upper bound, so
the package stays installable on newer interpreters for anyone who wants to try.

## Tradeoff

The versions actually exercised are 3.11 only, so a 3.12 or 3.13 incompatibility would not be
caught. That is acceptable for a submission that is run from documented commands rather than
distributed to many environments. It costs a contributor on 3.14 one `uv venv --python 3.11` step.

## Alternatives considered

- **Develop on 3.14, the machine default.** Rejected: the first failure would likely be a
  pydantic-core or Playwright build error, which is time spent on packaging rather than on the
  graded axes.
- **A CI matrix across 3.11, 3.12, and 3.13.** Rejected: it triples the run time to defend a
  portability claim that nothing in the brief asks for.
- **Cap with `requires-python = ">=3.11,<3.12"`.** Rejected: it turns a local convenience into a
  hard limit for anyone reading the repository.
