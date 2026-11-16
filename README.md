# Chat Uses

A live channel where chat controls a real computer: it types, clicks, reboots, rewinds and votes on what happens next. Video streams to YouTube; Discord is the control room.

Everything runs on one KVM server without nested virtualization: Linux targets are Incus system containers, other OSes run in QEMU software emulation.

**Start here:** [PLAN.md](PLAN.md) has the architecture, game design, safety rules and the phased roadmap.

## Runbooks

| Phase | Runbook | Tools |
|---|---|---|
| 0 — Groundwork | [docs/phase0.md](docs/phase0.md) | `infra/host/server-check.sh`, `infra/discord/setup_server.py` |
| 1 — Host foundation | [docs/phase1.md](docs/phase1.md) | `infra/host/bootstrap.sh`, `infra/host/wg-add-peer.sh`, `infra/tests/net-isolation.sh` |
| 2 — Target image | [docs/phase2.md](docs/phase2.md) | `images/build.sh debian-xfce`, `images/debian-xfce/check.sh` |
| 3 — Broadcast pipeline | [docs/phase3.md](docs/phase3.md) | `images/build.sh bcast`, `images/bcast/deploy.sh`, `images/bcast/check.sh` |
| 4 — Control plane | [docs/phase4.md](docs/phase4.md) | `control/` (`uv run pytest`), `images/build.sh brain`, `images/brain/deploy.sh`, `images/brain/check.sh`, `chat-brain-say`, `chat-brain-admin` |
| 5 — Safety layer | [docs/phase5.md](docs/phase5.md) | `images/build.sh egress`, `images/build.sh intranet`, their `deploy.sh` and `check.sh`; `bootstrap.sh incus-api`, `images/brain/panic-drill.sh` |

## Layout

```
infra/host/      host bootstrap, firewall template, config (host.env.example)
infra/incus/     networks, projects and profiles as YAML
infra/tests/     isolation tests
infra/discord/   Discord server setup (stdlib Python) and its tests
images/          images built by images/build.sh: the target (debian-xfce), the
                 broadcaster (bcast), the control plane (brain), the egress proxy
                 and the intranet; probes for VNC, audio and the overlay feed
overlay/         the stream layout page (served by bcast)
control/         the control plane ("brain", Python): chat commands in, desktop input out
docs/            runbooks
```
