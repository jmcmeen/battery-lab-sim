# Security Policy

## Scope

This project is a battery-lab simulator intended to run inside `docker compose up` on a developer laptop. It has **no internet-facing attack surface** by design: every service binds to the docker-compose network, telemetry is local MQTT, and storage is local TimescaleDB / MinIO containers. There are no auth flows, no inbound webhooks, no exposed APIs.

That said, two classes of issue are still in scope:

1. **Anything that could harm a user running the project locally** — e.g., container-escape bugs in the dev images, malicious YAML schedules that pivot to host code execution, dependency vulnerabilities with realistic exploit paths.
2. **Documentation that misleads users into an insecure deployment** — e.g., suggesting the placeholder `.env.example` credentials are safe for any environment beyond a single laptop.

## `.env.example` is dev-only

Every secret in `.env.example` is a `changeme` placeholder. **Rotate every credential before any deployment beyond `docker compose up` on a single laptop**, including a shared lab machine. The header comment in `.env.example` says the same thing — please don't ignore it.

## Reporting a vulnerability

Please **do not** open a public issue for security concerns. Instead, use GitHub's private vulnerability reporting:

1. Go to the [Security tab](https://github.com/jmcmeen/battery-lab-sim/security) of the repo.
2. Click "Report a vulnerability".
3. Describe the issue, including a minimal reproduction if possible.

You should hear back within 7 days. If GitHub private advisories are unavailable, email johnmcmeen@gmail.com with `[battery-lab-sim security]` in the subject.

## Supported versions

This is a personal portfolio project; only `main` is supported. There is no LTS branch.
