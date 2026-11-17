"""Tests for site_sync.py: brain's site copied onto the host, trusting only the manifest.

python3 -m unittest discover -s infra/host -v
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_sync


def entry(data: bytes) -> dict[str, Any]:
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class FakeBrain:
    """brain's /var/lib/chat-brain/site, served as `incus exec` would."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.manifest: dict[str, Any] | None = None  # None: made from the files
        self.extra: list[tarfile.TarInfo] = []  # members a hostile tar adds
        self.served: dict[str, bytes] = {}  # what the tar sends instead of a file
        self.commands: list[list[str]] = []

    def manifest_bytes(self) -> bytes:
        if self.manifest is not None:
            return json.dumps(self.manifest).encode()
        files = {p: entry(d) for p, d in self.files.items()}
        return json.dumps({"version": 1, "generated": 1_800_000_000, "files": files}).encode()

    @contextlib.contextmanager
    def __call__(self, command: list[str]) -> Iterator[IO[bytes]]:
        self.commands.append(command)
        if command[0] == "cat":
            yield io.BytesIO(self.manifest_bytes())
            return
        assert command[:5] == ["tar", "-C", site_sync.REMOTE, "-cf", "-"] and command[5] == "--"
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w") as tar:
            for info in self.extra:
                tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isreg() else None)
            for name in command[6:]:
                data = self.served.get(name, self.files.get(name))
                if data is None:
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        out.seek(0)
        yield out


SITE = {
    "index.html": b"<!doctype html>home",
    "runs/1.html": b"<!doctype html>run 1",
    "img/run-1.png": b"\x89PNG last",
    "style.css": b"body{}",
}


class Sync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.www, self.state = self.tmp / "www", self.tmp / "var" / "site-sync.json"

    def sync(self, brain: FakeBrain) -> dict[str, Any]:
        return site_sync.sync(brain, self.www, self.state)

    def test_a_first_copy_then_only_what_changed(self) -> None:
        brain = FakeBrain(SITE)
        result = self.sync(brain)
        self.assertEqual(result, {"files": 4, "fetched": 4, "removed": 0, "problems": []})
        for path, data in SITE.items():
            self.assertEqual((self.www / path).read_bytes(), data)
            self.assertEqual(os.stat(self.www / path).st_mode & 0o777, 0o644)
        brain.commands.clear()
        self.assertEqual(self.sync(brain)["fetched"], 0)
        self.assertEqual([c[0] for c in brain.commands], ["cat"])  # the manifest only
        brain.files["runs/1.html"] = b"<!doctype html>run 1, dead"
        brain.files["runs/2.html"] = b"<!doctype html>run 2"
        del brain.files["img/run-1.png"]
        result = self.sync(brain)
        self.assertEqual((result["fetched"], result["removed"]), (2, 1))
        self.assertEqual(brain.commands[-1][6:], ["runs/1.html", "runs/2.html"])
        self.assertFalse((self.www / "img").exists())  # the empty folder went too
        self.assertEqual((self.www / "runs/1.html").read_bytes(), b"<!doctype html>run 1, dead")

    def test_what_was_changed_on_the_host_is_put_back(self) -> None:
        brain = FakeBrain(SITE)
        self.sync(brain)
        (self.www / "index.html").unlink()
        (self.www / "stray.html").write_text("x")
        (self.www / "runs" / "evil.html").symlink_to("/etc/passwd")
        result = self.sync(brain)
        self.assertEqual((result["fetched"], result["removed"]), (1, 2))
        self.assertTrue((self.www / "index.html").is_file())
        self.assertFalse((self.www / "stray.html").exists())
        self.assertFalse((self.www / "runs" / "evil.html").is_symlink())

    def test_a_manifest_with_a_path_outside_the_site_is_refused_whole(self) -> None:
        for bad in ("../etc/passwd.html", "/etc/x.html", ".hidden.html", "a/../b.html", "run.sh",
                    "manifest.json", "UPPER.html", "a b.html", "a/b/c/d/e.html", ""):  # fmt: skip
            brain = FakeBrain(SITE)
            brain.manifest = {
                "version": 1,
                "files": {**{p: entry(d) for p, d in SITE.items()}, bad: entry(b"x")},
            }
            with self.assertRaisesRegex(site_sync.SyncError, "refused path"):
                self.sync(brain)
        self.assertFalse(self.www.exists())

    def test_sizes_and_shapes_are_checked(self) -> None:
        cases: list[tuple[Any, str]] = [
            ({"version": 2, "files": {}}, "version 1"),
            ([1, 2], "version 1"),
            ({"version": 1, "files": {"a.html": {"size": site_sync.MAX_FILE + 1, "sha256": "0" * 64}}},
             "too large"),
            ({"version": 1, "files": {"a.html": {"size": True, "sha256": "0" * 64}}}, "no size"),
            ({"version": 1, "files": {"a.html": {"size": 1, "sha256": "zz"}}}, "SHA-256"),
            ({"version": 1, "files": {"a.html": {"size": 1, "sha256": "0" * 64, "x": 1}}}, "bad entry"),
        ]  # fmt: skip
        for manifest, error in cases:
            brain = FakeBrain({})
            brain.manifest = manifest
            with self.assertRaisesRegex(site_sync.SyncError, error):
                self.sync(brain)

    def test_only_regular_files_that_match_the_manifest_are_written(self) -> None:
        brain = FakeBrain(SITE)
        link = tarfile.TarInfo("index.html")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/shadow"
        device = tarfile.TarInfo("style.css")
        device.type = tarfile.CHRTYPE
        unasked = tarfile.TarInfo("../../root/.ssh/authorized_keys")
        unasked.size = 3
        brain.extra = [link, device, unasked]
        brain.served["runs/1.html"] = b"<!doctype html>tampered"  # not what the manifest says
        result = self.sync(brain)
        self.assertEqual(result["fetched"], 3)
        self.assertEqual(len(result["problems"]), 1)
        self.assertIn("runs/1.html", result["problems"][0])
        self.assertFalse((self.www / "runs/1.html").exists())
        self.assertFalse((self.www / "index.html").is_symlink())
        self.assertEqual((self.www / "index.html").read_bytes(), SITE["index.html"])
        self.assertFalse((self.tmp / "root").exists())
        del brain.served["runs/1.html"]
        self.assertEqual(self.sync(brain)["fetched"], 1)  # the next round gets it

    def test_a_failed_fetch_is_reported_and_the_rest_kept(self) -> None:
        brain = FakeBrain(SITE)
        self.sync(brain)

        @contextlib.contextmanager
        def broken(command: list[str]) -> Iterator[IO[bytes]]:
            if command[0] == "tar":
                raise site_sync.SyncError("tar in brain: exit status 2")
            with brain(command) as out:
                yield out

        brain.files["index.html"] = b"<!doctype html>new"
        result = site_sync.sync(broken, self.www, self.state)
        self.assertEqual(result["problems"], ["tar in brain: exit status 2"])
        self.assertEqual((self.www / "index.html").read_bytes(), SITE["index.html"])  # the old one stays
        self.assertEqual(self.sync(brain)["fetched"], 1)

    def test_check_says_how_fresh_the_copy_is(self) -> None:
        self.assertFalse(site_sync.check(self.state)[0])
        self.sync(FakeBrain(SITE))
        ok, text = site_sync.check(self.state)
        self.assertTrue(ok)
        self.assertTrue(text.startswith("4 files, copied 0 s ago"), text)
        ok, text = site_sync.check(self.state, time.time() + site_sync.STALE + 60)
        self.assertFalse(ok)
        self.assertIn("stale", text)


if __name__ == "__main__":
    unittest.main()
