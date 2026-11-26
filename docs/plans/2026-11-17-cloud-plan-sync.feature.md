# Cloud plan sync: pull, edit, push, approve

Spans two repos: **superlite-cloud** (server + worker) and **superlite** (client CLI). Session slug: `cloud-plan-sync`.

## Goal

A superlite user whose run is executing on superlite cloud can, when the run stops at its plan gate, **pull the run's files to their machine** (any file on the run's branch, the plans first), **read and edit them in their own editor**, **push the edits back**, and only then **approve**. The run resumes from the edited files. The plan becomes something the user shapes before any build starts, not just text they say yes to.

For: every slcloud member who submits runs (owner of the run; admins on any run).

## What changes

**Commands (superlite client)**

| Command | What it does |
|---|---|
| `superlite-cli cloud pull <run>` | Fetches the run's branch at its plan-gate commit into `.superlite/cloud/<run>/` (a git worktree of your local clone, or a plain clone when you are not in one). Prints the folder, the branch, the sha and the plan files, and opens the review plan when `open_in_harness` is on. |
| `superlite-cli cloud push <run>` | Commits your edits in that folder and sends them back (auto-commit is on by default; turn it off for your superlite user with `superlite-cli cloud autocommit off` (stored in `$SUPERLITE_HOME/preferences.json`; `cloud autocommit status` shows it, and `cloud attach` shows it next to the run) or per command with `--no-commit`, and push then refuses uncommitted edits; `--commit` forces it on). Prints the new sha and the changed files. Refused unless the run is still `waiting` and your edits sit on top of its gate commit. |
| `/superlite:pull <run>`, `/superlite:push <run>` | Slash forms of the two commands above, same shape as `/superlite:watch`. |
| MCP `list_files`, `read_file`, `write_file` | For any agent client without git: list the branch's files, read one, or write one (a new commit on top of the gate commit, made in slcloud's repo cache). Same owner rules and fast-forward rule as push. |
| `/approve` (attached or in the watch pane) | Unchanged, except when the pulled folder has edits that were never pushed: it pushes them first, then approves. |

**How the files travel (chosen under the hood, the user never picks)**

Nothing leaves the worker until someone asks. The first `pull` on a waiting run asks slcloud to **publish** it: the worker commits the checkout (the gate commit), copies it into the shared repo cache, and tries to push `superlite/sessions/<slug>` to origin. `pull` waits for that, printing `publishing <run>...`, up to 120 s.

1. **Through the git remote** (GitHub or Bitbucket), the default: once published, pull is a `git fetch` from origin, and push is a fast-forward `git push` to that branch. Your own git credentials are used; the server never sees them.
2. **Direct through slcloud**, the fallback, used when origin refused the push, can't be reached, or your machine can't fetch from it: pull downloads a git bundle from slcloud, and push uploads one.

**Endpoints (slcloud)**

| Route | What it returns or does |
|---|---|
| `POST /api/tasks/{ref}/publish` | Asks the worker to publish a `waiting` run. 202 `{state:"pending"}`, or 200 with the files body when it's already published. 409 if the run is not `waiting` |
| `GET /api/tasks/{ref}/files` | `{state:"pending"}`, or `{state:"ready", branch, sha, pushed, files:[paths], plans:[paths], edits:{sha, files}|null}`. 409 if never published or not `waiting` |
| `GET /api/tasks/{ref}/files/bundle` | The git bundle of the branch at `sha` |
| `POST /api/tasks/{ref}/files` | Records the edits: `{sha, via:"remote"}`, or a bundle body with `?base=<sha>`. 409 if the run is not `waiting` or the edits are not a fast-forward of the gate sha |

**Events:** at the gate, a new `plan` event `{plans}` (plan paths only; the worker lists them from the checkout) before the run moves to `waiting`; the watch pane shows `plan ready: <plans> (superlite-cli cloud pull <run>)`. On publish, a `published` event `{branch, sha, pushed}`.

**Resume:** on approve the worker moves the run's checkout to the edited sha (fast-forward only) and starts the agent with an answer that names the edited files, so the agent rereads them before building.

## Acceptance checks

1. Submit a run whose agent writes `docs/plans/<x>.review.md` and stops at the gate. `cloud pull <run>` creates `.superlite/cloud/<run>/` holding the whole branch (not only the plan), and prints the plan path.
2. The watch pane for that run shows a `plan ready:` line naming the plan file.
3. Edit the plan and one non-plan file, then run `cloud push <run>`. It prints a new sha and both changed paths, and `GET /api/tasks/<run>/files` now shows `edits` with that sha.
4. `/approve`: the resumed run's first agent turn receives an answer that names both edited files, and the run's checkout holds your edited text.
5. Before the first pull, origin has no `superlite/sessions/<slug>` branch. After it, it does (when origin accepts the push).
6. An MCP client calls `read_file` on the plan, then `write_file` with new text. `GET /files` shows the edit, and check 4 holds for it.
7. When origin refuses pushes (no remote credentials on the worker), checks 1 and 3 still pass through the bundle path, and the output says `via slcloud`.
8. Push after the run has moved on (cancelled, or already approved): refused with a clear error, exit 1, nothing changed on the server.
9. Another member's run: `pull` gets `not found`, exactly as other run commands do.
10. A pushed edit that rewrites history (not a fast-forward of the gate sha): refused with 409.

## Code map

**superlite-cloud** (`.venv/bin/python -m pytest -q -m "not slow"`; flat `tests/test_<module>.py`, fakes for engine, runner and driver; no network in fast tests)
- Stand-up: `slcloud serve` → `app.build` (`app.py:64`): `api.create_app` + `stream.make_router` + MCP at `/mcp`, wrapped by `BearerMiddleware`. `slcloud worker` → `worker.loop_once`. In compose only the worker mounts `SLCLOUD_WORK`; both mount `SLCLOUD_DATA` (repo caches and bundles).
- Plan gate: `worker._run_task` (`worker.py:178`). The `PLAN_MARKER` match (`adapters.py:9`) adds a `result` event and moves to `waiting` (`worker.py:250-253`). This is where the gate commit, push and `plan` event go.
- Resume: `worker.py:192` (`resuming = session and answer and isdir(run_dir)`). The fast-forward to the edits goes here, through the sandbox's git (`sb.sh`), never host git in the run dir (`test_no_git_in_run_dir`).
- Git: `gitops.safe_hand_back` (`gitops.py:97`) and `repocache` `fetch_from`, `push(expect)` with a lease, `bundle`, `tip`, `fetch` (`repocache.py:36-125`). These are reused for the gate push.
- Store: the unused `tasks.dir` column (`store.py:13`). New gate and edit state goes into a new `task_files` table (no migrations: schema in `slcloud init`).
- Publish job: the worker loop (`worker.loop_once`, `app.run_worker`) also picks publish requests for `waiting` runs. Only the worker can reach `work_dir/<task_id>`. The server reads published files from the repo cache under DATA (`git show`/`ls-tree` on the bare repo), which it can reach.
- MCP: `mcp_server.py:108` tool list, `guard` error mapping, owner check through `runs.get_run`. Tests: `tests/test_mcp.py`.
- REST: `api.py`. New routes must be registered **before** the greedy `GET /api/tasks/{ref:path}` (`api.py:139`). Owner check: `runs.get_run(store, who, ref)`. Errors map as `NotFound`→404, `BadTransition`→409, `Rejected`→400.
- Events: `engine.add_live_event` (`engine.py:195`).
- **Frozen:** the existing MCP tools (new ones are added), every existing REST route and its body; `Task.public()` keys; MCP tool names and arguments; `HandBack` fields; the `{"error"}` shape and 404 (never 403) for runs you can't see; branch name `superlite/sessions/<slug>`; secrets masked in all output.

**superlite** (`superlite-cli run_tests --fast tests/scripts/test_cloud_*.py`; stdlib only, enforced; integration fake `tests/scripts/cloud_fake.py`)
- CLI: launcher `skills/swarm/scripts/superlite-cli` → `cloud.py main()` (`:157`), argparse subcommands. HTTP: `cloud_client.Client._call` (`:28`), errors `CloudError(.status)`. Config: `cloud_config`.
- Approve: `cloud_hook.py:300` (`/approve` → `respond`) and `cloud_watch.py:66`. Slash routing: `_SLASH` regex (`cloud_hook.py:23`) and the `hooks/prompt` case list (`:35-37`).
- Watch render: `cloud_watch.render` (`:34-59`). A `plan` kind gets added here.
- Opening plans: `agent_specs.open_in_harness()` and `using-superlite/references/<harness>-tools.md`.
- Docs and contract tests: `commands/<verb>.md` (one-line body, `test_cloud_commands_md.py` CMDS list), `test_doc_contract.py` (every `superlite-cli` call must exist), `assets/help.txt`, README.
- **Frozen:** `Client` method signatures and routes, `CloudError.status`, the token never printed, `cloud_hook.route`/`render` shapes and fail-open, `attach.json`, `cloud_watch.render`/`main` signatures, existing `cloud` subcommands and output lines, `orchestrator approve --review` semantics, the `hooks/prompt` fast path.

## Decisions

- Decided: any file on the run's branch can be pulled and edited, not only plans. The plans are listed first and opened.
- Decided: pull, edit, push, approve. The git remote (GitHub/Bitbucket) is the default path, and slcloud bundles are the fallback; superlite picks one, the user never does.
- Decided: the local copy is a git worktree at `.superlite/cloud/<run>/`. The user's checkout is never touched.
- Decided: `/approve` pushes unpushed edits first, then approves.
- Decided: CLI, slash commands and MCP `list_files`/`read_file`/`write_file` all ship.
- Decided: nothing is published until the first pull. That adds a worker publish job, because only the worker can reach the run's checkout.
- Ruling: `pull` waits up to 120 s for the publish, then exits 1 with `still publishing, try again` - the worker may be busy with another run - cost if wrong: one timeout flag.
- Ruling: MCP `write_file` and bundle edits are committed in slcloud's repo cache; the worker only fast-forwards the checkout on resume - one write path for REST, MCP and remote - cost if wrong: one task.
- Ruling: edits are accepted only while the run is `waiting` and only as a fast-forward of the gate sha (else 409) - the agent resumes from a state it can trust - cost if wrong: none for v1.
- Ruling: git credentials are never sent to slcloud. The remote path uses the user's own git locally and the worker's ambient git config, as hand-back does today.

## Prototype scope

- **Keep:** `slcloud` `api.create_app` with the real store, `repocache` against local bare repos on `tmp_path` (the "origin"), and the client's `cloud.py`, `cloud_client.py` and `cloud_watch.render`.
- **Stub:** the agent driver (writes a plan file and the marker), docker (process sandbox), webhooks, auth (one fixed principal). MCP tools are thin over the same service functions, so the prototype calls those functions directly.
- **Level:** L2. Real git objects and real bundles, because the fast-forward and bundle shapes are the risk.
- One script stands up server + worker + a fake origin on localhost, so you can run `pull` / edit / `push` / `approve` by hand.

## Integration outline

- **slcloud:** T1 `task_files` store (publish request, gate sha, edits) · T2 `files` service over the repo cache (list, read, bundle, accept edits from a bundle, a remote sha or one written file; fast-forward rule) · T3 worker: `plan` event at gate + publish job (commit, cache, push, bundle fallback) · T4 REST routes (publish, files, bundle, POST edits) · T5 MCP `list_files`/`read_file`/`write_file` · T6 worker resume fast-forward + edited-files answer · T7 seam test (worker ↔ api ↔ cache).
- **superlite:** T8 `cloud_client` files methods · T9 `cloud pull` (publish + wait, remote fetch first, bundle fallback, worktree) · T10 `cloud push` (commit, remote push first, bundle fallback, record edits) · T11 watch `plan` line + `/approve` pushes unpushed edits · T12 commands/*.md, `_SLASH`, help, README · T13 seam test against `cloud_fake`.
- Every existing test in both repos must still pass. The prototype's acceptance tests are the target.
