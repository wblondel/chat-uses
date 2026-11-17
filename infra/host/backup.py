#!/usr/bin/env python3
"""Back up what a rebuilt server couldn't make again, to Google Drive (PLAN.md Phase 10).

    sudo infra/host/backup.py           back up now (the chat-uses-backup timer: every night)
    sudo infra/host/backup.py --init    make the repository (once: bootstrap.sh backup does it)
    sudo infra/host/backup.py --check   the backups there are, their size, the last one's age
    sudo infra/host/backup.py --drill   a restore drill: the latest backup restored to a
                                        temporary folder and checked, then deleted

restic encrypts and deduplicates the backups; rclone carries them to Google
Drive (BACKUP_REPOSITORY, "rclone:gdrive:chat-uses-backups": the rclone
remote "gdrive" in /etc/chat-uses/rclone.conf). What goes in:

- brain's store: a copy brain makes itself (chat-brain-admin backup, SQLite's
  online backup), with YouTube's data expired as it will be BACKUP_KEEP_DAYS
  later, so no backup ever holds any past YouTube's 30 days (API policy III.E.4);
- the golden images (BACKUP_IMAGES, "debian-xfce"), exported once per build;
- the host's settings and secrets (/etc/chat-uses but restic's own password,
  /etc/wireguard) and its tools' records (/var/lib/chat-uses: session reports);
- the site's pictures (/var/www/chat-uses without its pages: brain writes those
  again from its store, and they name YouTube viewers).

Backups older than BACKUP_KEEP_DAYS (7) are deleted after each new one. How
it went is kept in /var/lib/chat-uses/backup.json, which the monitor reads:
#ops hears when a backup fails, when none has worked for 36 hours, and when
no restore drill was done for 3 months.

restic's password is /etc/chat-uses/secrets/restic-password. Keep a copy off
the server (a password manager): without it nobody can read the backups.
Standard library only.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
VAR = Path(os.environ.get("CHAT_USES_VAR", "/var/lib/chat-uses"))
ETC = Path(os.environ.get("CHAT_USES_ETC", "/etc/chat-uses"))
SITE = Path(os.environ.get("CHAT_USES_SITE", "/var/www/chat-uses"))
WIREGUARD = Path("/etc/wireguard")
STATE = VAR / "backup.json"  # how the last backup and the last drill went (the monitor reads it)
STAGE = VAR / "backup"  # brain's store and the images, for restic to take
PASSWORD = ETC / "secrets" / "restic-password"
RCLONE = ETC / "rclone.conf"
SETTINGS = ETC / "backup.env"  # bootstrap.sh's backup step writes it from host.env
BRAIN_COPY = "/var/lib/chat-brain/backup/brain.db"  # where chat-brain-admin backup leaves it
TAG = "chat-uses"
DEFAULTS = {
    "BACKUP_REPOSITORY": "rclone:gdrive:chat-uses-backups",
    "BACKUP_KEEP_DAYS": "7",
    "BACKUP_IMAGES": "debian-xfce",
}
TABLES = ("users", "commands", "events", "runs", "settings")  # what a store must have


# --- the host ----------------------------------------------------------------------------


def run(
    *args: str, timeout: float | None = 600, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """A command's output; one that hangs fails instead (exit 124)."""
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"{args[0]}: timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", f"{args[0]}: not installed")


def config() -> dict[str, str]:
    """BACKUP_* from host.env (through lib.sh) or, installed, from backup.env."""
    names = tuple(DEFAULTS)
    if (HERE / "lib.sh").exists():
        values = " ".join(f'"${{{name}-}}"' for name in names)
        script = f'source "{HERE}/lib.sh"; load_config; printf "%s\\0" {values}'
        found = dict(zip(names, run("bash", "-c", script).stdout.split("\0"), strict=False))
    else:
        try:
            lines = SETTINGS.read_text().splitlines()
        except OSError:
            lines = []
        found = dict(line.split("=", 1) for line in lines if "=" in line)
    return {name: found.get(name) or default for name, default in DEFAULTS.items()}


def last_line(answer: subprocess.CompletedProcess[str]) -> str:
    lines = (answer.stderr or answer.stdout).strip().splitlines()
    return lines[-1][:300] if lines else f"exit {answer.returncode}"


def restic(
    settings: dict[str, str], *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"RESTIC_CACHE_DIR": "/var/cache/restic"} | os.environ  # a timer's root may have no HOME
    env |= {
        "RESTIC_REPOSITORY": settings["BACKUP_REPOSITORY"],
        "RESTIC_PASSWORD_FILE": str(PASSWORD),
        "RCLONE_CONFIG": str(RCLONE),
    }
    return run("restic", *args, timeout=timeout, env=env)


def integrity(path: Path) -> str | None:
    """What's wrong with this copy of brain's store (None: it's whole)."""
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            (check,) = db.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            db.close()
    except sqlite3.Error as err:
        return f"not a database ({err})"
    if check != "ok":
        return f"damaged ({check})"
    if missing := [t for t in TABLES if t not in tables]:
        return f"not brain's store (no {', '.join(missing)})"
    return None


# --- what goes in ------------------------------------------------------------------------


def brain_store(keep_days: int) -> str | None:
    """brain's copy of its store, into STAGE/brain.db. What went wrong (None: done)."""
    made = run(
        "incus", "exec", "brain", "--project", "infra", "--", "chat-brain-admin", "backup", str(keep_days),
        timeout=300,
    )  # fmt: skip
    if made.returncode != 0:
        # The last copy goes: kept in backup after backup, its YouTube data would outlive
        # its 30 days. The backups already made still hold it.
        (STAGE / "brain.db").unlink(missing_ok=True)
        return f"brain's store: {last_line(made)}"
    part = STAGE / "brain.db.part"
    part.unlink(missing_ok=True)
    pulled = run("incus", "file", "pull", "--project", "infra", f"brain{BRAIN_COPY}", str(part))
    run("incus", "exec", "brain", "--project", "infra", "--", "rm", "-f", BRAIN_COPY)
    if pulled.returncode != 0:
        return f"brain's store: can't copy it out ({last_line(pulled)})"
    if wrong := integrity(part):
        part.unlink(missing_ok=True)
        return f"brain's store: the copy is {wrong}"
    part.chmod(0o600)
    part.replace(STAGE / "brain.db")
    return None


def fingerprint(name: str) -> str | None:
    answer = run("incus", "image", "info", f"chat-uses/{name}", "--project", "infra")
    match = re.search(r"^Fingerprint:\s*([0-9a-f]{12,64})\s*$", answer.stdout, re.M)
    return match[1] if answer.returncode == 0 and match else None


def images(names: list[str]) -> list[str]:
    """Each golden image exported to STAGE/images/NAME-FINGERPRINT/, once per build. What went wrong."""
    folder = STAGE / "images"
    folder.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    keep: set[str] = set()
    for name in names:
        fp = fingerprint(name)
        if fp is None:
            errors.append(f"image chat-uses/{name}: not found")
            keep.update(p.name for p in folder.glob(f"{name}-*"))  # the last export stays
            continue
        target = folder / f"{name}-{fp[:12]}"
        keep.add(target.name)
        if (target / ".done").exists():
            continue  # this build is out already
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir()
        exported = run(
            "incus", "image", "export", f"chat-uses/{name}", f"{target}/", "--project", "infra", timeout=3600
        )  # fmt: skip
        if exported.returncode != 0 or not any(target.iterdir()):
            errors.append(f"image chat-uses/{name}: export failed ({last_line(exported)})")
            shutil.rmtree(target, ignore_errors=True)
            keep.discard(target.name)
            continue
        (target / ".done").write_text(fp + "\n")
    for old in folder.iterdir():
        if old.name not in keep:
            shutil.rmtree(old, ignore_errors=True)  # an older build, or an image no longer listed
    return errors


def paths() -> list[str]:
    return [str(p) for p in (ETC, WIREGUARD, VAR, SITE) if p.exists()]


def excludes() -> list[str]:
    patterns = [str(PASSWORD), f"{SITE}/*.html", f"{SITE}/**/*.html", f"{STAGE}/*.part", f"{VAR}/drill-*"]
    return [arg for pattern in patterns for arg in ("--exclude", pattern)]


def summary(output: str) -> dict[str, Any]:
    """restic backup --json's last word: the snapshot and what it added."""
    for line in reversed(output.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("message_type") == "summary":
            return data
    return {}


def load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(state: dict[str, Any]) -> None:
    VAR.mkdir(parents=True, exist_ok=True)
    part = STATE.with_suffix(".part")
    part.write_text(json.dumps(state))
    part.replace(STATE)


# --- the commands ------------------------------------------------------------------------


def ready(settings: dict[str, str]) -> str | None:
    """Why backups can't run (None: they can)."""
    if not PASSWORD.is_file() or not PASSWORD.read_text().strip():
        return f"no restic password in {PASSWORD}: sudo infra/host/bootstrap.sh backup"
    if settings["BACKUP_REPOSITORY"].startswith("rclone:") and not RCLONE.is_file():
        return f"no rclone settings in {RCLONE}: docs/phase10.md, step 3"
    return None


def backup() -> int:
    settings = config()
    state = load()
    now = time.time()
    state["last_try"] = now
    if why := ready(settings):
        state["error"] = why
        save(state)
        print(f"backup: {why}", file=sys.stderr)
        return 1
    keep_days = max(1, min(30, int(settings["BACKUP_KEEP_DAYS"] or 7)))
    STAGE.mkdir(mode=0o700, parents=True, exist_ok=True)
    errors = []
    if error := brain_store(keep_days):
        errors.append(error)
    errors += images(settings["BACKUP_IMAGES"].split())
    done = restic(settings, "backup", "--json", "--tag", TAG, "--host", TAG, *excludes(), *paths())
    # Exit 3: some files couldn't be read, the backup was still made (restic's own word).
    if done.returncode not in (0, 3):
        errors.append(f"restic backup: {last_line(done)}")
    else:
        result = summary(done.stdout)
        state["snapshot"] = str(result.get("snapshot_id", ""))[:8]
        state["added"] = result.get("data_added")
        if done.returncode == 3:
            errors.append("restic backup: some files couldn't be read (journalctl -u chat-uses-backup)")
        forgot = restic(
            settings, "forget", "--tag", TAG, "--host", TAG, "--group-by", "host,tags",
            "--keep-within", f"{keep_days}d", "--prune",
        )  # fmt: skip
        if forgot.returncode != 0:
            errors.append(f"restic forget: {last_line(forgot)}")
    if done.returncode in (0, 3):
        state["last_ok"] = now  # a snapshot was made, even with a part missing
    state["error"] = "; ".join(errors) or None
    save(state)
    for error in errors:
        print(f"backup: {error}", file=sys.stderr)
    print(
        f"backup: {'done' if not errors else 'done, with problems'} (snapshot {state.get('snapshot') or '?'})"
    )
    return 0 if not errors else 1


def init() -> int:
    settings = config()
    if not PASSWORD.is_file():
        print(
            f"backup: no restic password in {PASSWORD}: sudo infra/host/bootstrap.sh backup", file=sys.stderr
        )
        return 1
    there = restic(settings, "cat", "config", timeout=120)
    if there.returncode == 0:
        print(f"backup: the repository {settings['BACKUP_REPOSITORY']} is there already")
        return 0
    made = restic(settings, "init", timeout=300)
    if made.returncode != 0:
        print(f"backup: can't make the repository: {last_line(made)}", file=sys.stderr)
        return 1
    print(f"backup: made the repository {settings['BACKUP_REPOSITORY']}")
    return 0


def check() -> int:
    settings = config()
    state = load()
    if why := ready(settings):
        print(f"backup: {why}")
        return 1
    listed = restic(settings, "snapshots", "--json", "--tag", TAG, timeout=300)
    if listed.returncode != 0:
        print(f"backup: can't read the repository: {last_line(listed)}")
        return 1
    try:
        snapshots = json.loads(listed.stdout) or []
    except ValueError:
        snapshots = []
    print(f"backup: {len(snapshots)} backups in {settings['BACKUP_REPOSITORY']}")
    for snap in snapshots[-10:]:
        when = str(snap.get("time", "?"))[:16].replace("T", " ")
        print(f"backup:   {snap.get('short_id', '?')}  {when}")
    stats = restic(settings, "stats", "--json", "--mode", "raw-data", timeout=600)
    try:
        size = json.loads(stats.stdout).get("total_size")
        print(f"backup: {size / 1e9:.2f} GB stored (compressed, encrypted)")
    except (ValueError, AttributeError, TypeError):
        pass
    if last := state.get("last_ok"):
        print(f"backup: the last one worked {(time.time() - last) / 3600:.0f} h ago")
    if state.get("error"):
        print(f"backup: the last try said: {state['error']}")
    drill = state.get("drill")
    last_drill = time.strftime("%Y-%m-%d", time.localtime(drill)) if drill else "never"
    print(f"backup: last restore drill: {last_drill}")
    return 0


def drill() -> int:
    """The latest backup restored to a temporary folder and checked, as a real restore would need it."""
    settings = config()
    if why := ready(settings):
        print(f"drill: {why}", file=sys.stderr)
        return 1
    VAR.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="drill-", dir=VAR) as tmp:
        started = time.time()
        restored = restic(settings, "restore", "latest", "--tag", TAG, "--host", TAG, "--target", tmp)
        root = Path(tmp)
        if restored.returncode != 0:
            problems.append(f"restore: {last_line(restored)}")
        else:
            took = time.time() - started
            size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
            print(f"drill: restored {size / 1e9:.2f} GB in {took / 60:.0f} min")
            problems += _check_restore(root)
    state = load()
    state["drill"], state["drill_ok"] = time.time(), not problems
    state["drill_problems"] = problems
    save(state)
    for problem in problems:
        print(f"drill: ✗ {problem}")
    print("drill: all good: this backup would rebuild the server" if not problems else "drill: FAILED")
    return 0 if not problems else 1


def _check_restore(root: Path) -> list[str]:
    problems: list[str] = []
    store = root / STAGE.relative_to("/") / "brain.db"
    if not store.is_file():
        problems.append("brain's store isn't in it")
    elif wrong := integrity(store):
        problems.append(f"brain's store is {wrong}")
    else:
        db = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        try:
            counts = {
                t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("users", "runs", "events")
            }
        finally:
            db.close()
        print(f"drill: ✓ brain's store: {', '.join(f'{n} {t}' for t, n in counts.items())}")
    secrets = root / ETC.relative_to("/") / "secrets"
    found = sorted(p.name for p in secrets.iterdir()) if secrets.is_dir() else []
    if (ETC / "secrets").is_dir() and not found:
        problems.append("the secrets aren't in it")
    elif found:
        print(f"drill: ✓ secrets: {', '.join(found)}")
    if PASSWORD.name in found:
        problems.append("restic's own password is in it (it must stay out)")
    for folder in sorted((root / STAGE.relative_to("/") / "images").glob("*")):
        files = [f for f in folder.iterdir() if f.name != ".done"]
        if wrong := [f.name for f in files if not _readable_tar(f)] or ([] if files else ["no file"]):
            problems.append(f"image {folder.name}: unreadable ({', '.join(wrong)})")
        else:
            print(f"drill: ✓ image {folder.name}: {len(files)} file(s), readable")
    pages = list((root / SITE.relative_to("/")).rglob("*.html")) if SITE.exists() else []
    if pages:
        problems.append(f"{len(pages)} site pages are in it (they name YouTube viewers)")
    return problems


def _readable_tar(path: Path) -> bool:
    """Every member of the image's tarball can be read through."""
    try:
        with tarfile.open(path) as tar:
            for _member in tar:
                pass
    except (tarfile.TarError, OSError, EOFError):
        return False
    return True


def main(argv: list[str]) -> int:
    match argv:
        case []:
            return backup()
        case ["--init"]:
            return init()
        case ["--check"]:
            return check()
        case ["--drill"]:
            return drill()
        case _:
            print(__doc__, file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
