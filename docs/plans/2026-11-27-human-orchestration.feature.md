# Human orchestration for cloud runs

Spans two repos: **superlite-cloud** (server + worker) and **superlite** (client CLI). Session slug: `human-orchestration`.

## Goal

A superlite user who starts a cloud run can choose who orchestrates it: **AI** (today's behavior, still the default) or **Human**. A Human run behaves like a local human-orchestrated superlite session. When the agent has a question, it stops and waits for your answer. When the plan is ready, it waits for you to approve it before it builds. You get the questions in the watch pane or an attached session, and you answer them there.

For: every slcloud member who submits runs with `superlite-cli factory send` (or `/superlite:send`) or with MCP `submit_task`.

## What changes

**Commands (superlite client)**

| Command | What it does |
|---|---|
| `superlite-cli factory send ... --orchestrator human\|ai` | Picks the orchestrator for this one run. Without the flag, the saved default is used, and if nothing is saved, AI. |
| `superlite-cli factory use <name> --orchestrator human\|ai` | Saves the default orchestrator for that factory, next to the `--superlite` default. `factory list` shows it. |
| `superlite-cli cloud runs` | Shows the run's orchestrator (`[human]`), and for a waiting run it shows what it waits on: `waiting: question` or `waiting: plan`. |
| Watch pane (`cloud watch <run>`) | Shows a new `? <question text>` line. While the run waits on a question, a plain line you type is sent as **the answer** (respond), not as a nudge. `/approve` and `/cancel` are unchanged. |
| Attached session (`/superlite:attach`) | No new syntax. A plain line sent while the run is `waiting` already goes as an answer. |
| `/superlite:send` | Its argument hint and doc gain `--orchestrator`. |

**Endpoints (slcloud)**

| Route | Change |
|---|---|
| `POST /api/tasks` | Accepts `orchestrator: "ai"\|"human"` (default `"ai"`). Any other value returns 400. |
| `GET /api/tasks`, `GET /api/tasks/{ref}`, `/api/status` | Each task carries `orchestrator`, plus `waiting_on: "question"\|"plan"\|null`. |
| `POST /api/tasks/{ref}/respond` | Unchanged. On a question wait, the answer resumes the session as the next prompt. |
| MCP `submit_task` | New `orchestrator="ai"` argument. |

**Events:** a new `question` event `{run, text}` is emitted when a Human run stops on a question, before the status moves to `waiting`. Plan approval keeps its current `plan` event and gate.

**How a Human run talks to you (under the hood)**

- In a Human run, the driver's headless note tells the agent this: a human answers. To ask, end the turn with the question(s) in plain text and then the line `Reply to answer`. Never use AskUserQuestion. The run orchestrator is `human`, so the agent runs `superlite-cli orchestrator set human`.
- The client's superlite preamble says the same: `orchestrator set human` replaces `set ai`.
- The worker sees `Reply to answer` at the end of a Human run's result. It parks the run in `waiting` with `waiting_on=question`. There is no `_gate` commit and no plan event.
- A plan result (`Reply approve to start`) still goes through `_gate`, as today, and sets `waiting_on=plan`.
- In an AI run, the question marker is ignored, so AI runs do not change.
- AskUserQuestion is disallowed in every cloud Claude run (`--disallowedTools`), because nobody can answer it headless.

## Acceptance checks

1. `factory send "fix the login bug" --superlite --orchestrator human` creates a run. `cloud runs` shows it with `[human]`.
2. The agent asks a question. The watch pane prints `? <question>`, and `cloud runs` shows `waiting: question`. The branch has no gate commit.
3. Typing an answer in the watch pane (or in an attached session) moves the run to `queued` and then `running`, and the agent's next text uses the answer.
4. When the agent's plan is ready, the run waits with `waiting: plan` and the plan event shows. `/approve` resumes it, and it builds.
5. A run sent with no flag and no saved default is AI. It never stops on `Reply to answer`, and nothing about it differs from today.
6. `factory use cloud --orchestrator human`, then `factory send ...` with no flag, gives a Human run. `--orchestrator ai` on the send overrides the default.
7. `POST /api/tasks` with `orchestrator: "robot"` returns 400. MCP `submit_task(..., orchestrator="human")` makes a Human run.
8. An existing database (no `orchestrator` column) starts fine. Its old tasks read as `ai`.
9. Inside a Human run, after the user's approve answer, `superlite-cli orchestrator approve --review <plan>` succeeds (the reply was recorded).

## Code map

**superlite-cloud** (`src/slcloud/`). Stood up by `slcloud.app` (FastAPI `api.py`, worker loop `worker.loop_once`). Tests are pytest, `.venv/bin/python -m pytest -q -m "not slow"`.

- `engine.py`:
  - Task dataclass (l.32-50), `FIELDS` (l.24) and `PUBLIC` (l.25): add `orchestrator` and `waiting_on`.
  - `from_row` (l.53): `None` → `"ai"`.
  - `submit(...)` (l.85): new keyword `orchestrator="ai"`, validated.
  - `respond` (l.185): clears `waiting_on`.
- `store.py:8-24`: the schema. Add an idempotent `ALTER TABLE tasks ADD COLUMN orchestrator TEXT` and `waiting_on TEXT`. There is no migration framework.
- `api.py:66-79`: submit passes `body.get("orchestrator", "ai")`. A `ValueError` becomes `Rejected` (400), the same path as a bad name.
- `mcp_server.py:43`: `submit_task` argument.
- `adapters.py`:
  - `PLAN_MARKER` (l.9): add `QUESTION_MARKER = "Reply to answer"`.
  - `_result` (l.20): a `question` flag.
- `drivers/claude.py`:
  - `headless_note` (l.13-22): orchestrator-aware.
  - `--disallowedTools` (l.34): add `AskUserQuestion`.
  - The driver needs the task's orchestrator (via `drivers/base.py:43-63`).
- `worker.py`:
  - The `waiting` transition (l.362-367): a question branch with no `_gate`, which emits the `question` event and sets `waiting_on`.
  - `_gate` (l.79): sets `waiting_on=plan`.
  - Resume (l.283) with `answer_for` (l.45): unchanged.
- Tests:
  - `tests/test_worker.py`: `FAKE_CLAUDE` l.23-52 gets a `question` prompt prefix. Also `test_result_event_on_done_and_waiting` l.273.
  - `tests/test_e2e.py` (`_setup` l.70, `test_flow_approve` l.134): add a question → answer → plan → approve flow.
  - Also `test_engine`, `test_store`, `test_api`, `test_mcp`, `test_drivers` (PLAN_MARKER l.154), `test_adapters`.
- FROZEN: existing routes and their fields, `respond`/`nudge` semantics, `PLAN_MARKER`, `_gate` behavior, and the event kinds already emitted.

**superlite** (`skills/swarm/scripts/`, called S below). Stdlib only for `cloud*.py` (checked by `test_cloud_integration.py:148`). Tests: `superlite-cli run_tests --fast PATHS`.

- `S/factory.py`:
  - Parsers l.440-470: `--orchestrator` on `send` and `use`.
  - `cmd_send` l.303-325: resolve flag → saved default → `ai`, then pass it to `superlite_prompt` and `p.send`.
  - `cmd_use` l.268-290.
- `S/factory_registry.py`:
  - `builtin_orchestrator` key, mirroring `BUILTIN_SUPERLITE_KEY` (:26).
  - `inst["orchestrator"]` in `use()` (:174) and `add()` (:158).
  - `orchestrator_default(name)` next to `superlite_enabled()` (:197).
- `S/factory_api.py:16-44`: `SUPERLITE_PREAMBLE` becomes mode-aware, as `superlite_prompt(text, orchestrator="ai")`. The AI text stays byte-identical.
- `S/cloud_factory.py:50-68`:
  - `send(..., orchestrator="ai")` puts `orchestrator` in the body only when it is `human`, so AI sends to an older server do not change.
  - The `startswith(SUPERLITE_PREAMBLE)` check (:57) moves to a shared prefix test.
- `S/cloud_watch.py`:
  - `render` gets a `question` kind (`? text`).
  - `handle_input` sends a plain line as `respond` while the last seen status is `waiting` on a question. This needs a small state the reader thread shares.
- `S/cloud.py:85-112` (`cmd_runs`): `[human]` and `waiting: <on>`. `S/cloud_status.py:133` stays as is.
- `S/cloud_hook.py:267-304`: already sends a plain line as `respond` when not running. No change, only a test.
- Docs: `commands/send.md`, `commands/factory.md`, `skills/swarm/orchestrator.md` (cloud Human runs).
- Tests:
  - `test_factory_api.py:55`, `test_factory_cli_e2e.py`, `test_cloud_factory.py:66`, `test_factory_registry.py`.
  - `test_cloud_watch.py`, `test_cloud_hook.py:159`, `test_cloud_cli.py`.
  - `test_factory_commands_md.py:53`, `test_cloud_commands_md.py` (docs match the parser).
- FROZEN: the existing `send`/`use` flags, the AI preamble text, the `cloud_client` API, and the hook routing rules.

## Prototype scope

The sandbox is `.superlite/sandboxes/human-orchestration/human-orchestration/`. It holds an L1 cut:

- Engine, store and API: submit plus respond plus events only.
- The worker loop, with a fake claude that asks one question, then posts a plan, then finishes.
- The client `cloud_watch` render and `handle_input`, plus a `send` shim with `--orchestrator`.

To try it, run one command that starts the server and worker, then `send --orchestrator human`, then answer in the watch pane, then `/approve`. It is at most 6 tasks.

## Integration build (outline)

| # | Task | Repo |
|---|---|---|
| 1 | store and engine: columns, migration, Task fields, submit validation | cloud |
| 2 | api and mcp: the `orchestrator` field and 400 | cloud |
| 3 | adapters and driver: question marker, headless note, disallowed AskUserQuestion | cloud |
| 4 | worker: question wait branch, `waiting_on`, `question` event, plus the e2e flow test | cloud |
| 5 | registry and factory: flag, saved default, mode-aware preamble | superlite |
| 6 | cloud_factory send body, `cloud runs` display | superlite |
| 7 | cloud_watch: question render and answer routing | superlite |
| 8 | docs: send, factory and orchestrator md, plus the md-parser tests | superlite |
| 9 | seam check: a cross-repo e2e (real client against a live local slcloud with the fake claude) covering checks 1-6 | both |

## Decisions

- **Question marker:** `Reply to answer`, on the last line. It is distinct from `Reply approve to start`, so the two waits never mix up.
- **AI runs:** the question marker is ignored, so an AI run never parks on a question (check 5).
- **AskUserQuestion:** disallowed in every cloud run, both modes. It cannot be answered headless in either.
- **Old servers:** the client sends `orchestrator` only for Human runs. When the server's reply lacks `orchestrator`, the client warns: `server does not support human orchestration; run is AI`.
- **Recording the reply (check 9):** local `orchestrator approve` needs `.superlite/human-replies.jsonl`, which the UserPromptSubmit hook writes. Verified 2026-11-27 with real `claude -p`: the hook fires on a fresh prompt, a `--resume` prompt, and a stream-json resume. So the worker needs no extra reply writing.
- **Saved default:** stored per factory, next to the superlite default. It applies to `send` only.
- **Tests at finish:** the full suite runs once, in both repos.
