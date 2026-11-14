# slcloud

slcloud is the superlite.cloud core server. It accepts agent tasks over REST and MCP, queues them, runs each one in a sandbox with the agent CLI you chose, and hands the result back as a git branch. Learning rows from members are collected and reviewed centrally.

## Quick start

Requirements: Docker with Compose, and a git repository your tasks can clone.

1. Create the env file and set a master key. The master key encrypts stored provider keys.

   ```sh
   cp .env.example .env
   # set SLCLOUD_MASTER_KEY in .env to the output of: openssl rand -hex 32
   ```

2. Build and start the server and the worker.

   ```sh
   docker compose up -d --build
   ```

   The server listens on http://localhost:8787. The worker builds the sandbox image `slcloud-worker:0.1.0`.

3. Create a workspace. Run this inside the server container. It prints the workspace ID and an admin token. The token is shown once, so save it.

   ```sh
   docker compose exec server slcloud init
   ```

4. Point the client commands at the server.

   ```sh
   export SLCLOUD_URL=http://localhost:8787
   export SLCLOUD_TOKEN=slc_...   # the admin token from step 3
   ```

5. Store a provider key for the workspace. `slcloud key set` reads the key from stdin, so it stays out of your shell history. Only the last 4 characters are ever shown back.

   ```sh
   slcloud key set anthropic
   ```

6. Create a token for each member. The role is `admin` or `member`. The token is shown once, so give it to that member.

   ```sh
   slcloud token create <workspace> <role>
   ```

7. Connect your agent to the server. `slcloud connect` prints the command to run in that agent, for example for Claude Code, Codex, Gemini or Cursor.

   ```sh
   slcloud connect claude
   ```

8. Send learning rows to the server. Sync keeps an offset, so running it again sends only new rows.

   ```sh
   slcloud sync
   ```

Once connected, agents submit tasks through the `submit_task` MCP tool at `http://localhost:8787/mcp`. Each task starts with a plan. Reply `approve` to the plan to start the run.

## Layout

- `src/slcloud/` is the package. The CLI entry point is `slcloud`.
- `Dockerfile` builds the server image. `Dockerfile.worker` builds the worker and sandbox image, with the agent CLIs pinned by version.
- `compose.yaml` defines the `server` and `worker` services.

## Development

```sh
uv venv --python 3.12 && uv pip install -e '.[dev]'
.venv/bin/python -m pytest -q -m "not slow"
```

## Security

Read [SECURITY.md](SECURITY.md) before you expose the server. It covers how sandboxes are isolated, why the worker holds the Docker socket, and how keys are stored.

## License

MIT. See [LICENSE](LICENSE).
