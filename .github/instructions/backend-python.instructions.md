---
applyTo: "**/*.py,**/pyproject.toml,**/requirements*.txt"
---

# Python Backend Review Rules

If `[light-ccr]` mode is active per `../copilot-instructions.md`, do not apply this file.

## Testability — flag in the diff

- **Hardcoded time.** `datetime.now()`, `datetime.utcnow()`, `time.time()` in business logic. Should accept a clock callable, defaulted at the edge.
- **Inline construction of external clients.** `requests.Session()`, `httpx.Client()`, Kafka clients, AWS SDK clients constructed inside business functions. Should be passed in or built in a composition root.
- **Module-level state mutated by functions** — globals updated as a side effect of normal calls.
- **Tests that patch the SUT.** `@patch('mymodule.MyClass')` where `MyClass` is the symbol under test.
- **Tests with no assertions** that don't document expected-no-exception intent.
- **HIO services: doers tested only via HTTP.** Background doers (cooperative multitasking via cues/decks) should have direct tests that inject cues without spinning up a full event loop.
- **Falcon resource tests asserting only status codes** without response-body shape assertions — serialization regressions ship.

## Outbound calls and timeouts

- New `requests`, `httpx`, `urllib`, or boto3 call without an explicit `timeout=` argument.
- Bare `except:` or `except Exception:` around outbound calls without re-raise or logging.
- `asyncio` tasks created and awaited without timeout where the call is to an external service.

## Error responses (Python specifics)

The repo-wide rules require a stable code, user-friendly message, and retryability signal. In Python:

- Falcon error handler returning `"Something went wrong"` or `str(exc)` (which may be a traceback) with no code or category.
- Bare `raise Exception(...)` from a resource without a handler converting it to the structured error shape.
- Returning HTTP 200 with `{"error": "..."}` in the body — use the proper status code so the retryability signal is correct.

## Logging hygiene

- ERROR level used for normal client mistakes (4xx). Trains operators to ignore ERROR.
- F-strings in log calls: `logger.info(f"user {x}")`. Bypasses level optimization and structured-log fields. Use `logger.info("user %s", x)` or structured logging.
- Caught exception logged at DEBUG or swallowed silently.
- New code path with no correlation ID when surrounding code threads one.
- `print()` in non-CLI code.

## Code health (Python)

- Mutable default arguments: `def f(x=[])`.
- Bare `except:` or `except Exception:` without re-raise or logging.
- New utility duplicating stdlib (`itertools`, `functools`, `pathlib`).
- Functions with >5 parameters that aren't a clear data structure — consider a dataclass or `TypedDict`.
- Type hints absent on new public functions in a codebase that uses them elsewhere.

## Build and supply chain

- `requirements.txt` without pinned transitive dependencies is not a lockfile. Flag if the diff adds dependencies without a corresponding `poetry.lock`, `uv.lock`, or `pip-compile` output update.
- Internal-namespace package added that could be typosquatted from a public registry — flag for review of the registry source.

## CI workflow changes

- Pipelines that install but don't run tests.
- `pytest --collect-only` or other "fake" test runs that report success without executing.
- `pip install` instead of `pip install --require-hashes` (or equivalent lockfile-respecting install) when a lockfile exists.
