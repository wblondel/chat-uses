"""YouTube live chat in (PLAN.md D8, Phase 4).

    video ID (set with chat-brain-admin) -> videos.list (REST, 1 unit)
      -> liveStreamingDetails.activeLiveChatId
      -> liveChatMessages.streamList (gRPC, server push, 5 units per call)

Reading a public chat only needs an API key, sent as `x-goog-api-key`.

- Every response carries a page token; after a disconnect the stream
  resumes from it, so nothing is missed or replayed.
- Commands that arrive more than MAX_AGE seconds after they were posted are
  dropped: the chat history YouTube sends on first connect, and the backlog
  after a long outage. Executing them late would surprise everyone.
- A chat that ended (or went offline) stops the adapter until a new video is
  set: each broadcast has its own chat.
- The quota is 10,000 units a day by default. Units are counted here and the
  adapter stops at QUOTA_STOP rather than run into the hard limit.

The stream_list_pb2 module is generated from Google's proto
(proto/stream_list.proto, proto/generate.sh).
"""

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
import grpc
import grpc.aio

from chatuses.adapters import stream_list_pb2 as pb
from chatuses.identity import user_id
from chatuses.pipeline import ChatMessage

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
GRPC_TARGET = "youtube.googleapis.com:443"
STREAM_LIST = "/youtube.api.v3.V3DataLiveChatMessageService/StreamList"

COST_VIDEOS_LIST = 1
COST_STREAM_LIST = 5
QUOTA_STOP = 9000  # of the default 10,000 units a day
QUOTA_DAY = ZoneInfo("America/Los_Angeles")  # YouTube's quota resets at midnight Pacific

MAX_AGE = 30.0  # seconds: older commands are dropped
MIN_CALL_GAP = 5.0  # seconds between two streamList calls, whatever happens
WAIT_FOR_LIVE = 60.0  # seconds between checks while the video isn't live yet
BACKOFF = (2.0, 120.0)  # after an error: first and longest wait
SEEN_IDS = 2000  # message IDs remembered to drop duplicates
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

TEXT = pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")

Deliver = Callable[[ChatMessage], None]


def video_id_from(text: str) -> str | None:
    """The video ID in a bare ID or a YouTube / YouTube Studio URL, or None."""
    text = text.strip()
    if _VIDEO_ID.fullmatch(text):
        return text
    parts = urlsplit(text if "//" in text else f"https://{text}")
    host = (parts.hostname or "").removeprefix("www.").removeprefix("m.")
    candidates: list[str] = []
    if host == "youtu.be":
        candidates.append(parts.path.strip("/").split("/")[0])
    elif host in ("youtube.com", "studio.youtube.com"):
        candidates += parse_qs(parts.query).get("v", [])
        segments = [s for s in parts.path.split("/") if s]
        for i, segment in enumerate(segments[:-1]):
            if segment in ("live", "video", "shorts", "embed"):
                candidates.append(segments[i + 1])
    return next((c for c in candidates if _VIDEO_ID.fullmatch(c)), None)


def roles_of(author: pb.LiveChatMessageAuthorDetails) -> frozenset[str]:
    roles: set[str] = set()
    if author.is_chat_owner:
        roles.add("owner")
    if author.is_chat_moderator:
        roles.add("moderator")
    if author.is_chat_sponsor:
        roles.add("member")
    if author.is_verified:
        roles.add("verified")
    return frozenset(roles)


def _published(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


class Stop(Exception):
    """The adapter stops for this video; `str()` is the reason, shown in status."""


@dataclass
class Counters:
    messages: int = 0  # chat messages handed to brain
    stale: int = 0  # dropped as too old
    duplicates: int = 0
    calls: int = 0  # streamList calls
    quota_day: str = ""
    quota_used: int = 0
    errors: deque[str] = field(default_factory=lambda: deque(maxlen=5))


class YouTubeChat:
    def __init__(
        self,
        api_key: str,
        deliver: Deliver,
        *,
        api: str = API,
        channel: Callable[[], grpc.aio.Channel] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api_key = api_key
        self.deliver = deliver
        self.api = api
        self._channel = channel or (
            lambda: grpc.aio.secure_channel(
                GRPC_TARGET,
                grpc.ssl_channel_credentials(),
                # Notice a dead connection within minutes, without pinging
                # more often than servers allow by default.
                options=[("grpc.keepalive_time_ms", 300_000), ("grpc.keepalive_timeout_ms", 20_000)],
            )
        )
        self.clock = clock
        self.video_id: str | None = None
        self.state = "off"  # off, looking, waiting, streaming, ended, stopped
        self.reason = ""
        self.counters = Counters()
        self._task: asyncio.Task[None] | None = None
        self._seen: deque[str] = deque(maxlen=SEEN_IDS)
        self._seen_set: set[str] = set()
        self._last_call = 0.0

    # --- control ---------------------------------------------------------------

    def follow(self, video_id: str | None) -> None:
        """Follow this video's chat from now on (None: stop following)."""
        if self._task:
            self._task.cancel()
            self._task = None
        self.video_id = video_id
        self.reason = ""
        if video_id is None:
            self.state = "off"
            return
        self.state = "looking"
        self._task = asyncio.create_task(self._run(video_id))

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def status(self) -> dict[str, Any]:
        self._spend(0)  # roll the quota day over if needed
        c = self.counters
        return {
            "state": self.state,
            "reason": self.reason,
            "video": self.video_id,
            "messages": c.messages,
            "stale": c.stale,
            "calls": c.calls,
            "quota_used_today": c.quota_used,
            "recent_errors": list(c.errors),
        }

    # --- the work ---------------------------------------------------------------

    async def _run(self, video_id: str) -> None:
        try:
            chat_id = await self._find_chat(video_id)
            await self._stream(chat_id)
        except Stop as stop:
            self.reason = str(stop)
            if self.state != "ended":
                self.state = "stopped"
            log.warning("YouTube chat for %s: %s", video_id, stop)
        except Exception as err:  # never die silently: show it in status
            self.state, self.reason = "stopped", f"unexpected error: {type(err).__name__}"
            log.exception("YouTube chat for %s stopped", video_id)

    def _spend(self, units: int) -> None:
        day = datetime.fromtimestamp(self.clock(), QUOTA_DAY).date().isoformat()
        if day != self.counters.quota_day:
            self.counters.quota_day, self.counters.quota_used = day, 0
        if units and self.counters.quota_used + units > QUOTA_STOP:
            raise Stop(
                f"daily quota nearly used ({self.counters.quota_used} units): set the video again tomorrow"
            )
        self.counters.quota_used += units

    def _error(self, text: str) -> None:
        self.counters.errors.append(text)
        log.warning("YouTube chat: %s", text)

    async def _find_chat(self, video_id: str) -> str:
        """The video's live chat ID, waiting while the video isn't live yet."""
        url = f"{self.api}/videos"
        params = {"part": "liveStreamingDetails", "id": video_id, "key": self.api_key}
        while True:
            self._spend(COST_VIDEOS_LIST)
            try:
                async with (
                    aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session,
                    session.get(url, params=params) as response,
                ):
                    data: dict[str, Any] = await response.json(content_type=None)
                    status = response.status
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                self._error(f"videos.list: {type(err).__name__}")
                await asyncio.sleep(WAIT_FOR_LIVE)
                continue
            if status >= 500:  # Google's side, usually brief
                self._error(f"videos.list: HTTP {status}")
                await asyncio.sleep(WAIT_FOR_LIVE)
                continue
            if status != 200:
                raise Stop(f"videos.list refused ({status}): {_google_reason(data)}")
            items: list[dict[str, Any]] = data.get("items") or []
            if not items:
                raise Stop("no such video (check the ID)")
            details: dict[str, Any] = items[0].get("liveStreamingDetails") or {}
            if not details:
                raise Stop("this video is not a live stream")
            if details.get("actualEndTime"):
                self.state = "ended"
                raise Stop("this live stream has ended: set the new one")
            chat_id = details.get("activeLiveChatId")
            if isinstance(chat_id, str) and chat_id:
                return chat_id
            self.state = "waiting"
            self.reason = "waiting for the stream to go live"
            await asyncio.sleep(WAIT_FOR_LIVE)

    async def _stream(self, chat_id: str) -> None:
        token = ""
        backoff = BACKOFF[0]
        async with self._channel() as channel:
            call = channel.unary_stream(
                STREAM_LIST,
                request_serializer=pb.LiveChatMessageListRequest.SerializeToString,
                response_deserializer=pb.LiveChatMessageListResponse.FromString,
            )
            while True:
                await asyncio.sleep(max(0.0, self._last_call + MIN_CALL_GAP - time.monotonic()))
                self._spend(COST_STREAM_LIST)
                self._last_call = time.monotonic()
                self.counters.calls += 1
                request = pb.LiveChatMessageListRequest(
                    live_chat_id=chat_id, part=["id", "snippet", "authorDetails"], page_token=token or None
                )
                self.state, self.reason = "streaming", ""
                try:
                    async for response in call(request, metadata=(("x-goog-api-key", self.api_key),)):
                        backoff = BACKOFF[0]
                        for item in response.items:
                            self._item(item)
                        if response.next_page_token:
                            token = response.next_page_token
                        if response.offline_at:
                            self.state = "ended"
                            raise Stop("the live stream went offline")
                except grpc.aio.AioRpcError as err:
                    backoff = self._rpc_error(err, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF[1])
                    continue
                # The server closed the stream normally: resume from the token.

    def _rpc_error(self, err: grpc.aio.AioRpcError, backoff: float) -> float:
        """Raise Stop for errors retrying can't fix; return how long to wait otherwise."""
        code = err.code()
        detail = (err.details() or "")[:200]
        self._error(f"streamList: {code.name} {detail}")
        if code in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.NOT_FOUND):
            self.state = "ended"
            raise Stop(f"the chat has ended or is disabled ({code.name})")
        if code in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.UNAUTHENTICATED):
            raise Stop(f"YouTube refused the API key ({code.name}): {detail}")
        if code == grpc.StatusCode.INVALID_ARGUMENT:
            raise Stop(f"YouTube rejected the request: {detail}")
        if code == grpc.StatusCode.RESOURCE_EXHAUSTED:
            return max(backoff, 30.0)
        self.state, self.reason = "reconnecting", code.name
        return backoff

    def _item(self, item: pb.LiveChatMessage) -> None:
        if item.id:
            if item.id in self._seen_set:
                self.counters.duplicates += 1
                return
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(item.id)
            self._seen_set.add(item.id)
        snippet = item.snippet
        if snippet.type != TEXT:
            return  # Super Chats, memberships, bans, deletions...: later phases
        now = self.clock()
        published = _published(snippet.published_at)
        if published is not None and now - published > MAX_AGE:
            self.counters.stale += 1
            return
        author = item.author_details
        try:
            uid = user_id("yt", author.channel_id or snippet.author_channel_id)
        except ValueError:
            return
        text = snippet.text_message_details.message_text or snippet.display_message
        self.counters.messages += 1
        self.deliver(ChatMessage(uid, author.display_name, text, now, roles_of(author)))


def _google_reason(data: dict[str, Any]) -> str:
    error: dict[str, Any] = data.get("error") or {}
    errors: list[dict[str, Any]] = error.get("errors") or [{}]
    return str(errors[0].get("reason") or error.get("message") or "unknown")[:120]
