"""The Incus client against a fake API over real TLS: a pinned server
certificate (self-signed, not a CA, no matching name, like Incus's own) and
a client certificate the server requires."""

import asyncio
import json
import shutil
import ssl
import subprocess
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from chatuses.incus import Incus, IncusError

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def certificate(directory: Path, name: str) -> tuple[Path, Path]:
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:secp384r1", "-nodes",
         "-days", "2", "-subj", f"/CN={name}", "-addext", "basicConstraints=critical,CA:FALSE",
         "-keyout", str(key), "-out", str(crt)],
        check=True, capture_output=True,
    )  # fmt: skip
    return crt, key


class FakeIncusApi:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []
        self.refuse = ""
        self.exec_failure = ""  # the "err" of a failed exec operation

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/1.0/instances", self.instances)
        app.router.add_post("/1.0/instances/{name}/snapshots", self.snapshot)
        app.router.add_get("/1.0/operations/{id}/wait", self.wait)
        app.router.add_get("/1.0/instances/{name}/state", self.state)
        app.router.add_post("/1.0/instances/{name}/exec", self.exec)
        return app

    async def _note(self, request: web.Request) -> None:
        body = json.loads(await request.text()) if request.can_read_body else None
        self.requests.append((request.method, request.path, dict(request.query), body))

    async def instances(self, request: web.Request) -> web.Response:
        await self._note(request)
        addresses = [{"family": "inet6", "address": "fe80::1"}, {"family": "inet", "address": "10.30.0.251"}]
        return web.json_response(
            {
                "type": "sync",
                "metadata": [
                    {"name": "stopped", "state": None},
                    {
                        "name": "other",
                        "state": {"network": {"eth0": {"addresses": [{"address": "10.30.0.9"}]}}},
                    },
                    {
                        "name": "phase2-check",
                        "state": {"network": {"lo": {}, "eth0": {"addresses": addresses}}},
                    },
                ],
            }
        )

    async def snapshot(self, request: web.Request) -> web.Response:
        await self._note(request)
        if self.refuse:
            return web.json_response({"type": "error", "error": self.refuse, "error_code": 403}, status=403)
        return web.json_response({"type": "async", "operation": "/1.0/operations/op1"}, status=202)

    async def wait(self, request: web.Request) -> web.Response:
        await self._note(request)
        if request.match_info["id"] == "exec1" and self.exec_failure:
            failed = {"status": "Failure", "status_code": 400, "err": self.exec_failure}
            return web.json_response({"type": "sync", "metadata": failed})
        if request.match_info["id"] == "exec1":
            return web.json_response(
                {"type": "sync", "metadata": {"status": "Success", "metadata": {"return": 3}}}
            )
        return web.json_response({"type": "sync", "metadata": {"status": "Success"}})

    async def state(self, request: web.Request) -> web.Response:
        await self._note(request)
        return web.json_response({"type": "sync", "metadata": {"status": "Stopped"}})

    async def exec(self, request: web.Request) -> web.Response:
        await self._note(request)
        return web.json_response({"type": "async", "operation": "/1.0/operations/exec1"}, status=202)


@asynccontextmanager
async def serving(tmp_path: Path, api: FakeIncusApi) -> AsyncGenerator[tuple[str, dict[str, Path]]]:
    server_crt, server_key = certificate(tmp_path, "server")
    client_crt, client_key = certificate(tmp_path, "client")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=client_crt)
    context.verify_mode = ssl.CERT_REQUIRED  # like Incus: no trusted client certificate, no answer
    context.load_cert_chain(server_crt, server_key)
    runner = web.AppRunner(api.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    port = runner.addresses[0][1]
    files = {"server": server_crt, "client": client_crt, "client_key": client_key}
    try:
        yield f"https://127.0.0.1:{port}", files
    finally:
        await runner.cleanup()


def test_a_snapshot_of_the_instance_found_by_its_address(tmp_path: Path) -> None:
    async def test() -> None:
        api = FakeIncusApi()
        async with serving(tmp_path, api) as (url, files):
            incus = Incus(url, str(files["client"]), str(files["client_key"]), str(files["server"]))
            assert await incus.snapshot("panic-1", address="10.30.0.251") == "phase2-check"
        assert [(m, p) for m, p, _, _ in api.requests] == [
            ("GET", "/1.0/instances"),
            ("POST", "/1.0/instances/phase2-check/snapshots"),
            ("GET", "/1.0/operations/op1/wait"),
        ]
        assert all(q["project"] == "targets" for _, _, q, _ in api.requests)
        assert api.requests[1][3] == {"name": "panic-1", "stateful": False}

    run(test)


def test_a_named_instance_and_refusals(tmp_path: Path) -> None:
    async def test() -> None:
        api = FakeIncusApi()
        async with serving(tmp_path, api) as (url, files):
            incus = Incus(
                url, str(files["client"]), str(files["client_key"]), str(files["server"]), "other-project"
            )
            assert await incus.snapshot("s", instance="desk/top") == "desk/top"
            assert api.requests[0][1] == "/1.0/instances/desk/top/snapshots"  # the server decodes %2F
            assert api.requests[0][2]["project"] == "other-project"
            with pytest.raises(
                IncusError, match=r"no instance in project other-project has the address 10\.0\.0\.1"
            ):
                await incus.snapshot("s", address="10.0.0.1")
            api.refuse = "not authorized"
            with pytest.raises(IncusError, match="not authorized"):
                await incus.snapshot("s", instance="phase2-check")

    run(test)


def test_another_server_certificate_is_refused(tmp_path: Path) -> None:
    async def test() -> None:
        api = FakeIncusApi()
        async with serving(tmp_path, api) as (url, files):
            impostor, _ = certificate(tmp_path, "impostor")
            incus = Incus(url, str(files["client"]), str(files["client_key"]), str(impostor))
            with pytest.raises(aiohttp.ClientConnectorCertificateError):
                await incus.snapshot("s", instance="phase2-check")
        assert api.requests == []

    run(test)


def test_state_and_exec_for_the_watchdog(tmp_path: Path) -> None:
    async def test() -> None:
        api = FakeIncusApi()
        async with serving(tmp_path, api) as (url, files):
            incus = Incus(url, str(files["client"]), str(files["client_key"]), str(files["server"]))
            assert await incus.state("desk") == "Stopped"
            assert await incus.exec("desk", ["systemctl", "is-active", "--quiet", "chat-xvnc"]) == 3
        [_state, (method, path, _query, body), _wait] = api.requests
        assert (method, path) == ("POST", "/1.0/instances/desk/exec")
        assert body == {
            "command": ["systemctl", "is-active", "--quiet", "chat-xvnc"],
            "environment": {},
            "interactive": False,
            "wait-for-websocket": False,
            "record-output": False,
        }

    run(test)


def test_exec_gives_back_the_exit_statuses_incus_reports_as_errors(tmp_path: Path) -> None:
    # A shell's 127 and 126 come back from Incus as a failed operation (what
    # `rm -rf /` in a target does to systemctl: red-team test 10).
    async def test() -> None:
        api = FakeIncusApi()
        async with serving(tmp_path, api) as (url, files):
            incus = Incus(url, str(files["client"]), str(files["client_key"]), str(files["server"]))
            api.exec_failure = "Command not found"
            assert await incus.exec("desk", ["systemctl", "is-active", "chat-xvnc"]) == 127
            api.exec_failure = "Command not executable"
            assert await incus.exec("desk", ["systemctl", "is-active", "chat-xvnc"]) == 126
            api.exec_failure = "Instance is not running"
            with pytest.raises(IncusError, match="Instance is not running"):
                await incus.exec("desk", ["systemctl", "is-active", "chat-xvnc"])

    run(test)
