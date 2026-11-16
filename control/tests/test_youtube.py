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
