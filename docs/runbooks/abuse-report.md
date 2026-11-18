# Abuse report from netcup

netcup forwards complaints about traffic from the server's address (port
scans, spam, attacks, copyright) and expects an answer, usually within a
short deadline; an unanswered one can get the server locked. Answer fast,
even if only to say you're looking.

How the server is built answers most of them: the targets (what chat
controls) have **no route to the internet**. Their only way out is the
`egress` proxy: HTTP and HTTPS to a short allowlist of sites, private and
link-local addresses refused, bandwidth capped (`TARGET_NET_LIMIT`). No NAT
on `chatnet`; nftables drops everything else (PLAN.md D13, D37;
`infra/tests/net-isolation.sh` proves it).

## 1. Read it

What, when (with the time zone), from which address and port, to where.
Is it the server's address (bootstrap prints it: "public IPv4")? Is it something the server
does at all (the stream to YouTube, the site on 80/443, WireGuard, SSH)?

## 2. Pause what could be the source

If it could be the target: panic and stop it
(`sudo incus stop chatpc --project targets`) until you know. The stream
can show `chat-bcast-scene brb`.

## 3. Check the logs at that time

```sh
# The target's web requests through egress (14 days):
sudo incus exec egress --project infra -- grep 'HOST_OR_ADDRESS' /var/log/squid/access.log | tail -50
# Refusals (private addresses, sites off the allowlist):
sudo incus exec egress --project infra -- grep -c TCP_DENIED /var/log/squid/access.log
# Who logged in:
last -n 20
sudo infra/tests/net-isolation.sh        # the isolation still holds
```

Something in the logs that the isolation shouldn't allow:
[compromise.md](compromise.md).

## 4. Answer

From the netcup customer control panel (or by replying to the email):
what you checked, what you found, what you did. For traffic the server
can't have made, say so with the reasons above, and attach the log lines
for that time (they show the target's requests, never viewers' data). For
copyright about the stream: the content is a live desktop controlled by
chat, moderated; what you removed and when.

## 5. Afterwards

Resume when you understand it. If the server did send it: fix the cause
(an allowlist entry too wide, a service exposed), re-run
`infra/tests/net-isolation.sh`, and write the post-mortem in
`docs/incidents/`.
