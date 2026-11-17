"""Tests for backup.py: whole backups and restore drills against a fake incus and a fake
restic (a folder for a repository), and the monitor's reading of how they went.

    python3 -m unittest discover -s infra/host -v
"""

import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup
import monitor

FAKE_INCUS = r"""#!/usr/bin/env python3
import io, json, os, shutil, sqlite3, sys, tarfile
from pathlib import Path
args = sys.argv[1:]
here = Path(__file__).parent
inside = here / "brain-root"  # brain's files, as seen from the host
flags = (here / "flags").read_text().split()
images = json.loads((here / "images.json").read_text())  # name -> fingerprint
log = here / "incus.log"
with log.open("a") as f:
    f.write(" ".join(args) + "\n")
copy = inside / "var/lib/chat-brain/backup/brain.db"
if args[:2] == ["exec", "brain"] and "chat-brain-admin" in args:
    if "brain-down" in flags:
        sys.exit("Error: Instance is not running")
    copy.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(copy)
    for table in ("users", "commands", "events", "runs", "settings"):
        db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT)")
    db.execute("INSERT INTO users VALUES ('dc:1')")
    db.commit()
    db.close()
    print(f"Backup: {copy} (0.0 MB)")
elif args[:2] == ["exec", "brain"] and "rm" in args:
    copy.unlink(missing_ok=True)
elif args[:2] == ["file", "pull"]:
    source, target = args[-2], args[-1]
    shutil.copy(inside / source.removeprefix("brain/"), target)
elif args[:2] == ["image", "info"]:
    name = args[2].removeprefix("chat-uses/")
    if name not in images:
        sys.exit("Error: Image not found")
    print(f"Fingerprint: {images[name]}\nSize: 1.00MiB")
elif args[:2] == ["image", "export"]:
    name, target = args[2].removeprefix("chat-uses/"), Path(args[3])
    data = b"rootfs"
    with tarfile.open(target / f"{images[name]}.tar", "w") as tar:
        info = tarfile.TarInfo("rootfs/hello")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
else:
    sys.exit("unexpected: " + " ".join(args))
"""

FAKE_RESTIC = r"""#!/usr/bin/env python3
import fnmatch, json, os, shutil, sys
from pathlib import Path
args = sys.argv[1:]
repo = Path(os.environ["RESTIC_REPOSITORY"])
password = Path(os.environ["RESTIC_PASSWORD_FILE"]).read_text().strip()
with (Path(__file__).parent / "restic.log").open("a") as f:
    f.write(" ".join(args) + "\n")
if args[:2] == ["cat", "config"]:
    if not (repo / "config").exists():
        sys.exit(10)
    sys.exit(0)
if args[0] == "init":
    repo.mkdir(parents=True)
    (repo / "config").write_text(password)
    sys.exit(0)
if not (repo / "config").exists():
    print("Fatal: repository does not exist", file=sys.stderr)
    sys.exit(10)
if (repo / "config").read_text() != password:
    print("Fatal: wrong password or no key found", file=sys.stderr)
    sys.exit(12)
kept = repo / "snapshots"
snapshots = sorted(kept.glob("*"), key=lambda p: int(p.name)) if kept.exists() else []
if args[0] == "backup":
    excludes, paths, i = [], [], 1
    while i < len(args):
        if args[i] == "--exclude":
            excludes.append(args[i + 1]); i += 2
        elif args[i] in ("--tag", "--host"):
            i += 2
        elif args[i].startswith("--"):
            i += 1
        else:
            paths.append(args[i]); i += 1
    target = repo / "snapshots" / str(len(snapshots) + 1)
    for path in paths:
        for file in [Path(path)] if Path(path).is_file() else Path(path).rglob("*"):
            if not file.is_file():
                continue
            name = str(file)
            if any(fnmatch.fnmatch(name, p.replace("**/", "*")) for p in excludes):
                continue
            dest = target / str(file).lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(file, dest)
    target.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"message_type": "status", "percent_done": 1}))
    made = f"{len(snapshots) + 1:08d}abcdef"
    print(json.dumps({"message_type": "summary", "snapshot_id": made, "data_added": 1234}))
elif args[0] == "forget":
    pass
elif args[0] == "snapshots":
    print(json.dumps([{"short_id": f"{int(s.name):08d}", "time": "2026-11-18T04:20:00Z"} for s in snapshots]))
elif args[0] == "stats":
    print(json.dumps({"total_size": 2_500_000_000}))
elif args[:2] == ["restore", "latest"]:
    target = Path(args[args.index("--target") + 1])
    shutil.copytree(snapshots[-1], target, dirs_exist_ok=True)
else:
    sys.exit("unexpected: " + " ".join(args))
"""


class Server(unittest.TestCase):
    """A host with a brain, two golden images and Google Drive (a folder)."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, script in (("incus", FAKE_INCUS), ("restic", FAKE_RESTIC)):
            fake = self.bin / name
            fake.write_text(script)
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        (self.bin / "flags").write_text("")
        self.images = {"debian-xfce": "a" * 64}
        self.save_images()
        self.etc, self.var, self.site, self.wg = tmp / "etc", tmp / "var", tmp / "www", tmp / "wireguard"
        secrets = self.etc / "secrets"
        secrets.mkdir(parents=True)
        (secrets / "restic-password").write_text("correct horse\n")
        (secrets / "discord-bot-token").write_text("token\n")
        (self.etc / "rclone.conf").write_text("[gdrive]\ntype = drive\n")
        (self.etc / "host.env").write_text("SITE_DOMAIN=chat.example.org\n")
        (self.site / "runs").mkdir(parents=True)
        (self.site / "index.html").write_text("<p>Ana</p>")
        (self.site / "runs" / "1.html").write_text("<p>Ana</p>")
        (self.site / "runs" / "1.png").write_bytes(b"png")
        self.wg.mkdir()
        (self.wg / "wg0.conf").write_text("[Interface]\n")
        self.repo = tmp / "drive"
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        self.enterContext(mock.patch.dict(os.environ, env))
        for name, value in (
            ("ETC", self.etc),
            ("VAR", self.var),
            ("SITE", self.site),
            ("WIREGUARD", self.wg),
            ("STATE", self.var / "backup.json"),
            ("STAGE", self.var / "backup"),
            ("PASSWORD", secrets / "restic-password"),
            ("RCLONE", self.etc / "rclone.conf"),
            ("SETTINGS", self.etc / "backup.env"),
            ("HERE", tmp),  # installed: the settings come from backup.env
        ):
            self.enterContext(mock.patch.object(backup, name, value))
        (self.etc / "backup.env").write_text(
            f"BACKUP_REPOSITORY={self.repo}\nBACKUP_KEEP_DAYS=7\nBACKUP_IMAGES=debian-xfce\n"
        )

    def save_images(self) -> None:
        (self.bin / "images.json").write_text(json.dumps(self.images))

    def main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = backup.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def snapshot(self, number: int) -> Path:
        return self.repo / "snapshots" / str(number)

    def in_snapshot(self, number: int, path: Path) -> Path:
        return self.snapshot(number) / str(path).lstrip("/")

    def state(self) -> dict[str, object]:
        return json.loads((self.var / "backup.json").read_text())


class Backups(Server):
    def test_a_night_backup_and_the_next(self) -> None:
        self.assertEqual(self.main("--init")[0], 0)
        self.assertIn("there already", self.main("--init")[1])
        code, out, err = self.main()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("backup: done (snapshot 00000001)", out)
        state = self.state()
        self.assertEqual((state["snapshot"], state["added"], state["error"]), ("00000001", 1234, None))
        store = self.var / "backup" / "brain.db"
        self.assertEqual(store.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.in_snapshot(1, store).is_file())
        self.assertFalse((self.bin / "brain-root/var/lib/chat-brain/backup/brain.db").exists())  # cleaned up
        image = self.var / "backup" / "images" / f"debian-xfce-{'a' * 12}" / f"{'a' * 64}.tar"
        self.assertTrue(self.in_snapshot(1, image).is_file())
        self.assertTrue(self.in_snapshot(1, self.etc / "secrets" / "discord-bot-token").is_file())
        self.assertTrue(self.in_snapshot(1, self.wg / "wg0.conf").is_file())
        self.assertTrue(self.in_snapshot(1, self.site / "runs" / "1.png").is_file())
        self.assertFalse(self.in_snapshot(1, self.etc / "secrets" / "restic-password").exists())
        self.assertFalse(self.in_snapshot(1, self.site / "index.html").exists())  # pages name viewers
        self.assertFalse(self.in_snapshot(1, self.site / "runs" / "1.html").exists())
        restic_log = (self.bin / "restic.log").read_text()
        self.assertIn(
            "forget --tag chat-uses --host chat-uses --group-by host,tags --keep-within 7d --prune",
            restic_log,
        )
        incus_log = (self.bin / "incus.log").read_text()
        self.assertIn("chat-brain-admin backup 7", incus_log)

        self.assertEqual(self.main()[0], 0)  # the same build: not exported again
        self.assertEqual((self.bin / "incus.log").read_text().count("image export"), 1)
        self.images["debian-xfce"] = "b" * 64  # a new build
        self.save_images()
        self.assertEqual(self.main()[0], 0)
        folders = sorted(p.name for p in (self.var / "backup" / "images").iterdir())
        self.assertEqual(folders, [f"debian-xfce-{'b' * 12}"])  # the old export goes

    def test_it_says_how_it_goes(self) -> None:
        self.main("--init")
        with mock.patch.object(backup, "PROGRESS_EVERY", 0.0):
            _code, out, _err = self.main()
        lines = [line.removeprefix("backup: ") for line in out.splitlines()]
        self.assertEqual(lines[0], "brain's store: brain copies it")
        self.assertTrue(lines[1].startswith("brain's store: copied ("), lines)
        self.assertEqual(lines[2], f"image debian-xfce: exporting build {'a' * 12} (a few minutes)")
        self.assertTrue(lines[3].startswith("image debian-xfce: exported ("), lines)
        self.assertTrue(lines[4].startswith("restic: encrypting and sending what changed to "), lines)
        self.assertTrue(lines[4].endswith("(the first time: everything, the image too)"), lines)
        self.assertIn("100%", lines)  # restic's progress
        self.assertTrue(
            any(line.startswith("restic: snapshot 00000001, 0 MB new, in ") for line in lines), lines
        )
        self.assertIn("restic: deleting the backups more than 7 days older", lines)
        again = self.main()[1]
        self.assertIn(f"image debian-xfce: this build ({'a' * 12}) is out already", again)
        self.assertNotIn("the first time", again)

    def test_progress_lines(self) -> None:
        progress = backup._progress  # pyright: ignore[reportPrivateUsage]
        start = {"percent_done": 0.0, "total_bytes": 1.36e9, "bytes_done": 0, "seconds_remaining": 1_031_280}
        self.assertEqual(progress(start), "0% (0.00 of 1.36 GB), comparing with the last backup")
        going = {"percent_done": 0.42, "total_bytes": 2.9e9, "bytes_done": 1.2e9, "seconds_remaining": 240}
        self.assertEqual(progress(going), "42% (1.20 of 2.90 GB), about 4 min left")
        wild = {"percent_done": 0.05, "seconds_remaining": 10**7}
        self.assertEqual(progress(wild), "5%")

    def test_brain_down_the_rest_is_still_backed_up(self) -> None:
        self.main("--init")
        self.main()
        (self.bin / "flags").write_text("brain-down")
        code, _out, err = self.main()
        self.assertEqual(code, 1)
        self.assertIn("brain's store: Error: Instance is not running", err)
        state = self.state()
        self.assertIn("brain's store", str(state["error"]))
        self.assertEqual(state["snapshot"], "00000002")  # a snapshot all the same
        self.assertFalse((self.var / "backup" / "brain.db").exists())  # the stale copy goes
        self.assertTrue(self.in_snapshot(2, self.etc / "host.env").is_file())

    def test_a_missing_image_or_repository_is_said(self) -> None:
        self.main("--init")
        (self.etc / "backup.env").write_text(
            f"BACKUP_REPOSITORY={self.repo}\nBACKUP_KEEP_DAYS=7\nBACKUP_IMAGES=debian-xfce bcast\n"
        )
        code, _out, err = self.main()
        self.assertEqual(code, 1)
        self.assertIn("image chat-uses/bcast: not found", err)
        (self.etc / "backup.env").write_text(f"BACKUP_REPOSITORY={self.repo}-elsewhere\n")
        code, _out, err = self.main()
        self.assertEqual(code, 1)
        self.assertIn("restic backup: Fatal: repository does not exist", err)

    def test_nothing_without_a_password_or_rclone(self) -> None:
        (self.etc / "secrets" / "restic-password").unlink()
        code, _out, err = self.main()
        self.assertEqual(code, 1)
        self.assertIn("no restic password", err)
        self.assertIn("no restic password", str(self.state()["error"]))
        self.assertNotIn("last_ok", self.state())
        (self.etc / "secrets" / "restic-password").write_text("x\n")
        (self.etc / "rclone.conf").unlink()
        (self.etc / "backup.env").write_text("BACKUP_IMAGES=\n")  # the default: Google Drive
        self.assertIn("no rclone settings", self.main()[2])

    def test_check(self) -> None:
        self.main("--init")
        self.main()
        code, out, _err = self.main("--check")
        self.assertEqual(code, 0)
        self.assertIn("backup: 1 backups in", out)
        self.assertIn("2.50 GB stored", out)
        self.assertIn("last restore drill: never", out)


class Drills(Server):
    def test_a_drill_restores_and_checks_everything(self) -> None:
        self.main("--init")
        self.main()
        code, out, _err = self.main("--drill")
        self.assertEqual(code, 0, out)
        self.assertIn("drill: ✓ brain's store: 1 users, 0 runs, 0 events", out)
        self.assertIn("drill: ✓ secrets: discord-bot-token", out)
        self.assertIn(f"drill: ✓ image debian-xfce-{'a' * 12}: 1 file(s), readable", out)
        self.assertIn("drill: all good", out)
        state = self.state()
        self.assertTrue(state["drill_ok"])
        self.assertEqual(list((self.var).glob("drill-*")), [])  # the restored copy is gone

    def test_a_damaged_backup_fails_its_drill(self) -> None:
        self.main("--init")
        self.main()
        store = self.in_snapshot(1, self.var / "backup" / "brain.db")
        store.write_bytes(b"not a database at all" * 100)
        image = next(self.snapshot(1).rglob("*.tar"))
        image.write_bytes(b"\x00garbage")
        code, out, _err = self.main("--drill")
        self.assertEqual(code, 1)
        self.assertIn("drill: ✗ brain's store is not a database", out)
        self.assertIn("unreadable", out)
        self.assertFalse(self.state()["drill_ok"])


class Integrity(unittest.TestCase):
    def test_what_a_store_must_be(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "brain.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE users (id TEXT)")
            db.commit()
            db.close()
            self.assertEqual(
                backup.integrity(path), "not brain's store (no commands, events, runs, settings)"
            )


class MonitorReadsIt(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_quiet_until_backups_are_set_up_and_while_they_work(self) -> None:
        self.assertEqual(monitor.backup_problems({}, self.NOW), {})
        fine = {"last_ok": self.NOW - 3600, "error": None, "drill": self.NOW - 86400, "drill_ok": True}
        self.assertEqual(monitor.backup_problems(fine, self.NOW), {})

    def test_failed_late_and_no_drill(self) -> None:
        found = monitor.backup_problems({"last_try": self.NOW, "error": "no restic password"}, self.NOW)
        self.assertEqual(
            found,
            {
                "backup failed": "the last backup had a problem: no restic password",
                "backup none": "no backup has worked yet: journalctl -u chat-uses-backup",
            },
        )
        late = monitor.backup_problems({"last_ok": self.NOW - 40 * 3600, "drill": self.NOW}, self.NOW)
        self.assertEqual(late, {"backup late": "no backup has worked for 40 h"})
        never = monitor.backup_problems({"last_ok": self.NOW}, self.NOW)
        self.assertEqual(never, {"drill": "no restore drill done yet: sudo chat-uses-backup --drill"})
        old = monitor.backup_problems({"last_ok": self.NOW, "drill": self.NOW - 100 * 86400}, self.NOW)
        self.assertEqual(old["drill"], "no restore drill for 100 days: sudo chat-uses-backup --drill")
        failed = monitor.backup_problems(
            {"last_ok": self.NOW, "drill": self.NOW, "drill_ok": False}, self.NOW
        )
        self.assertIn("the last restore drill failed", failed["drill"])
        self.assertEqual(monitor.grace("backup failed"), 0)


if __name__ == "__main__":
    unittest.main()
