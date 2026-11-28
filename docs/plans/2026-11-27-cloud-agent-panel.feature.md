# Cloud agent panel (feature plan)

## Goal and who it is for

When you are watching a superlite cloud run in a terminal (`superlite-cli cloud watch <run>`, opened with
`/superlite:watch`), you can see the run's background agents live, like Claude Code's agent list at the bottom of
its screen. Today the watch pane prints only run-level lines (status, result, nudges), so the agents the coordinator
spawns on the worker are invisible from the laptop.

For: anyone who starts a cloud run and watches it from a terminal.

## What changes for the user

- **`superlite-cli cloud watch <run>` in a real terminal:** the screen splits into three parts, as in the approved
  mock-up (`watch_demo.py`):
  - **Feed (top, scrolls):** the existing lines, plus one line when an agent starts (`▶ implementer (sonnet) started
    T2 · driver agent events`) or ends (`■ … finished`, red `■ … blocked`).
  - **Agent panel (pinned above the input):** a header (`agents · <run> · N running · 45.1k tokens`) and one row per
    agent, each under the agent that spawned it: status dot (green running, grey done, red blocked or failed), type,
    tier (opus/sonnet/haiku), task, a running clock, tokens. The panel redraws in place and never scrolls away.
  - **Input line (bottom):** unchanged behaviour: a plain line nudges, `/approve` approves, `/cancel` cancels.
- **No terminal, `TERM=dumb`, or output piped:** no panel. Agent start and end lines are printed as plain feed lines,
  so logs and the agent-harness handoff keep working.
- **Cloud API / event stream:** a new event kind `agent` in a run's events, data
  `{id, parent, type, tier, task, state: start|done|failed, tokens}`. Older clients ignore it (they already ignore
  kinds they do not render).
- **Unchanged:** `/superlite:attach` and nudging from the chat session, the status line, the Desktop agent map.

## Acceptance checks

1. Start a cloud run that spawns subagents and open `superlite-cli cloud watch <run>` in a terminal: the panel shows
   the coordinator and each subagent, nested under its parent, with tier and task, while the feed scrolls above.
2. A running agent's clock ticks and its tokens grow; when it finishes it turns grey, and the header's running count
   drops.
3. An agent that errors shows red, and the feed prints a red `blocked`/`failed` line for it.
4. Typing a line in the watch pane still nudges the run; `/approve` and `/cancel` still work; typed text is not
   overwritten by the panel redraw.
5. Resize the terminal: the panel redraws to the new width without leaving garbage rows; on a narrow terminal (60
   cols) rows are truncated, not wrapped.
6. `superlite-cli cloud watch <run> --follow | cat` (no tty) prints agent lines as plain text with no escape codes.
7. Reconnecting (`--after <id>`) or opening the watch late rebuilds the panel from the run's past `agent` events.
8. An old client (current `cloud_watch.py`) watching a run with `agent` events behaves exactly as today.
9. Background agents (`run_in_background`) show as running until they actually finish, not until their launch call
   returns.

Run the whole test suite at finish: **Yes, once at finish** (both repos).
Agent source: **Claude stream-json** (user choice).

## Code map

Two repos. `cloud` = this worktree (superlite-cloud, `src/slcloud`). `plugin` = `/Users/iheatuwogu/Projects/supers/superlite`
(its own worktree is cut at the integration step).

**Verified stream-json shape** (Claude Code 2.1.332, `claude -p --output-format stream-json --verbose`, sanitized
sample: `docs/plans/2026-11-27-cloud-agent-panel.stream-sample.jsonl`):
- Spawn: an `assistant` line with a `tool_use` block `name: "Agent"`, `id`, `input.{description, subagent_type,
  model, run_in_background}`. The line's own `parent_tool_use_id` is null in the main session, the parent Agent's id
  inside a subagent.
- `system` `subtype: task_started`: `task_id`, `tool_use_id` (= the Agent block id), `description`, `subagent_type`,
  `is_backgrounded`, `spawn_depth`.
- Subagent `assistant` lines carry `parent_tool_use_id` = its Agent id, `message.model` and `message.usage`.
- The `tool_result` for a background Agent comes at once with `tool_use_result.status: "async_launched"`; it is **not**
  the end. Foreground: `tool_use_result.{status: completed, totalTokens, resolvedModel}`.
- End, both kinds: `system` `subtype: task_notification`: `tool_use_id`, `status` (completed|failed|stopped),
  `usage.total_tokens`.
- A `task_notification` can trigger a second turn (a second `init … result`) after the first `result`.

**cloud: entry and event path (frozen shapes marked F)**
- `drivers/claude.py:53-74` `events(line)`: parses `assistant` lines only; `result` → `status`. New: an agent tracker
  in the driver turns the lines above into `{"kind": "agent", ...}` events. Launch flags (`:28-38`) already include
  `--verbose`; no flag change.
- `worker.py:333-340`: `driver.events` → `_turn_end` → `cap.feed`. No change.
- `capture.py:20` `_KINDS` gains `agent`; `feed` (`:111`, `:133`) cleans strings (mask, redact, 500 chars) and keeps
  ints. Every captured event gets `step` (`_emit` `:55`).
- `engine.py:199/212`, `store.py:14` events table; `stream.py:42-53` SSE; `api.py:131`. No change. **F:** event row
  `{id, kind, data, at}`, SSE frame `id/event/data` with `data.run`.
- `runs.py:115-120` `_doing`: add an `agent` branch (`<type>: <task>`), so `live_runs.doing` stays meaningful. **F:**
  `live_runs` item keys `{name, status, step, doing, agent}` (`tests/test_api.py:279`).
- Tests: `tests/test_drivers.py` (`FakeProc :10`, `make :30`, inline JSON event tests `:125-144`),
  `tests/test_capture.py`, `tests/test_runs.py`. Command: `.venv/bin/python -m pytest -q -m "not slow"`.

**plugin: watch client**
- `skills/swarm/scripts/cloud_watch.py`: `render` `:35-60` (unknown kinds → None), `handle_input` `:63`,
  `_read_input` thread `:75`, `main` `:97` (handoff `:103`, loop `:115-121`, plain `print`). **F:** every existing
  render string (`tests/scripts/test_cloud_watch.py:19-23, :80, :85-86`), no ESC in `render` output (`:32-36`), and
  a fake-tty run with no agent events printing exactly `"login_fix done: PR opened\n"` (`:151-161`).
- `cloud_stream.py:117` blocking SSE generator, 30 s read timeout, keepalives swallowed: clocks need a ticker thread.
- `factory_watch.py:21` reuses `cloud_watch.render`/`clean`/`handle_input`; its tests (`test_factory_watch.py:32-42,
  :76, :114, :167-172`) must not change. Factory gets no panel now.
- Conventions: scripts are stdlib only, Python 3.12+, Windows runs Git Bash: no curses/termios. No ANSI helpers exist.
  Tests: `uv run pytest tests/scripts/test_cloud_watch.py` (xdist on).
- Docs: `commands/watch.md:6-10`, `README.md:58`.

## Decisions

- **Parent tree:** parent = the spawning Agent line's `parent_tool_use_id`, null → the root row (`main`, the run's
  coordinator session). Event `id` = the Agent `tool_use_id`. The tree is built from ids, so out-of-order or late
  events still nest.
- **Lifecycle:** `start` on `task_started` (joined to the Agent block by `tool_use_id` for `model`); `done`/`failed`
  only on `task_notification` (`stopped` → `failed`, shown red). A foreground `tool_result` with `status: completed`
  also ends it, in case a build emits no notification; `async_launched` never does.
- **Tier:** `haiku|sonnet|opus` taken from `message.model` / `resolvedModel` / `input.model`, first found; else `?`.
- **Task label:** the Agent `description` (superlite puts `T3 · …` there); `type` = `subagent_type` without the
  plugin prefix (`superlite:implementer` → `implementer`).
- **Tokens:** live = sum of `message.usage` output + input tokens on lines with that `parent_tool_use_id`, deduped by
  `message.id`; final = `task_notification.usage.total_tokens`. The driver emits a `tokens`-only `agent` update at most
  once per 5 s per agent, so the events table does not grow per message.
- **Wire event:** `{"kind": "agent", "id", "parent", "type", "tier", "task", "state": "start|update|done|failed",
  "tokens"}`. Fields are optional except `id` and `state`.
- **Run end:** when the run reaches a terminal status, the client greys any agent still running. The worker's turn
  loop is unchanged (the dropped second turn is a separate, pre-existing issue; noted in the report).
- **Panel gate:** the panel draws only when stdout is a tty, `TERM` is not `dumb`, **and** the first `agent` event has
  arrived. So the existing fake-tty tests and old runs print exactly as today.
- **Drawing:** stdlib ANSI only. A scroll region (DECSTBM) for the feed rows, the panel and input line below it; a
  writer lock around every write; a 1 s ticker thread redraws clocks; resize via `SIGWINCH` where it exists, else
  polled size each tick. On exit the scroll region and cursor are reset. Input stays the existing line-based stdin
  thread (cooked mode: the terminal echoes typed text on the input row).
- **Panel height:** at most `rows - 6` agent rows; past that, running agents first, then the newest, with `+N more`.
- **Feed lines:** `▶ <type> (<tier>) started <task>`, `■ <type> finished <task>`, red `■ <type> failed <task>`. In
  plain mode the same text with no colour.
- **Code shape:** the agent tracker (`stream-json → agent events`) is its own module in cloud
  (`drivers/agents.py`); the panel model and drawing are a new plugin module (`agent_panel.py`), `cloud_watch.py`
  wires it in. `factory_watch` can adopt it later.

## Prototype scope

Sandbox in this checkout. Two pieces, run together on recorded data, no cloud server:
1. A copy of the agent tracker that turns stream-json lines into `agent` events (L1: fed from the sample file plus a
   synthetic fixture with nesting, a failure and 8 agents).
2. A copy of the panel module and a `replay` script: `python replay.py <stream.jsonl> [--speed 4]` plays the lines
   through the tracker into the real pinned panel in your terminal, with a working input line that echoes what a nudge
   would send. `--plain` shows the fallback.
Try-out: run the replay in the right pane, resize it, type a nudge, pipe it to `cat`.

## Integration outline

- cloud: `drivers/agents.py` (tracker) + `drivers/claude.py` wiring; `capture.py` `_KINDS`; `runs.py` `_doing`; tests
  with the sanitized sample as a fixture.
- plugin (own worktree): `skills/swarm/scripts/agent_panel.py` (model + drawing); `cloud_watch.py` wiring (gate,
  ticker, lock, render of agent feed lines); tests for model, plain fallback, drawing on a fake terminal, frozen
  strings; `commands/watch.md` and `README.md` lines.
- Seam check: the plugin's tests replay the cloud tracker's output for the sample fixture (a copied JSON file of
  expected events, so the repos stay independent).
