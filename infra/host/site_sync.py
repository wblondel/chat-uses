#!/usr/bin/env python3
"""Copy the public site brain renders onto the host, for Caddy (PLAN.md Phase 8, D65).

    sudo infra/host/site_sync.py            sync once (the chat-uses-site-sync timer: every minute)
    sudo infra/host/site_sync.py --check    how fresh the host's copy is; changes nothing

brain renders the site into /var/lib/chat-brain/site and writes manifest.json
last: every file's size and SHA-256 (control/src/chatuses/site.py). This reads
the manifest through `incus exec`, fetches the files that differ from the
host's copy as one tar stream, and puts each in /var/www/chat-uses, atomically,
only if it matches the manifest. Files the manifest no longer lists are deleted:
that's how a deletion request reaches the site.

brain is ours but reads hostile input all day, so nothing it sends is trusted
further than this: a manifest with a path that isn't a plain name under the
site (a few extensions, no dot files, no ..) is refused whole; only regular
files that were asked for are taken from the tar (no links, no devices); sizes
are bounded; a file that doesn't match its manifest entry waits for the next
round. Caddy serves the copy read-only, as its own user. Standard library only.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO, Any

WWW = Path(os.environ.get("CHAT_USES_SITE", "/var/www/chat-uses"))
STATE = Path(os.environ.get("CHAT_USES_VAR", "/var/lib/chat-uses")) / "site-sync.json"
BRAIN, PROJECT = "brain", "infra"
REMOTE = "/var/lib/chat-brain/site"
MAX_MANIFEST = 8 * 1024 * 1024  # bytes
MAX_FILE = 8 * 1024 * 1024  # bytes, one file
MAX_TOTAL = 4 * 1024 * 1024 * 1024  # bytes, the whole site
MAX_FILES = 200_000
BATCH = 400  # paths per tar
STALE = 15 * 60  # seconds: --check says the copy is stale after this
PATH = re.compile(
    r"(?:[a-z0-9][a-z0-9_-]{0,63}/){0,3}[a-z0-9][a-z0-9_.-]{0,99}\.(?:html|css|png|txt|xml|svg|ico)"
)
SHA256 = re.compile(r"[0-9a-f]{64}")

Exec = Callable[[list[str]], AbstractContextManager[IO[bytes]]]


class SyncError(Exception):
    pass


@contextmanager
def incus_exec(command: list[str]) -> Iterator[IO[bytes]]:
    """`command` in brain (2 minutes at most), its output as a stream; a failure raises SyncError."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(
            ["incus", "exec", BRAIN, "--project", PROJECT, "--", "timeout", "120", *command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errors,
        )
        assert proc.stdout is not None
        try:
            yield proc.stdout
        finally:
            proc.stdout.close()
            try:
                proc.wait(timeout=150)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            errors.seek(0)
            said = errors.read(400).decode(errors="replace").strip()
            raise SyncError(f"{command[0]} in brain: {said or f'exit status {proc.returncode}'}")


def read_manifest(run: Exec) -> tuple[float, dict[str, dict[str, Any]]]:
    """(when brain rendered it, path -> {"size", "sha256"}), every path checked."""
    with run(["cat", "--", f"{REMOTE}/manifest.json"]) as out:
        raw = out.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise SyncError("manifest.json is too large")
    try:
        data = json.loads(raw)
    except ValueError as err:
        raise SyncError(f"manifest.json isn't JSON: {err}") from err
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise SyncError("manifest.json: not a version 1 manifest")
    files: dict[str, dict[str, Any]] = data["files"]
    if len(files) > MAX_FILES:
        raise SyncError(f"manifest.json lists more than {MAX_FILES} files")
    total = 0
    for path, entry in files.items():
        if not PATH.fullmatch(path):
            raise SyncError(f"manifest.json: refused path {path[:120]!r}")
        if not isinstance(entry, dict) or set(entry) != {"size", "sha256"}:
            raise SyncError(f"manifest.json: bad entry for {path}")
        size, digest = entry["size"], entry["sha256"]
        if type(size) is not int or not 0 <= size <= MAX_FILE:
            raise SyncError(f"manifest.json: {path} is too large or has no size")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise SyncError(f"manifest.json: {path} has no SHA-256")
        total += size
    if total > MAX_TOTAL:
        raise SyncError("manifest.json: the site is too large")
    generated = data.get("generated")
    return (float(generated) if isinstance(generated, (int, float)) else 0.0), files


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
        if isinstance(state, dict) and isinstance(state.get("files"), dict):
            return state
    except (OSError, ValueError):
        pass
    return {"files": {}}


def _write(target: Path, data: bytes) -> None:
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".sync-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def sync(run: Exec, www: Path = WWW, state_file: Path = STATE) -> dict[str, Any]:
    """One round: what was fetched, removed, and what failed."""
    generated, wanted = read_manifest(run)
    state = load_state(state_file)
    have: dict[str, str] = {p: d for p, d in state["files"].items() if p in wanted}
    todo = sorted(p for p, e in wanted.items() if have.get(p) != e["sha256"] or not _plain_file(www / p))
    fetched, problems = 0, list[str]()
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        asked = set(batch)
        try:
            command = ["tar", "-C", REMOTE, "-cf", "-", "--", *batch]
            with run(command) as stream, tarfile.open(fileobj=stream, mode="r|") as tar:
                for member in tar:
                    name = member.name
                    if name not in asked or not member.isreg():
                        continue  # only regular files that were asked for
                    asked.discard(name)  # once
                    entry = wanted[name]
                    source = tar.extractfile(member)
                    if member.size != entry["size"] or source is None:
                        problems.append(f"{name}: not the size the manifest says")
                        continue
                    data = source.read(entry["size"] + 1)
                    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        problems.append(f"{name}: doesn't match the manifest (changed meanwhile?)")
                        continue
                    _write(www / name, data)
                    have[name] = entry["sha256"]
                    fetched += 1
        except (SyncError, tarfile.TarError, OSError) as err:
            problems.append(str(err))
    removed = 0
    if www.is_dir():
        for path in sorted(www.rglob("*"), reverse=True):  # what's in a folder comes before it
            rel = path.relative_to(www).as_posix()
            if (
                path.is_symlink()
                or (path.is_file() and rel not in wanted)
                or not (path.is_file() or path.is_dir())
            ):
                path.unlink()
                have.pop(rel, None)
                removed += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    new_state = {"generated": generated, "synced": time.time(), "files": have}
    _write(state_file, json.dumps(new_state, sort_keys=True).encode())
    return {"files": len(wanted), "fetched": fetched, "removed": removed, "problems": problems}


def _plain_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def check(state_file: Path = STATE, now: float | None = None) -> tuple[bool, str]:
    """Is the host's copy fresh? (for server-check.sh)"""
    now = time.time() if now is None else now
    state = load_state(state_file)
    synced = state.get("synced")
    if not isinstance(synced, (int, float)):
        return False, "the site was never copied from brain (is the chat-uses-site-sync timer on?)"
    files, generated = len(state["files"]), state.get("generated") or 0
    age = now - synced
    text = f"{files} files, copied {round(age)} s ago, rendered by brain {round(now - generated)} s ago"
    if age > STALE:
        return False, f"stale: {text} (journalctl -u chat-uses-site-sync)"
    return True, text


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        ok, text = check()
        print(f"site: {text}")
        return 0 if ok else 1
    if argv:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        result = sync(incus_exec)
    except (SyncError, OSError) as err:
        print(f"site: not copied: {err}", file=sys.stderr)
        return 1
    if result["fetched"] or result["removed"] or result["problems"]:
        print(f"site: {result['files']} files, {result['fetched']} copied, {result['removed']} removed")
    for problem in result["problems"][:20]:
        print(f"site: {problem}", file=sys.stderr)
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
