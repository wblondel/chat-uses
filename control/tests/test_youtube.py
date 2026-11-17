import asyncio
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import grpc
import grpc.aio
import pytest
from aiohttp import test_utils, web

from chatuses.adapters import stream_list_pb2 as pb
from chatuses.adapters import youtube
from chatuses.adapters.youtube import QUOTA_DAY, Quota, YouTubeChat, video_id_from
from chatuses.pipeline import ChatMessage

KEY = "test-api-key"
VIDEO = "dQw4w9WgXcQ"
TEXT = pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "MIN_CALL_GAP", 0.0)
    monkeypatch.setattr(youtube, "WAIT_FOR_LIVE", 0.05)
    monkeypatch.setattr(youtube, "BACKOFF", (0.01, 0.05))
    monkeypatch.setattr(youtube, "TOO_SOON_WAIT", 0.05)
    monkeypatch.setattr(youtube, "MAX_GAP", 0.0)  # reconnect at once (pacing has its own tests)


def iso(ts: float, z: bool = True) -> str:
    text = datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds")
    return text.replace("+00:00", "Z") if z else text


def message(
    msg_id: str,
    text: str,
    *,
    channel: str = "UCalice",
    name: str = "Alice",
    age: float = 0.0,
    kind: pb.LiveChatMessageSnippet.TypeWrapper.Type = TEXT,
    moderator: bool = False,
) -> pb.LiveChatMessage:
    return pb.LiveChatMessage(
        id=msg_id,
        snippet=pb.LiveChatMessageSnippet(
            type=kind,
            published_at=iso(time.time() - age),
            display_message=text,
            text_message_details=pb.LiveChatTextMessageDetails(message_text=text),
            author_channel_id=channel,
        ),
        author_details=pb.LiveChatMessageAuthorDetails(
            channel_id=channel, display_name=name, is_chat_moderator=moderator
        ),
    )


# One connection's script: responses to send, then how it ends.
@dataclass
class Session:
    responses: list[pb.LiveChatMessageListResponse] = field(default_factory=lambda: [])
    abort: grpc.StatusCode | None = None  # None: close normally
    detail: str = "scripted"  # the abort's message
    hang: bool = False  # keep the stream open after the responses


@dataclass
class FakeYouTube:
    sessions: list[Session]
    requests: list[pb.LiveChatMessageListRequest] = field(default_factory=lambda: [])
    keys: list[str | None] = field(default_factory=lambda: [])
    port: int = 0
    server: grpc.aio.Server | None = None

    async def start(self) -> "FakeYouTube":
        async def stream_list(
            request: pb.LiveChatMessageListRequest, context: grpc.aio.ServicerContext[Any, Any]
        ) -> AsyncIterator[pb.LiveChatMessageListResponse]:
            self.requests.append(request)
            key = dict(context.invocation_metadata() or ()).get("x-goog-api-key")
            self.keys.append(key if isinstance(key, str) else None)
            session = self.sessions.pop(0) if self.sessions else Session(hang=True)
            for response in session.responses:
                yield response
            if session.abort is not None:
                await context.abort(session.abort, session.detail)
            if session.hang:
                await asyncio.sleep(3600)

        handler = grpc.method_handlers_generic_handler(
            "youtube.api.v3.V3DataLiveChatMessageService",
            {
                "StreamList": grpc.unary_stream_rpc_method_handler(
                    stream_list,
                    request_deserializer=pb.LiveChatMessageListRequest.FromString,
                    response_serializer=pb.LiveChatMessageListResponse.SerializeToString,
                )
            },
        )
        self.server = grpc.aio.server()
        self.server.add_generic_rpc_handlers((handler,))
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        await self.server.start()
        return self

    async def stop(self) -> None:
        if self.server:
            await self.server.stop(0)

    def channel(self) -> grpc.aio.Channel:
        return grpc.aio.insecure_channel(f"127.0.0.1:{self.port}")


def videos_api(replies: list[tuple[int, dict[str, Any]]], seen: list[dict[str, str]]) -> web.Application:
    async def videos(request: web.Request) -> web.Response:
        seen.append(dict(request.query))
        status, body = replies.pop(0) if len(replies) > 1 else replies[0]
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/videos", videos)
    return app


LIVE = (200, {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-1"}}]})


async def until(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def adapter(fake: FakeYouTube, api: str, got: list[ChatMessage]) -> YouTubeChat:
    return YouTubeChat(KEY, got.append, api=api, channel=fake.channel)


# --- tests ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (VIDEO, VIDEO),
        (f"https://www.youtube.com/watch?v={VIDEO}&t=12", VIDEO),
        (f"https://youtu.be/{VIDEO}?si=abc", VIDEO),
        (f"https://www.youtube.com/live/{VIDEO}?feature=share", VIDEO),
        (f"https://studio.youtube.com/video/{VIDEO}/livestreaming", VIDEO),
        (f"youtube.com/watch?v={VIDEO}", VIDEO),
        (f"https://m.youtube.com/watch?v={VIDEO}", VIDEO),
        ("https://www.youtube.com/@channel", None),
        ("https://evil.example/watch?v=dQw4w9WgXcQ", None),
        ("too-short", None),
        ("", None),
    ],
)
def test_video_ids_from_ids_and_urls(text: str, expected: str | None) -> None:
    assert video_id_from(text) == expected


def test_chat_is_streamed_resumed_and_ends() -> None:
    async def test() -> None:
        fake = await FakeYouTube(
            [
                Session(
                    [
                        pb.LiveChatMessageListResponse(
                            items=[
                                message("old", "!click A1", age=600),  # history from before we connected
                                message("m1", "!type hello", moderator=True),
                            ],
                            next_page_token="t1",
                        )
                    ]
                ),  # then the server closes the stream normally
                Session(
                    [
                        pb.LiveChatMessageListResponse(
                            items=[
                                message("m1", "!type hello"),  # overlap: a duplicate
                                message(
                                    "sc", "$5!", kind=pb.LiveChatMessageSnippet.TypeWrapper.SUPER_CHAT_EVENT
                                ),
                                message("m2", "!enter", channel="UCbob", name="Bob"),
                            ],
                            next_page_token="t2",
                        )
                    ],
                    abort=grpc.StatusCode.FAILED_PRECONDITION,  # LIVE_CHAT_ENDED
                ),
            ]
        ).start()
        seen: list[dict[str, str]] = []
        got: list[ChatMessage] = []
        async with test_utils.TestServer(videos_api([LIVE], seen)) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), got)
            chat.follow(VIDEO)
            await until(lambda: chat.state == "ended")
        assert [(m.user_id, m.display_name, m.text) for m in got] == [
            ("yt:UCalice", "Alice", "!type hello"),
            ("yt:UCbob", "Bob", "!enter"),
        ]
        assert got[0].roles == frozenset({"moderator"})
        assert seen == [{"part": "liveStreamingDetails", "id": VIDEO, "key": KEY}]
        assert [r.page_token for r in fake.requests] == ["", "t1"]
        assert all(r.live_chat_id == "chat-1" for r in fake.requests)
        assert list(fake.requests[0].part) == ["id", "snippet", "authorDetails"]
        assert fake.keys == [KEY, KEY]
        status = chat.status()
        assert (status["messages"], status["stale"], status["calls"]) == (2, 1, 2)
        assert status["quota_used_today"] == 1 + 5 + 5
        assert status["average_stream_s"] == 0  # both closed at once
        assert (status["calls_last_hour"], status["open_s"]) == (2, None)
        assert "ended" in status["reason"]
        await fake.stop()

    run(test)


def test_waits_for_the_stream_to_go_live() -> None:
    async def test() -> None:
        fake = await FakeYouTube([]).start()
        scheduled = (
            200,
            {"items": [{"liveStreamingDetails": {"scheduledStartTime": "2026-11-16T20:00:00Z"}}]},
        )
        seen: list[dict[str, str]] = []
        got: list[ChatMessage] = []
        async with test_utils.TestServer(videos_api([scheduled, scheduled, LIVE], seen)) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), got)
            chat.follow(VIDEO)
            await until(lambda: chat.state == "waiting")
            await until(lambda: chat.state == "streaming" and len(fake.requests) == 1)
            assert len(seen) == 3
            await chat.close()
        await fake.stop()

    run(test)


STOPPING: list[tuple[tuple[int, dict[str, Any]], str, str]] = [
    ((200, {"items": []}), "stopped", "no such video"),
    ((200, {"items": [{}]}), "stopped", "not a live stream"),
    (
        (200, {"items": [{"liveStreamingDetails": {"actualEndTime": "2026-11-15T20:00:00Z"}}]}),
        "ended",
        "ended",
    ),
    (
        (400, {"error": {"errors": [{"reason": "badRequest"}], "message": "API key not valid"}}),
        "stopped",
        "400",
    ),
]


@pytest.mark.parametrize(("reply", "state", "reason"), STOPPING)
def test_videos_list_answers_that_stop_the_adapter(
    reply: tuple[int, dict[str, Any]], state: str, reason: str
) -> None:
    async def test() -> None:
        fake = await FakeYouTube([]).start()
        async with test_utils.TestServer(videos_api([reply], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), [])
            chat.follow(VIDEO)
            await until(lambda: chat.state in ("stopped", "ended"))
            assert chat.state == state and reason in chat.reason
            assert fake.requests == []  # never opened a stream
        await fake.stop()

    run(test)


@pytest.mark.parametrize(
    ("code", "state", "reason"),
    [
        (grpc.StatusCode.PERMISSION_DENIED, "stopped", "refused the API key"),
        (grpc.StatusCode.NOT_FOUND, "ended", "ended"),
        (grpc.StatusCode.INVALID_ARGUMENT, "stopped", "rejected"),
    ],
)
def test_stream_errors_that_stop(code: grpc.StatusCode, state: str, reason: str) -> None:
    async def test() -> None:
        fake = await FakeYouTube([Session(abort=code)]).start()
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), [])
            chat.follow(VIDEO)
            await until(lambda: chat.state in ("stopped", "ended"))
            assert chat.state == state and reason in chat.reason
            assert len(fake.requests) == 1
        await fake.stop()

    run(test)


def test_transient_errors_reconnect_and_resume() -> None:
    async def test() -> None:
        first = pb.LiveChatMessageListResponse(items=[message("m1", "!enter")], next_page_token="t1")
        fake = await FakeYouTube(
            [
                Session([first], abort=grpc.StatusCode.UNAVAILABLE),
                Session(abort=grpc.StatusCode.INTERNAL),
                Session(
                    [pb.LiveChatMessageListResponse(items=[message("m2", "!tab")], next_page_token="t2")]
                ),
            ]
        ).start()
        got: list[ChatMessage] = []
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), got)
            chat.follow(VIDEO)
            await until(lambda: len(got) == 2 and len(fake.requests) >= 4)
            assert [r.page_token for r in fake.requests[:4]] == ["", "t1", "t1", "t2"]
            assert [m.text for m in got] == ["!enter", "!tab"]
            assert any("UNAVAILABLE" in e for e in chat.status()["recent_errors"])
            await chat.close()
        await fake.stop()

    run(test)


def test_backlog_after_an_outage_is_dropped() -> None:
    async def test() -> None:
        fake = await FakeYouTube(
            [
                Session(
                    [
                        pb.LiveChatMessageListResponse(
                            items=[message("late", "!click A1", age=45), message("now", "!enter", age=2)],
                            next_page_token="t1",
                        )
                    ],
                    hang=True,
                )
            ]
        ).start()
        got: list[ChatMessage] = []
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), got)
            chat.follow(VIDEO)
            await until(lambda: chat.counters.stale + chat.counters.messages == 2)
            assert [m.text for m in got] == ["!enter"]
            await chat.close()
        await fake.stop()

    run(test)


def test_offline_stream_ends_the_adapter() -> None:
    async def test() -> None:
        fake = await FakeYouTube(
            [Session([pb.LiveChatMessageListResponse(offline_at=iso(time.time()))])]
        ).start()
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), [])
            chat.follow(VIDEO)
            await until(lambda: chat.state == "ended")
            assert "offline" in chat.reason
        await fake.stop()

    run(test)


def test_the_quota_guard_pauses_before_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "QUOTA_STOP", 12)  # videos.list 1 + two streamList calls of 5

    async def test() -> None:
        fake = await FakeYouTube([Session(), Session(), Session()]).start()  # each closes at once
        told: list[str] = []
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            url = str(api.make_url("")).rstrip("/")
            chat = YouTubeChat(KEY, lambda _m: None, api=url, channel=fake.channel, on_quota=told.append)
            chat.follow(VIDEO)
            await until(lambda: chat.state == "waiting")
            assert "nearly used (11 units)" in chat.reason and "midnight Pacific" in chat.reason
            assert len(fake.requests) == 2 and len(told) == 1 and "goes on by itself" in told[0]
            await chat.close()
        await fake.stop()

    run(test)


def midnight_pacific(day: str) -> float:
    return datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=QUOTA_DAY).timestamp()


class Clock:
    """A clock that runs from a given time."""

    def __init__(self, start: float) -> None:
        self.base, self.started = start, time.monotonic()

    def __call__(self) -> float:
        return self.base + time.monotonic() - self.started


def test_google_s_quota_refusal_pauses_until_midnight_pacific_then_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(youtube, "QUOTA_RESUME_AFTER", 0.05)
    exceeded = (403, {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}})

    async def test() -> None:
        fake = await FakeYouTube([Session(hang=True)]).start()
        saved: list[str] = []
        clock = Clock(midnight_pacific("2026-11-17") - 0.3)  # 23:59:59.7 Pacific
        quota = Quota(clock, load=lambda: "2026-11-16 120", save=saved.append)
        told: list[str] = []
        seen: list[dict[str, str]] = []
        async with test_utils.TestServer(videos_api([exceeded, LIVE], seen)) as api:
            url = str(api.make_url("")).rstrip("/")
            chat = YouTubeChat(
                KEY,
                lambda _m: None,
                api=url,
                channel=fake.channel,
                clock=clock,
                quota=quota,
                on_quota=told.append,
            )
            assert chat.status()["quota_used_today"] == 120  # kept from before the restart
            chat.follow(VIDEO)
            await until(lambda: chat.state == "waiting")
            assert "YouTube says the daily quota is used up (quotaExceeded)" in chat.reason
            assert "08:00 UTC" in chat.reason  # midnight Pacific in November
            assert saved[-1] == "2026-11-16 10000" and len(told) == 1
            await until(lambda: chat.state == "streaming")  # after midnight: a new day, a new quota
            assert len(seen) == 2 and saved[-1] == "2026-11-17 6" and len(told) == 1
            await chat.close()
        await fake.stop()

    run(test)


def test_a_restart_on_a_used_up_day_asks_youtube_nothing() -> None:
    async def test() -> None:
        seen: list[dict[str, str]] = []
        clock = Clock(midnight_pacific("2026-11-16") + 3600)
        quota = Quota(clock, load=lambda: "2026-11-16 10000")
        async with test_utils.TestServer(videos_api([LIVE], seen)) as api:
            chat = YouTubeChat(
                KEY, lambda _m: None, api=str(api.make_url("")).rstrip("/"), clock=clock, quota=quota
            )
            chat.follow(VIDEO)
            await until(lambda: chat.state == "waiting")
            assert seen == [] and "the daily quota is used up (10000 units)" in chat.reason
            await chat.close()

    run(test)


def test_a_stream_refused_for_quota_pauses_but_one_sent_too_soon_retries() -> None:
    async def test() -> None:
        fake = await FakeYouTube(
            [
                Session(abort=grpc.StatusCode.RESOURCE_EXHAUSTED, detail="Request sent too soon"),
                Session(
                    abort=grpc.StatusCode.RESOURCE_EXHAUSTED,
                    detail="Quota exceeded for quota metric 'Queries' of service youtube.googleapis.com",
                ),
            ]
        ).start()
        saved: list[str] = []
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            url = str(api.make_url("")).rstrip("/")
            quota = Quota(save=saved.append)
            chat = YouTubeChat(KEY, lambda _m: None, api=url, channel=fake.channel, quota=quota)
            chat.follow(VIDEO)
            await until(lambda: chat.state == "waiting")
            assert len(fake.requests) == 2  # retried once after "too soon", paused on the quota
            assert "daily quota is used up (Quota exceeded" in chat.reason
            assert saved[-1].endswith(" 10000")
            await chat.close()
        await fake.stop()

    run(test)


def test_quota_days_roll_over_at_midnight_pacific() -> None:
    clock = Clock(midnight_pacific("2026-11-16") - 1)
    saved: list[str] = []
    quota = Quota(clock, load=lambda: "garbage", save=saved.append)
    assert quota.spent() == 0 and quota.today() == "2026-11-15"
    quota.spend(5)
    assert saved == ["2026-11-15 5"]
    assert quota.resets_at() == midnight_pacific("2026-11-16")
    clock.base += 2
    assert quota.spent() == 0 and quota.today() == "2026-11-16"
    quota.used_up()
    assert saved[-1] == "2026-11-16 10000"


def test_following_another_video_replaces_the_first() -> None:
    async def test() -> None:
        fake = await FakeYouTube([Session(hang=True), Session(hang=True)]).start()
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), [])
            chat.follow(VIDEO)
            await until(lambda: len(fake.requests) == 1)
            chat.follow("abcdefghijk")
            await until(lambda: len(fake.requests) == 2)
            assert chat.video_id == "abcdefghijk"
            chat.follow(None)
            assert chat.state == "off"
            await chat.close()
        await fake.stop()

    run(test)


def test_published_times_with_offsets_are_understood() -> None:
    got: list[ChatMessage] = []
    chat = YouTubeChat(KEY, got.append)
    item = message("x", "!enter", age=60)
    item.snippet.published_at = iso(time.time() - 60, z=False)  # +00:00 instead of Z
    chat._item(item)  # pyright: ignore[reportPrivateUsage]
    assert got == [] and chat.counters.stale == 1


def test_a_google_server_error_is_retried() -> None:
    async def test() -> None:
        fake = await FakeYouTube([Session(hang=True)]).start()
        replies: list[tuple[int, dict[str, Any]]] = [(503, {"error": {"message": "backend error"}}), LIVE]
        async with test_utils.TestServer(videos_api(replies, [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), [])
            chat.follow(VIDEO)
            await until(lambda: chat.state == "streaming" and len(fake.requests) == 1)
            assert any("503" in e for e in chat.status()["recent_errors"])
            await chat.close()
        await fake.stop()

    run(test)


def test_viewers_are_asked_of_youtube_while_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    watching = (
        200,
        {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-1", "concurrentViewers": "42"}}]},
    )
    hidden = (200, {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-1"}}]})

    async def test() -> None:
        fake = await FakeYouTube([Session(hang=True)]).start()
        seen: list[dict[str, str]] = []
        async with test_utils.TestServer(videos_api([watching, watching, hidden], seen)) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), [])
            assert await chat.viewers() is None  # no video followed: YouTube isn't asked
            chat.follow(VIDEO)
            await until(lambda: chat.state == "streaming")
            assert await chat.viewers() == 42
            assert await chat.viewers() is None  # the channel hides its count
            assert len(seen) == 3 and chat.status()["quota_used_today"] == 1 + 5 + 1 + 1
            monkeypatch.setattr(youtube, "QUOTA_STOP", 8)
            assert await chat.viewers() is None and len(seen) == 3  # no quota left for it
            await chat.close()
        await fake.stop()

    run(test)


# --- spreading the day's quota --------------------------------------------------------------

EVEN = youtube.COST_STREAM_LIST / ((youtube.QUOTA_STOP - youtube.QUOTA_BURST) / 86_400)  # 50.5 s


def pacific(at: str) -> float:
    return datetime.fromisoformat(at).replace(tzinfo=QUOTA_DAY).timestamp()


def pacer(at: str, spent: int, chatted_ago: float | None = None) -> YouTubeChat:
    """An adapter at a Pacific time of day, with that much quota spent."""
    now = pacific(at)
    quota = Quota(lambda: now, load=lambda: f"{datetime.fromtimestamp(now, QUOTA_DAY).date()} {spent}")
    chat = YouTubeChat(KEY, lambda _m: None, clock=lambda: now, quota=quota)
    if chatted_ago is not None:
        chat._last_message = now - chatted_ago  # pyright: ignore[reportPrivateUsage]
    return chat


def pace(chat: YouTubeChat, opened: float = 10.0) -> float:
    return chat._pace(opened)  # pyright: ignore[reportPrivateUsage]


def test_the_day_s_quota_builds_up_and_busy_chat_spends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "MAX_GAP", 90.0)
    # Midnight Pacific: the burst is there, chat that plays doesn't wait.
    assert pace(pacer("2026-11-17T00:00:00", 0, chatted_ago=60)) == 0.0
    # A quiet chat waits 1.5 times the even interval, less the time YouTube kept it open.
    assert pace(pacer("2026-11-17T00:00:00", 0)) == pytest.approx(1.5 * EVEN - 10)
    assert pace(pacer("2026-11-17T00:00:00", 0, chatted_ago=600)) == pytest.approx(1.5 * EVEN - 10)
    # The burst spent: one call's worth comes every EVEN seconds.
    assert pace(pacer("2026-11-17T00:00:00", youtube.QUOTA_BURST, chatted_ago=1)) == pytest.approx(EVEN)
    # Noon, less than half spent: what the quiet morning left is there for chat now.
    assert pace(pacer("2026-11-17T12:00:00", 4000, chatted_ago=1)) == 0.0
    # Noon, more spent than the allowance (an older brain spent it): wait for it to catch up.
    behind = pace(pacer("2026-11-17T12:00:00", 6000, chatted_ago=1))
    assert behind == pytest.approx((6005 - youtube.QUOTA_BURST) * EVEN / 5 - 12 * 3600)
    # Nothing left: no wait here, the next call pauses until the reset.
    assert pace(pacer("2026-11-17T12:00:00", youtube.QUOTA_STOP)) == 0.0


def test_a_quiet_chat_waits_no_longer_than_max_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "MAX_GAP", 30.0)
    assert pace(pacer("2026-11-17T00:00:00", 0)) == 30.0


@pytest.mark.parametrize(
    "busy_hours", [set[int](), set(range(24)), {h for h in range(24) if h % 3 == 0}, set(range(16, 22))]
)
def test_a_whole_day_never_runs_out(monkeypatch: pytest.MonkeyPatch, busy_hours: set[int]) -> None:
    monkeypatch.setattr(youtube, "MAX_GAP", 90.0)
    now = [pacific("2026-11-17T00:00:00")]
    end = pacific("2026-11-18T00:00:00")
    quota = Quota(lambda: now[0])
    chat = YouTubeChat(KEY, lambda _m: None, clock=lambda: now[0], quota=quota)
    calls = 0
    while True:
        now[0] += pace(chat, 10)
        if now[0] >= end:
            break
        quota.spend(youtube.COST_STREAM_LIST)  # QuotaOut would fail the test
        calls += 1
        hour = datetime.fromtimestamp(now[0], QUOTA_DAY).hour
        chat._last_message = now[0] if hour in busy_hours else 0.0  # pyright: ignore[reportPrivateUsage]
        now[0] += 10  # YouTube closes it after 10 s
    assert quota.spent() <= youtube.QUOTA_STOP
    assert calls >= 1100  # chat was read all day: a call every 80 s at worst


def test_daylight_saving_days_have_their_own_length() -> None:
    spring = Quota(lambda: pacific("2026-03-08T12:00:00"))  # 23 hours
    autumn = Quota(lambda: pacific("2026-11-01T12:00:00"))  # 25 hours
    assert spring.rate() == pytest.approx((youtube.QUOTA_STOP - youtube.QUOTA_BURST) / (23 * 3600))
    assert autumn.rate() == pytest.approx((youtube.QUOTA_STOP - youtube.QUOTA_BURST) / (25 * 3600))
    assert spring.ready_at(youtube.QUOTA_STOP) == pytest.approx(spring.resets_at())


def two_seconds(_opened: float) -> float:
    return 2.0


def test_what_was_said_during_the_wait_is_not_dropped_as_late(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "MAX_AGE", 2.0)
    monkeypatch.setattr(youtube, "MAX_GAP", 5.0)

    async def test() -> None:
        fake = await FakeYouTube(
            [
                Session(
                    [
                        pb.LiveChatMessageListResponse(
                            items=[message("history", "!click A1", age=2.5)], next_page_token="t1"
                        )
                    ]
                ),  # closed normally: the adapter waits 2 s before the next call
                Session(
                    [
                        pb.LiveChatMessageListResponse(
                            items=[
                                message("waited", "!enter", age=1.5),  # about 3.5 s old when it comes
                                message("ancient", "!type x", age=30),
                            ],
                            next_page_token="t2",
                        )
                    ],
                    hang=True,
                ),
            ]
        ).start()
        got: list[ChatMessage] = []
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), got)
            monkeypatch.setattr(chat, "_pace", two_seconds)
            chat.follow(VIDEO)
            await until(lambda: chat.status()["next_s"] is not None)
            assert chat.status()["next_s"] in (1, 2)
            await until(lambda: chat.counters.stale + chat.counters.messages == 3, timeout=8)
            assert [m.text for m in got] == ["!enter"]  # the history before we came is still too old
            assert chat.status()["next_s"] is None and len(fake.requests) == 2
            await chat.close()
        await fake.stop()

    run(test)


def test_after_an_error_a_backlog_is_late_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "MAX_AGE", 1.0)
    monkeypatch.setattr(youtube, "MAX_GAP", 5.0)

    async def test() -> None:
        fake = await FakeYouTube(
            [
                Session([pb.LiveChatMessageListResponse(next_page_token="t1")]),  # closed normally
                Session(abort=grpc.StatusCode.UNAVAILABLE),  # after the 4 s wait
                Session(
                    [
                        pb.LiveChatMessageListResponse(
                            items=[message("late", "!enter")],  # about 4 s old when it comes: within
                            next_page_token="t2",  # MAX_AGE + the wait, but the wait ended in an error
                        )
                    ],
                    hang=True,
                ),
            ]
        ).start()
        got: list[ChatMessage] = []
        async with test_utils.TestServer(videos_api([LIVE], [])) as api:
            chat = adapter(fake, str(api.make_url("")).rstrip("/"), got)
            monkeypatch.setattr(chat, "_pace", four_seconds)
            chat.follow(VIDEO)
            await until(lambda: chat.counters.stale + chat.counters.messages == 1, timeout=8)
            assert got == [] and chat.counters.stale == 1
            await chat.close()
        await fake.stop()

    run(test)


def four_seconds(_opened: float) -> float:
    return 4.0


def test_a_bigger_quota_from_google_scales_the_stop_and_the_pace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "MAX_GAP", 90.0)
    now = [pacific("2026-11-17T00:00:00")]
    quota = Quota(lambda: now[0], daily=50_000)
    assert (quota.stop, quota.burst) == (45_000, 2_250)
    chat = YouTubeChat(KEY, lambda _m: None, clock=lambda: now[0], quota=quota)
    end = pacific("2026-11-18T00:00:00")
    busy_waits: list[float] = []
    quiet_waits: list[float] = []
    while now[0] < end:
        hour = datetime.fromtimestamp(now[0], QUOTA_DAY).hour
        chat._last_message = now[0] if hour >= 12 else 0.0  # pyright: ignore[reportPrivateUsage]
        wait = pace(chat, 10)
        (busy_waits if hour >= 12 else quiet_waits).append(wait)
        now[0] += wait
        if now[0] >= end:
            break
        quota.spend(youtube.COST_STREAM_LIST)
        now[0] += 10
    assert quota.spent() <= 45_000
    assert max(busy_waits) < 1.0  # chat that plays: at once, all afternoon
    assert max(quiet_waits) < 6.0  # quiet: a few seconds
    quota.used_up()
    assert quota.spent() == 50_000
