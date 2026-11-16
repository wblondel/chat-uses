"""Chat's snapshots of the target (PLAN.md 6.6): time travel.

- **Auto snapshots** every `auto_snapshot_min` minutes (a law, 10 to 30),
  only while the target is healthy and its screen changed since the last
  one; a ring of `auto_snapshot_keep` (8).
- **Named saves** (`!save NAME`, a vote): `named_saves` slots (3). Saving a
  name again replaces it; a new name in a full set replaces the oldest.
- **Rewind targets:** `!rewind` the latest auto snapshot, `!rewind N` the
  N-th back; `!load NAME` a named save. Moderators can go to any of them by
  number (/rewind-to).
- **Thumbnails** (200 px wide) for the overlay's timeline, in thumbs/ on
  brain's data volume; deleted with their snapshot.

The Incus snapshots are named `auto-YYYYmmdd-HHMMSS` and
`save-NAME-YYYYmmdd-HHMMSS`; the store (`snapshots`) knows which are
chat's. Forensic snapshots taken on panic (`panic-...`) aren't in it: chat
can never go back to one.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from chatuses.referee.laws import Laws
from chatuses.screen import Frame
from chatuses.store import Store

log = logging.getLogger(__name__)

Kind = Literal["auto", "named"]
TIMELINE = 6  # snapshots the overlay shows


class Backend(Protocol):
    """The calls snapshots need (incus.Incus)."""

    async def snapshot(self, name: str, *, instance: str = "", address: str = "") -> str: ...
    async def restore(self, instance: str, name: str) -> None: ...
    async def delete_snapshot(self, instance: str, name: str) -> None: ...
    async def snapshots(self, instance: str) -> list[str]: ...


@dataclass(frozen=True)
class Snap:
    id: int
    kind: str
    name: str | None
    instance: str
    ref: str  # the Incus snapshot's name
    created: float
    thumb: str | None

    @property
    def label(self) -> str:
        return (
            self.name
            if self.kind == "named" and self.name
            else time.strftime("%H:%M", time.localtime(self.created))
        )


class Snapshots:
    def __init__(
        self,
        store: Store,
        backend: Backend,
        instance: Callable[[], Awaitable[str]],
        laws: Callable[[], Laws],
        thumbs: Path | None = None,
        grab: Callable[[], Awaitable[Frame | None]] | None = None,
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.backend = backend
        self.instance = instance  # the target's instance name (found by address if need be)
        self.laws = laws
        self.thumbs = thumbs
        self.grab = grab  # the screen now, for the thumbnail (None: no thumbnails)
        self.on_change = on_change  # the overlay's timeline
        self.clock = clock

    # --- what there is ---------------------------------------------------------------

    def all(self, kind: Kind | None = None) -> list[Snap]:
        return [Snap(**row) for row in self.store.snapshots(kind)]

    def auto(self, n: int = 1) -> Snap | None:
        """The n-th latest auto snapshot (1: the latest)."""
        autos = self.all("auto")
        return autos[n - 1] if 1 <= n <= len(autos) else None

    def named(self, name: str) -> Snap | None:
        return next((s for s in self.all("named") if s.name == name), None)

    def find(self, which: str) -> Snap | None:
        """A snapshot by number (as the dashboard and /status show them) or a save's name."""
        which = which.strip().lstrip("#")
        if which.isdigit():
            return next((s for s in self.all() if s.id == int(which)), None)
        return self.named(which.casefold())

    def latest_auto_time(self) -> float | None:
        latest = self.auto(1)
        return latest.created if latest else None

    # --- making and restoring ----------------------------------------------------------

    async def take(self, kind: Kind, name: str | None = None) -> Snap:
        """Snapshot the target now; raises what the backend raises."""
        instance = await self.instance()
        now = self.clock()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
        base = ref = f"auto-{stamp}" if kind == "auto" else f"save-{name}-{stamp}"
        taken = {s.ref for s in self.all()}
        n = 1
        while ref in taken:  # two in one second
            n += 1
            ref = f"{base}-{n}"
        await self.backend.snapshot(ref, instance=instance)
        snap_id = self.store.add_snapshot(now, kind, name, instance, ref)
        await self._thumbnail(snap_id)
        await self._prune(kind, name, snap_id)
        self.on_change()
        found = next(s for s in self.all() if s.id == snap_id)
        log.info("snapshot %s (%s) taken", found.ref, found.label)
        return found

    async def reconcile(self) -> int:
        """Forget snapshots the target no longer has (it was rebuilt, or someone deleted them);
        how many. Brain does this when it starts."""
        instance = await self.instance()
        there = set(await self.backend.snapshots(instance))
        gone = [s for s in self.all() if s.instance != instance or s.ref not in there]
        for snap in gone:
            self.store.delete_snapshot(self.clock(), snap.id)
            if self.thumbs is not None and snap.thumb:
                (self.thumbs / snap.thumb).unlink(missing_ok=True)
        if gone:
            log.info("snapshots: %d no longer on %s, forgotten", len(gone), instance)
            self.on_change()
        return len(gone)

    async def restore(self, snap: Snap) -> None:
        await self.backend.restore(snap.instance, snap.ref)
        self.store.event(self.clock(), "time-travel", {"id": snap.id, "kind": snap.kind, "label": snap.label})

    async def _thumbnail(self, snap_id: int) -> None:
        if self.thumbs is None or self.grab is None:
            return
        try:
            frame = await self.grab()
            if frame is None:
                return
            self.thumbs.mkdir(parents=True, exist_ok=True)
            path = self.thumbs / f"{snap_id}.png"
            path.write_bytes(frame.thumbnail().png())
            self.store.set_snapshot_thumb(snap_id, path.name)
        except Exception as err:  # a snapshot without a thumbnail is still a snapshot
            log.warning("no thumbnail for snapshot %d: %s", snap_id, err)

    async def _prune(self, kind: Kind, name: str | None, new_id: int) -> None:
        laws = self.laws()
        if kind == "auto":
            for old in self.all("auto")[laws.auto_snapshot_keep :]:
                await self._delete(old)
            return
        for old in self.all("named"):  # the same name again replaces it
            if old.name == name and old.id != new_id:
                await self._delete(old)
        for old in self.all("named")[laws.named_saves :]:
            await self._delete(old)

    async def _delete(self, snap: Snap) -> None:
        try:
            await self.backend.delete_snapshot(snap.instance, snap.ref)
        except Exception as err:  # already gone (or the target with it): forget it anyway
            log.warning("deleting snapshot %s: %s", snap.ref, err)
        self.store.delete_snapshot(self.clock(), snap.id)
        if self.thumbs is not None and snap.thumb:
            (self.thumbs / snap.thumb).unlink(missing_ok=True)

    # --- for the overlay and the dashboard ---------------------------------------------

    def view(self) -> dict[str, Any]:
        def entry(s: Snap) -> dict[str, Any]:
            return {
                "id": s.id,
                "kind": s.kind,
                "label": s.label,
                "created": s.created,
                "thumb": bool(s.thumb),
            }

        return {
            "timeline": [entry(s) for s in self.all()[:TIMELINE]],
            "saves": [entry(s) for s in self.all("named")],
            "slots": self.laws().named_saves,
        }
