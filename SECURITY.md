# Security

## Reporting a vulnerability

Do not open a public issue for a security problem. Report it privately through the repository's security advisory page. Include the version, the steps to reproduce, and the impact you expect.

## Supported versions

Security fixes go to the newest 0.1.x release.

## Security model

**Agent runs.** The Claude adapter always passes `--bare`. Bare mode skips hooks, plugin sync and auto-discovery of project configuration, so a run uses only what slcloud gives it.

**Sandboxes.** A task sandbox receives only its own working directory, mounted at `/work`, and the one API key for that task's provider. It gets no other host environment variables and no other host paths. Sandboxes have network access, so an agent can reach any host the worker can reach. Run the server on a network that you trust for that.

**Docker socket.** The worker service mounts `/var/run/docker.sock` so it can start sandbox containers. Access to the Docker socket is equivalent to root on the host. Treat the worker as privileged. Sandbox containers never mount the socket. Only the `worker` service in `compose.yaml` mounts it.

**API keys.** Provider API keys are encrypted at rest with `SLCLOUD_MASTER_KEY`. Anything that lists or displays a key shows its last 4 characters only. Keys never appear in logs, events, errors or API output. Keep `.env` out of version control. The master key must be high-entropy. Generate it with `openssl rand -hex 32`. In 0.1 the vault key is derived as the SHA-256 of the master key, with no salted KDF, so a weak master key can be brute-forced from the stored ciphertext. If the master key is lost, the stored keys cannot be decrypted, and you must set them again.

**Tokens.** Workspace tokens are shown once, when they are created. The server stores only their SHA-256 hashes.

## Hardening for a public server

- Serve slcloud behind HTTPS and set `SLCLOUD_URL` to the `https://` address.
- Give each member their own token, and use the member role unless the person must administer the workspace.
- Limit who can reach the server. The worker's Docker socket access is the main host-level risk.
