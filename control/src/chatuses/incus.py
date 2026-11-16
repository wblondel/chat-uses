"""brain's access to the Incus API (PLAN.md 5.3, D4, D44).

A restricted client certificate that reaches only the `targets` project
(infra/host/bootstrap.sh, step incus-api). The host's own certificate is
pinned: brain trusts that one certificate, whatever name it carries.

For now one call: the forensic snapshot taken when a moderator panics
(PLAN.md 7.5). Phase 6 adds rewinds and the runs' lifecycle.
"""

import ssl
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
