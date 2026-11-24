# Running superlite headless in the sandbox: considerations

Found while testing superlite-cloud live (local podman, real Anthropic key, 2026-11-19 to 2026-11-23).

## What was wrong

The Claude adapter ran `claude -p --bare --plugin-dir /opt/superlite --permission-mode acceptEdits`.

- `--bare` skips plugin hooks and leaves only Bash, Edit and Read. There's no Skill or Task tool, so the agent can't load superlite skills or dispatch subagents. Every "superlite" run was really a plain Claude run.
- `acceptEdits` lets the agent edit files but not run shell commands, and nobody is there to approve them. Agents couldn't run tests or `superlite-cli`.

## What was measured

All runs used the worker image `slcloud-worker:0.1.0` (Claude Code 2.1.325, superlite v4.2.0).

| Flags | Tools | Superlite hooks | Repo's own hooks / MCP | Result |
|---|---|---|---|---|
| `--bare`, `acceptEdits` | Bash, Edit, Read | no | no | worked without superlite, couldn't run tests |
| no `--bare`, `bypassPermissions`, `IS_SANDBOX=1` | full set: Skill, Task, Write… | yes | **yes, they run** | superlite small flow, reviewer subagent, 8 tests pass, $0.19 |
| as above + `--setting-sources user --strict-mcp-config` | full set | yes | no | Skill available, SessionStart hook fires |

`IS_SANDBOX=1` is needed because the sandbox runs as root, and Claude refuses `bypassPermissions` as root without it.

## Considerations

1. **Untrusted repo config.** Without `--bare`, Claude loads the cloned repo's `.claude/settings.json` hooks and `.mcp.json` servers. A control run showed both executing in the sandbox. Mitigation: `--setting-sources user --strict-mcp-config`, with the plugin loaded through `--plugin-dir`. The repo's `CLAUDE.md` is still read, which is wanted, because it's project guidance.
2. **The key is inside the sandbox.** With `bypassPermissions` the agent can run anything, and `ANTHROPIC_API_KEY` is in its environment. Prompt injection from repo content could send it out. gVisor (`SLCLOUD_GVISOR=1`) and limiting outbound network to the API and the git remote reduce this. A per-run or spend-limited key reduces the damage. Follow-up.
3. **Plain runs.** Every Claude run in superlite-cloud is a superlite run: the worker already creates a superlite session in each sandbox. Splitting off plain runs isn't needed. A factory client's `--superlite` flag only adds the preamble for superlite-cloud.
4. **Worktrees and hand-back.** Hand-back commits the run's working folder (`/work`) only. In a live run superlite opened its own session (`clamp`) and built in `/work/.claude/worktrees/clamp`, so the handed-back branch came back empty. Fix: the Claude driver disallows `EnterWorktree` and `ExitWorktree` and appends a system-prompt note naming the session the worker opened in `/work` (`session ensure --slug <run slug>`). Rerun live: superlite built in `/work`, and the branch came back with the diff. A multi-task feature build is still untested.
4a. **Two Claude command lines.** Live runs use `drivers/claude.py` (stream-json in and out), not `ClaudeAdapter.command`. Both had `--bare`, and now both share `adapters.CLAUDE_ISOLATION`.
4b. **Compact nudge.** Superlite's prompt hook asks the agent to `/compact`, which a headless run can't do. Claude sandboxes now get `SUPERLITE_NO_AUTOCOMPACT=1`.
5. **Questions in headless runs.** Superlite still asks things, for example a first-run question about turning specs on. A plan that ends in `Reply approve to start` becomes the run's waiting state, and other questions don't. Follow-up: pre-set the setup defaults in the sandbox and surface questions as waiting.
6. **Cost and time.** Superlite runs dispatch subagents and reviewers, so they cost more than plain runs. Follow-up: a per-run cap (`--max-budget-usd` exists for `-p`) and cost on the run page.
7. **Test tools.** The worker image has no pytest or other project toolchains. Runs need a per-repo setup step or a richer image to check their own work. Known limit.
8. **Other agents.** Codex, Gemini and Cursor don't load Claude plugins, so superlite only applies to the `claude` agent. Known limit.
