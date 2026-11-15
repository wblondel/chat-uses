# Chat Uses

A live channel where chat controls a real computer: it types, clicks, reboots, rewinds and votes on what happens next. Video streams to YouTube; Discord is the control room.

Everything runs on one KVM server without nested virtualization: Linux targets are Incus system containers, other OSes run in QEMU software emulation.

**Start here:** [PLAN.md](PLAN.md) has the architecture, game design, safety rules and the phased roadmap.

## Runbooks

| Phase | Runbook | Tools |
|---|---|---|
| 0 — Groundwork | [docs/phase0.md](docs/phase0.md) | `infra/host/server-check.sh`, `infra/discord/setup_server.py` |
| 1 — Host foundation | [docs/phase1.md](docs/phase1.md) | `infra/host/bootstrap.sh`, `infra/host/wg-add-peer.sh`, `infra/tests/net-isolation.sh` |

## Layout

```
infra/host/      host bootstrap, firewall template, config (host.env.example)
infra/incus/     networks, projects and profiles as YAML
infra/tests/     isolation tests
infra/discord/   Discord server setup (stdlib Python) and its tests
docs/            runbooks
```
