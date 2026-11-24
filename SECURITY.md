# Security

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately through the repository's security advisory page. Include the version, the steps to reproduce, and the impact you expect.

## Supported versions

Security fixes go to the newest 0.1.x release.

## Security model

**Agent runs.** Claude runs do not use `--bare`, because superlite needs its plugin hooks and the Skill and Task tools. The cloned repo is untrusted, so the adapter passes `--setting-sources user` and `--strict-mcp-config`: a repo's `.claude/settings.json` hooks and its `.mcp.json` servers never load, and only the superlite plugin from `--plugin-dir` does. The repo's `CLAUDE.md` is still read as project guidance. Runs use `--permission-mode bypassPermissions` with `IS_SANDBOX=1`: nobody can approve a prompt in a headless run, so the agent can run any command inside its sandbox, and the sandbox container is the boundary. Earlier versions of the adapter passed `--bare`, which also kept repo hooks out but left only Bash, Edit and Read, so superlite never actually ran. See `docs/notes/2026-11-23-headless-superlite-runs.md`.

**Sandboxes.** A task sandbox receives only its own working directory, mounted at `/work`, the one API key for that task's provider, and fixed non-secret settings the agent needs (`IS_SANDBOX=1` for Claude). It gets no other host environment variables and no other host paths. Because the agent can run any command, anything in the sandbox, including that API key, is reachable by code the agent runs. Prompt injection from repo content could send the key out. Prefer a spend-limited key, and consider gVisor (`SLCLOUD_GVISOR=1`) and restricted egress. Sandboxes have network access, so an agent can reach any host the worker can reach. Run the server on a network that you trust for that.

**Process runner (dev and test only).** `SLCLOUD_SANDBOX=process` runs the agent as a plain process on the host: there is no container boundary, yet Claude still gets `--permission-mode bypassPermissions` and `IS_SANDBOX=1`. The agent can run any command with the worker's own privileges and read everything the worker user can. Never use `process` for untrusted repos or on a shared or public server. The default is `docker`. Under `process`, `HOME` is a per-run directory beside the checkout (never the checkout), so the repo cannot supply the agent's "user" settings, and the agent works in the run directory instead of `/work`.

**Docker socket.** The worker service mounts `/var/run/docker.sock` so it can start sandbox containers. Access to the Docker socket is equivalent to root on the host. Treat the worker as privileged. Sandbox containers never mount the socket. Only the `worker` service in `compose.yaml` mounts it.

**API keys.** Provider API keys are encrypted at rest with `SLCLOUD_MASTER_KEY`. Anything that lists or displays a key shows its last 4 characters only. Keys never appear in logs, events, errors or API output. Keep `.env` out of version control. The master key must be high-entropy. Generate it with `openssl rand -hex 32`. In 0.1 the vault key is derived as the SHA-256 of the master key, with no salted KDF, so a weak master key can be brute-forced from the stored ciphertext. If the master key is lost, the stored keys cannot be decrypted, and you must set them again.

**Tokens.** Workspace tokens are shown once, when they are created. The server stores only their SHA-256 hashes.

## Hardening for a public server

- Serve slcloud behind HTTPS and set `SLCLOUD_URL` to the `https://` address.
- Give each member their own token, and use the member role unless the person must administer the workspace.
- Limit who can reach the server. The worker's Docker socket access is the main host-level risk.
