"""brain's access to the Incus API (PLAN.md 5.3, D4, D44).

A restricted client certificate that reaches only the `targets` project
(infra/host/bootstrap.sh, step incus-api). The host's own certificate is
pinned: brain trusts that one certificate, whatever name it carries.

For now one call: the forensic snapshot taken when a moderator panics
(PLAN.md 7.5). Phase 6 adds rewinds and the runs' lifecycle.

`python -m chatuses.incus` (images/brain/check.sh) checks the access with
brain's settings, changing nothing: it finds the target's instance, and
makes sure the certificate is refused in project infra.
"""

import asyncio
import os
import ssl
import sys
from typing import Any, cast
from urllib.parse import quote

import aiohttp

TIMEOUT = 60.0  # seconds, for a whole call (a btrfs snapshot takes about one)


class IncusError(Exception):
    pass


class Incus:
    def __init__(self, url: str, cert: str, key: str, server_cert: str, project: str = "targets") -> None:
        self.url = url.rstrip("/")
        self.project = project
        self._ssl = ssl.create_default_context(cafile=server_cert)
        self._ssl.check_hostname = False  # pinned: the chain must still end at the host's certificate
        self._ssl.load_cert_chain(cert, key)

    async def _call(
        self, session: aiohttp.ClientSession, method: str, path: str, body: object = None
    ) -> dict[str, Any]:
        sep = "&" if "?" in path else "?"
        url = f"{self.url}{path}{sep}project={quote(self.project)}"
        async with session.request(method, url, json=body, ssl=self._ssl) as resp:
            try:
                data: object = await resp.json(content_type=None)
            except ValueError as err:
                raise IncusError(f"HTTP {resp.status}: not JSON") from err
        if not isinstance(data, dict):
            raise IncusError(f"HTTP {resp.status}: unexpected answer")
        answer = cast(dict[str, Any], data)
        if answer.get("type") == "error":
            raise IncusError(str(answer.get("error") or f"HTTP {resp.status}"))
        return answer

    async def instance_at(self, session: aiohttp.ClientSession, address: str) -> str:
        """The name of the instance holding this IPv4 address."""
        data = await self._call(session, "GET", "/1.0/instances?recursion=2")
        instances: list[dict[str, Any]] = data.get("metadata") or []
        for instance in instances:
            state: dict[str, Any] = instance.get("state") or {}
            networks: dict[str, dict[str, Any]] = state.get("network") or {}
            for net in networks.values():
                addresses: list[dict[str, Any]] = net.get("addresses") or []
                if any(a.get("address") == address for a in addresses):
                    return str(instance["name"])
        raise IncusError(f"no instance in project {self.project} has the address {address}")

    async def find(self, address: str) -> str:
        """The target's instance name, by its address (a read-only call)."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            return await self.instance_at(session, address)

    async def get(self, path: str) -> dict[str, Any]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            return await self._call(session, "GET", path)

    async def snapshot(self, name: str, *, instance: str = "", address: str = "") -> str:
        """Snapshot the instance (named, or found by address) as `name`; returns the instance's name."""
        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if not instance:
                instance = await self.instance_at(session, address)
            data = await self._call(
                session,
                "POST",
                f"/1.0/instances/{quote(instance, safe='')}/snapshots",
                {"name": name, "stateful": False},
            )
            operation = data.get("operation")
            if not isinstance(operation, str) or not operation.startswith("/1.0/operations/"):
                raise IncusError("Incus didn't return an operation")
            done = await self._call(session, "GET", f"{operation}/wait?timeout={int(TIMEOUT)}")
        meta: dict[str, Any] = done.get("metadata") or {}
        if meta.get("status") != "Success":
            raise IncusError(str(meta.get("err") or meta.get("status") or "the snapshot failed"))
        return instance


def main() -> None:
    # Imported here: brain imports this module. And this file runs as
    # __main__, so the errors raised are chatuses.incus's, not this copy's.
    from chatuses.brain import Config
    from chatuses.incus import IncusError

    config = Config.from_env(os.environ)
    incus, why_not = config.incus()
    if incus is None:
        sys.exit(f"Incus access off: {why_not}")
    try:
        name = asyncio.run(incus.find(config.target_host))
    except (IncusError, OSError, aiohttp.ClientError, TimeoutError) as err:
        sys.exit(f"Incus API at {config.incus_url}: {err or type(err).__name__}")
    # brain's own instance: it exists, and the certificate must not reach it.
    incus.project = "infra"
    try:
        asyncio.run(incus.get("/1.0/instances/brain"))
    except IncusError as err:
        if "permission" not in str(err) and "not authorized" not in str(err).lower():
            sys.exit(f"Incus API at {config.incus_url}, project infra: {err} (expected a refusal)")
    else:
        sys.exit(f"Incus API at {config.incus_url}: brain's certificate reaches project infra (restrict it!)")
    where = f"Incus API at {config.incus_url}"
    print(f"{where}: target {config.target_host} is instance {name}; project infra refused")


if __name__ == "__main__":
    main()
