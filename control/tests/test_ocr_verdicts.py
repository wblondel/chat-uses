"""Screen OCR verdicts (Phase 7): moderators say whether each hit was real."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import discord
from aiohttp import test_utils

from chatuses.adapters import discord_bot
from chatuses.adapters.discord_bot import DiscordChat, OcrVerdict, verdict_view, with_verdict
from chatuses.brain import Brain
from chatuses.executor import connect_to
from chatuses.safety.screen_ocr import Hit
from chatuses.screen import Frame
from chatuses.store import Store

T = 1_800_000_000.0
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Moderator": "Mia"}


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


def hit(found: tuple[str, ...] = ("x badword",), ts: float = T) -> Hit:
    return Hit(ts, found, found, Frame(2, 1, b"\x10\x20\x30\x00" * 2))


def brain_in(store: Store, tmp_path: Path) -> Brain:
    brain = Brain(store, lambda: connect_to("127.0.0.1", 1))
    brain.discord = DiscordChat("token", 111, "control", lambda _m: None, lambda: "", brain.moderation)
    brain.ocr_mode, brain.screens_dir = "shadow", tmp_path / "screens"
    return brain


def test_a_hit_is_posted_with_its_buttons_and_moderators_mark_it(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_in(store, tmp_path)
            brain.ocr_hit(hit())
            [(hit_id, _ts, _kind, _payload)] = store.recent_events(("ocr_hit",))
            assert brain.discord is not None
            [(text, image, channel, verdict_for)] = brain.discord._alerts  # pyright: ignore[reportPrivateUsage]
            assert f"Screen OCR hit #{hit_id}" in text and "Was it real?" in text
            assert (image is not None, channel, verdict_for) == (True, "mod-alerts", hit_id)
            mod = brain.moderation
            assert mod.verdict("Mia", hit_id + 99, "real").text == f"No screen OCR hit #{hit_id + 99}."
            assert not mod.verdict("Mia", hit_id, "maybe").ok
            assert (
                mod.verdict("Mia", hit_id, "false").text == f"Screen OCR hit #{hit_id} marked a false alarm."
            )
            assert mod.verdict("Ann", hit_id, "real").ok  # another moderator corrects it: the latest counts
            assert store.ocr_verdicts()[hit_id]["verdict"] == "real"
            brain.ocr_hit(hit(ts=T + 60))
            assert store.ocr_summary() == {
                "hits": 2,
                "modes": {"shadow": 2},
                "real": 1,
                "false": 0,
                "unmarked": 1,
            }
            ok, text = await brain.admin(["ocr"])
            assert ok == "ok" and text.startswith(
                "Screen OCR: shadow. Hits in the last 90 days: 2 (2 shadow)"
            )
            assert ": 1 real, 0 false alarms, 1 not marked." in text and "real (Ann)" in text
            newest = store.recent_events(("ocr_hit",))[0][0]
            assert await brain.admin(["ocr-verdict", f"#{newest}", "false"]) == (
                "ok",
                f"Screen OCR hit #{newest} marked a false alarm.",
            )

    run(test)


def test_a_session_report_counts_the_verdicts_of_its_hits(tmp_path: Path) -> None:
    with Store(":memory:") as store:
        brain = brain_in(store, tmp_path)
        brain.ocr_hit(hit(ts=T - 600))  # before the session
        brain.ocr_hit(hit(ts=T + 60))
        brain.ocr_hit(hit(ts=T + 120))
        ids = [i for i, *_ in store.recent_events(("ocr_hit",))]
        for hit_id, verdict in zip(ids, ("real", "false", "real"), strict=True):  # newest first
            brain.moderation.verdict("Mia", hit_id, verdict)
        numbers = store.session_numbers(T, T + 600)
        assert numbers["ocr_verdicts"] == {"real": 1, "false": 1}


def interaction(user_id: int = 7, owner_id: int = 7, content: str = "👁️ Screen OCR hit #1") -> Any:
    sent: list[tuple[str, bool]] = []
    edited: list[str] = []

    async def send_message(text: str, *, ephemeral: bool = False) -> None:
        sent.append((text, ephemeral))

    async def edit_message(*, content: str, allowed_mentions: Any) -> None:
        edited.append(content)

    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Mia"),
        guild=SimpleNamespace(id=111, owner_id=owner_id),
        message=SimpleNamespace(content=content),
        response=SimpleNamespace(
            send_message=send_message, edit_message=edit_message, sent=sent, edited=edited
        ),
    )


def test_the_buttons_are_for_moderators_and_say_who_decided(tmp_path: Path) -> None:
    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_in(store, tmp_path)
            brain.ocr_hit(hit())
            [(hit_id, *_)] = store.recent_events(("ocr_hit",))
            chat = brain.discord
            assert chat is not None
            viewer = interaction(user_id=8, owner_id=7)
            await chat.verdict_pressed(viewer, hit_id, "real")
            assert (
                viewer.response.sent == [(discord_bot.NOT_A_MODERATOR, True)] and store.ocr_verdicts() == {}
            )
            owner = interaction(content=f"👁️ Screen OCR hit #{hit_id}\nVerdict: ✅ real (Ann)")
            await chat.verdict_pressed(owner, hit_id, "false")
            assert owner.response.edited == [f"👁️ Screen OCR hit #{hit_id}\nVerdict: ❎ false alarm (Mia)"]
            assert store.ocr_verdicts()[hit_id]["by"] == "Mia (dc:7) on Discord"
            gone = interaction()
            await chat.verdict_pressed(gone, 999, "real")
            assert gone.response.sent == [("No screen OCR hit #999.", True)]

    run(test)


def test_the_buttons_carry_the_hit_so_a_press_works_after_a_restart() -> None:
    async def test() -> None:
        view = verdict_view(42)
        ids = [getattr(item, "custom_id", None) for item in view.children]
        assert ids == ["chatuses:ocr:42:real", "chatuses:ocr:42:false"] and view.is_persistent()
        match = OcrVerdict.__discord_ui_compiled_template__.fullmatch("chatuses:ocr:42:false")
        assert match is not None
        button = await OcrVerdict.from_custom_id(SimpleNamespace(), SimpleNamespace(), match)  # type: ignore[arg-type]
        assert (button.hit, button.verdict) == (42, "false")
        assert OcrVerdict.__discord_ui_compiled_template__.fullmatch("chatuses:ocr:42:maybe") is None
        assert with_verdict("a\nb", "real", "Mia") == "a\nb\nVerdict: ✅ real (Mia)"

    run(test)


@asynccontextmanager
async def dashboard(tmp_path: Path) -> AsyncGenerator[tuple[Brain, test_utils.TestClient[Any, Any]]]:
    with Store(":memory:") as store:
        brain = brain_in(store, tmp_path)
        async with test_utils.TestClient(test_utils.TestServer(brain.web_app(TOKEN))) as client:
            yield brain, client


def test_the_dashboard_shows_and_takes_verdicts(tmp_path: Path) -> None:
    async def test() -> None:
        async with dashboard(tmp_path) as (brain, client):
            brain.ocr_hit(hit())
            [(hit_id, *_)] = brain.store.recent_events(("ocr_hit",))
            events = (await (await client.get("/api/events", headers=AUTH)).json())["events"]
            assert [e["verdict"] for e in events if e["type"] == "ocr_hit"] == [None]
            resp = await client.post(
                "/api/ocr-verdict", json={"hit": hit_id, "verdict": "real"}, headers=AUTH
            )
            assert await resp.json() == {"ok": True, "text": f"Screen OCR hit #{hit_id} marked real."}
            events = (await (await client.get("/api/events", headers=AUTH)).json())["events"]
            [shown] = [e for e in events if e["type"] == "ocr_hit"]
            assert (shown["verdict"]["verdict"], shown["verdict"]["by"]) == ("real", "Mia on the dashboard")
            for bad in (
                {"hit": "1", "verdict": "real"},
                {"hit": True, "verdict": "real"},
                {"hit": 1, "verdict": "?"},
            ):
                assert (await client.post("/api/ocr-verdict", json=bad, headers=AUTH)).status == 400

    run(test)


def test_a_real_button_press_reaches_the_verdict_through_discord_py(tmp_path: Path) -> None:
    """A component interaction as Discord sends it, dispatched by discord.py's own state."""

    async def test() -> None:
        with Store(":memory:") as store:
            brain = brain_in(store, tmp_path)
            brain.ocr_hit(hit())
            [(hit_id, *_)] = store.recent_events(("ocr_hit",))
            assert brain.discord is not None
            client = discord_bot._Client(brain.discord)  # pyright: ignore[reportPrivateUsage]
            brain.discord._client = client  # pyright: ignore[reportPrivateUsage]
            state = client._connection  # pyright: ignore[reportPrivateUsage]
            state.user = discord.ClientUser(
                state=state,
                data=cast(
                    Any, {"id": "99", "username": "bot", "discriminator": "0", "avatar": None, "bot": True}
                ),
            )
            client.add_dynamic_items(OcrVerdict)
            edited: list[str] = []

            async def edit_message(_self: Any, **kwargs: Any) -> None:
                edited.append(kwargs["content"])

            original = discord.InteractionResponse.edit_message
            discord.InteractionResponse.edit_message = edit_message  # type: ignore[method-assign,assignment]
            try:
                state.parse_interaction_create(press(hit_id))
                for _ in range(50):
                    await asyncio.sleep(0.01)
                    if edited:
                        break
            finally:
                discord.InteractionResponse.edit_message = original  # type: ignore[method-assign]
            assert edited and edited[0].endswith("Verdict: ❎ false alarm (Alice)")
            assert store.ocr_verdicts()[hit_id]["verdict"] == "false"
            await client.close()

    run(test)


def press(hit_id: int) -> Any:
    """Discord's INTERACTION_CREATE for a button under an alert, by the server's owner."""
    role = {"id": "111", "name": "@everyone", "permissions": "0", "position": 0, "color": 0, "hoist": False}
    return {
        "id": "1",
        "application_id": "2",
        "type": 3,
        "token": "tok",
        "version": 1,
        "attachment_size_limit": 8388608,
        "guild_id": "111",
        "guild": {"id": "111", "owner_id": "42", "roles": [role], "features": [], "locale": "en-US"},
        "channel_id": "333",
        "channel": {"id": "333", "type": 0, "name": "mod-alerts", "guild_id": "111", "position": 0},
        "member": {
            "user": {
                "id": "42",
                "username": "alice",
                "discriminator": "0",
                "avatar": None,
                "global_name": "Alice",
            },
            "roles": [],
            "joined_at": "2024-01-01T00:00:00+00:00",
            "deaf": False,
            "mute": False,
            "flags": 0,
            "permissions": "8",
        },
        "message": {
            "id": "555",
            "channel_id": "333",
            "author": {"id": "99", "username": "bot", "discriminator": "0", "avatar": None, "bot": True},
            "content": f"👁️ Screen OCR hit #{hit_id}: a listed word",
            "timestamp": "2026-11-16T20:00:00+00:00",
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "type": 0,
            "components": [  # as posted: the alert's two buttons
                {
                    "type": 1,
                    "components": [
                        {"type": 2, "style": 4, "label": "Real", "custom_id": f"chatuses:ocr:{hit_id}:real"},
                        {
                            "type": 2,
                            "style": 2,
                            "label": "False alarm",
                            "custom_id": f"chatuses:ocr:{hit_id}:false",
                        },
                    ],
                }
            ],
        },
        "data": {"custom_id": f"chatuses:ocr:{hit_id}:false", "component_type": 2},
        "app_permissions": "0",
        "locale": "en-US",
        "guild_locale": "en-US",
        "entitlements": [],
    }
