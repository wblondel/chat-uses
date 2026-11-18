"""On air or off (images/bcast/.../air.py, PLAN.md D80): a server restart takes the stream off
YouTube until someone puts it back; AIR_AT_BOOT=last keeps it as it was."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

AIR = Path(__file__).resolve().parents[2] / "images/bcast/rootfs/usr/lib/chat-bcast/air.py"


@pytest.fixture
def air(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("air", AIR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    boot = tmp_path / "boot_id"
    boot.write_text("boot-1\n")
    monkeypatch.setattr(module, "AIR", tmp_path / "control" / "air.json")
    monkeypatch.setattr(module, "BOOT_ID", boot)
    monkeypatch.setattr(module, "SETTINGS", (tmp_path / "bcast.env",))
    monkeypatch.delenv("AIR_AT_BOOT", raising=False)
    return module


def test_off_until_put_on_air_and_off_again_after_a_restart(air: ModuleType, tmp_path: Path) -> None:
    assert not air.on_air() and air.status() == "off air (never put on air on this bcast)"
    air.set_air(True, "golive")
    assert air.on_air() and air.status().startswith("on air since ") and air.status().endswith(", by golive")
    assert (air.AIR.stat().st_mode & 0o777) == 0o644
    (tmp_path / "boot_id").write_text("boot-2\n")  # the server restarted
    assert not air.on_air()
    assert air.status().startswith("off air: the server restarted since it was put on air")
    (tmp_path / "bcast.env").write_text('AIR_AT_BOOT="last"\n')  # 24/7: as it was
    assert air.on_air()
    air.set_air(False, "Ana")
    assert not air.on_air() and air.status().startswith("off air since ")


def test_unreadable_is_off(air: ModuleType) -> None:
    air.AIR.parent.mkdir()
    air.AIR.write_text("not json")
    assert not air.on_air()
    air.AIR.write_text(json.dumps(["on"]))
    assert not air.on_air()


def test_the_command(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/systemctl.log\n')
    (bin_dir / "systemctl").chmod(0o755)
    (tmp_path / "boot_id").write_text("boot-1\n")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AIR_FILE": str(tmp_path / "air.json"),
        "AIR_BOOT_ID": str(tmp_path / "boot_id"),
        "STREAM_KEY_FILE": str(tmp_path / "key"),
        "AIR_AT_BOOT": "off",
    }

    def air(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(AIR), *args], env=env, capture_output=True, text=True, check=False
        )

    refused = air("on")
    assert refused.returncode == 1 and "no stream key" in refused.stdout
    (tmp_path / "key").write_text("abcd\n")
    assert air("check").returncode == 1
    done = air("on", "wblondel", "on", "the", "host")
    assert done.returncode == 0 and done.stdout == "on air: YouTube gets the stream in a few seconds\n"
    assert air("check").returncode == 0
    assert "by wblondel on the host" in air("status").stdout
    assert air("off").stdout == "off air: nothing goes to YouTube now\n"
    assert (tmp_path / "systemctl.log").read_text().splitlines() == [
        "restart chat-bcast-forwarder.service"
    ] * 2
    assert air("sideways").returncode == 2
