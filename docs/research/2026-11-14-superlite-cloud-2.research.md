# Research plan: superlite.cloud

Slug: `superlite-cloud-2` · Level: Simple · Orchestrator: human · Tiers: A, B, C

## Scope

superlite.cloud is a server that wraps superlite so it runs in the cloud as a software factory. Local superlite (and Claude or other local agents) talk to it, for example over MCP. It runs builds in the background, syncs run and learning data from local runs and other connected users, and should feel the same as local superlite. The core is open source and self-hostable (Docker first, for small engineering teams). Revenue comes from a hosted, enterprise-ready offering.

In scope: the competitor landscape and where superlite.cloud can be better, open-source and hosted business models and licences, the architecture for the local-to-cloud bridge, sandboxed execution, multi-model agents with bring-your-own keys, syncing learning data across a team, and the path to market.
Out of scope: writing code, picking a cloud vendor contract, legal advice beyond what public terms say.

Background: superlite main at 4.1.0 (per-repo learning loop merged), `docs/plans/2026-11-12-superlite-learning-loop.*`, `docs/how-a-run-works.md`, `docs/sessions.md`.

## Constraints given by the user

- Keep the superlite core: reuse the existing CLI, skills and agents inside cloud workers.
- Multi-model: must run Codex, Gemini, Cursor and similar agents, not only Claude.
- Bring your own key: customers pay model costs.
- Self-host first: a Docker image that small teams can set up easily comes before the hosted SaaS. Enterprise hosting is how it makes money.

## Sub-questions (one researcher each)

1. **Landscape.** What do current cloud software factories and background coding agents offer (Factory, Devin, OpenAI Codex cloud, GitHub Copilot coding agent, Cursor background agents, Google Jules, Claude Code on the web and Managed Agents, OpenHands Cloud, others)? Pricing, open or closed, self-host, multi-model, local-to-cloud handoff, learning across runs. Where are the gaps?
2. **Business model and licence.** How do open-source dev tools with a hosted paid tier make money (OpenHands, n8n, Sentry, PostHog, Supabase, GitLab, Coder, Daytona and others)? Compare permissive plus hosting, open-core, and source-available (BSL, FSL, ELv2). What do enterprises need before they pay (SSO, audit, VPC, SOC 2)?
3. **Local-to-cloud bridge.** What is the best protocol for local superlite and local agents to drive a remote factory: remote MCP (streamable HTTP, OAuth), A2A, plain REST with webhooks? How do existing tools hand off between local and cloud (Claude Code teleport and remote sessions, Codex cloud tasks, Cursor)? How do you keep it feeling local (status, approval, artifacts back in the checkout)?
4. **Execution and multi-model.** How do you run untrusted agent builds safely in Docker self-host and in hosted multi-tenant setups (devcontainers, gVisor, Firecracker, e2b, Daytona, Modal, Kubernetes)? How do you run Claude Code, Codex CLI, Gemini CLI and others headless? What do provider terms say about bring-your-own key and using consumer subscriptions on a server?
5. **Team learning sync.** What superlite data exists to sync (metrics, incidents, lessons, routing, run archives)? How should it sync across a team (event log, CRDT, server of record), with privacy, redaction and opt-in? How do others share agent memory across a team?
6. **Go to market.** How have open-source developer-infra tools gone from a Docker one-liner to enterprise revenue? What does a fast setup look like (time to first run)? Which channels and pricing fit (per seat, per run, usage)? Which wedge should superlite.cloud lead with?

## Search strategy

Official docs, pricing pages, licence texts and repos first (tier A). Then engineering blogs, benchmarks and reputable analysis (tier B). Then forums, social posts and vendor marketing (tier C). Prefer 2025–2026 sources; a newer source of a higher grade supersedes an older one. Facts about superlite come from this repo (tier A).

## Evidence tiers

- **A**: primary sources: official docs, pricing pages, licence texts, terms of service, papers, source repos (including superlite itself).
- **B**: reputable secondary sources: engineering blogs, benchmarks, analyst write-ups, credible press.
- **C**: forums, social posts, vendor marketing claims.

## Rulings from the user's answers

- Ruling: compare all three licence models and recommend one (because the user asked for a comparison).
- Ruling: the first wedge is easy Docker self-host for small teams, and the revenue target is enterprise hosting (because the user said ease of setup is key for now).
- Ruling: all four build constraints apply (core reuse, multi-model, BYOK, self-host first).
