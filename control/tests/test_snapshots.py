import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from fake_incus import FakeTarget

from chatuses.incus import IncusError
from chatuses.referee.laws import Laws
from chatuses.screen import Frame
from chatuses.snapshots import Snapshots
from chatuses.store import Store


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


class Clock:
    now = 1_790_000_000.0

    def __call__(self) -> float:
        self.now += 60  # a minute between snapshots: distinct names
        return self.now


async def desk() -> str:
    return "desk"


def manager(store: Store, target: FakeTarget, thumbs: Path | None = None, **laws: Any) -> Snapshots:
    async def grab() -> Frame:
        return Frame(1600, 900, bytes(1600 * 900 * 4))

    changes: list[None] = []
    return Snapshots(
        store, target, desk, lambda: Laws(**laws), thumbs, grab, lambda: changes.append(None), Clock()
    )


def test_auto_snapshots_keep_a_ring(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            target = FakeTarget()
            snaps = manager(store, target, tmp_path, auto_snapshot_keep=3)
            taken = [await snaps.take("auto") for _ in range(5)]
            assert [s.id for s in snaps.all("auto")] == [5, 4, 3]  # newest first
            assert target.there == [s.ref for s in taken[2:]] and target.deleted == [s.ref for s in taken[:2]]
            assert sorted(p.name for p in tmp_path.iterdir()) == ["3.png", "4.png", "5.png"]  # thumbs go too
            assert taken[0].ref.startswith("auto-") and (tmp_path / "5.png").read_bytes()[:4] == b"\x89PNG"
            assert snaps.auto(1) == snaps.all("auto")[0] and snaps.auto(3) and snaps.auto(4) is None
            assert snaps.auto(0) is None

    run(test)


def test_named_saves_replace_the_same_name_then_the_oldest() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            target = FakeTarget()
            snaps = manager(store, target, named_saves=2)
            first = await snaps.take("named", "desktop")
            await snaps.take("named", "game")
            again = await snaps.take("named", "desktop")  # the same name: replaces it
            assert [s.name for s in snaps.all("named")] == ["desktop", "game"]
            assert snaps.named("desktop") == again and first.ref in target.deleted
            await snaps.take("named", "chess")  # a full set: the oldest goes
            assert [s.name for s in snaps.all("named")] == ["chess", "desktop"]
            assert snaps.named("game") is None
            assert again.ref.startswith("save-desktop-") and again.label == "desktop"

    run(test)


def test_find_by_number_or_save_name() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            snaps = manager(store, FakeTarget())
            auto = await snaps.take("auto")
            save = await snaps.take("named", "clean")
            assert snaps.find(str(auto.id)) == auto and snaps.find(f"#{save.id}") == save
            assert snaps.find("CLEAN") == save and snaps.find("nope") is None and snaps.find("99") is None

    run(test)


def test_two_snapshots_in_one_second_get_distinct_names() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            target = FakeTarget()
            snaps = Snapshots(store, target, desk, Laws, clock=lambda: 1_790_000_000.0)
            a, b = await snaps.take("auto"), await snaps.take("auto")
            assert b.ref == f"{a.ref}-2" and target.there == [a.ref, b.ref]

    run(test)


def test_restore_and_reconcile_after_the_target_was_rebuilt(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            target = FakeTarget()
            snaps = manager(store, target, tmp_path)
            kept, lost = await snaps.take("auto"), await snaps.take("named", "lost")
            await snaps.restore(kept)
            assert target.restored == [kept.ref]
            target.there.remove(lost.ref)  # deleted behind brain's back
            assert await snaps.reconcile() == 1
            assert snaps.all() == [kept] and not (tmp_path / f"{lost.id}.png").exists()
            target.instance = "desk-rebuilt"  # a new target: none of the old snapshots are on it
            snaps.instance = lambda: asyncio.sleep(0, "desk-rebuilt")
            assert await snaps.reconcile() == 1 and snaps.all() == []
            [(_, _, kind, payload)] = store.recent_events(("time-travel",))
            assert payload == {"id": kept.id, "kind": "auto", "label": kept.label} and kind == "time-travel"

    run(test)


def test_a_failed_delete_still_forgets_and_a_failed_thumbnail_still_snapshots(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            target = FakeTarget()

            async def broken_grab() -> Frame:
                raise OSError("VNC refused")

            snaps = Snapshots(
                store, target, desk, lambda: Laws(auto_snapshot_keep=2), tmp_path, broken_grab, clock=Clock()
            )
            first = await snaps.take("auto")
            assert first.thumb is None and target.there == [first.ref]
            target.there.clear()  # its Incus snapshot vanished: deleting it will fail
            await snaps.take("auto")
            await snaps.take("auto")
            assert first not in snaps.all() and len(snaps.all()) == 2

    run(test)


def test_a_snapshot_that_fails_isnt_recorded() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            target = FakeTarget()
            target.error = IncusError("disk full")
            snaps = manager(store, target)
            with pytest.raises(IncusError, match="disk full"):
                await snaps.take("auto")
            assert snaps.all() == []

    run(test)


def test_the_overlay_view() -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            snaps = manager(store, FakeTarget())
            for _ in range(7):
                await snaps.take("auto")
            await snaps.take("named", "clean")
            view = snaps.view()
            assert len(view["timeline"]) == 6 and view["timeline"][0]["label"] == "clean"
            assert [s["label"] for s in view["saves"]] == ["clean"] and view["slots"] == 3
            assert set(view["timeline"][1]) == {"id", "kind", "label", "created", "thumb"}

    run(test)
