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
- The quota is 10,000 units a day by default, and YouTube's day starts at
  midnight Pacific. Units are counted here (kept in brain's store, so a
  restart doesn't start the count again) and the adapter pauses at
  QUOTA_STOP rather than run into the hard limit. When the quota is used up,
  by us or as Google says (403 quotaExceeded, or a streamList refusal about
  quota), it waits for the reset and goes on by itself, and says so once
  (on_quota: #ops).
- Each streamList call costs 5 units however long YouTube keeps it open, and
  YouTube closes streams after about 10 s: reconnecting at once spends the
  default quota in about 5 hours. So the day's quota is an allowance that
  builds up (Quota.ready_at): QUOTA_BURST units at midnight Pacific,
  growing evenly to QUOTA_STOP by the next. After YouTube closes a stream,
  the next call waits until the allowance covers it (`_pace`); while chat
  is quiet (no message for ACTIVE_FOR seconds) it also waits PACE_QUIET
  times the even interval (MAX_GAP at most), so quiet hours leave quota for
  busy ones. It never runs out before the reset. With the default quota:
  about 66 s between calls while chat is quiet, and while it's busy from
  no wait (after quiet hours) to about 40 s (busy all day); with more
  quota, no wait. Messages said meanwhile arrive when it reconnects, from
  the page token, and aren't dropped as late: MAX_AGE counts from the end
  of that wait (MAX_GAP of it at most). Status shows the calls in the last
  hour, how long the closed ones lasted on average, how long the open one
  has been open, and the wait before the next.

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
from datetime import UTC, datetime, timedelta
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
QUOTA_DAILY = 10_000  # YouTube's default daily quota
QUOTA_STOP = 9000  # of it: we pause here, before Google refuses
QUOTA_BURST = 450  # of it, ready at midnight Pacific; the rest comes evenly through the day
QUOTA_DAY = ZoneInfo("America/Los_Angeles")  # YouTube's quota resets at midnight Pacific
QUOTA_RESUME_AFTER = 120.0  # seconds after the reset before trying again (clocks differ)
QUOTA_REASONS = ("quotaExceeded", "dailyLimitExceeded")

MAX_AGE = 30.0  # seconds: older commands are dropped (counted from the end of a paced wait)
MIN_CALL_GAP = 5.0  # seconds between two streamList calls, whatever happens
ACTIVE_FOR = 180.0  # seconds: chat with a message this recent is active
PACE_QUIET = 1.5  # times the even interval between calls, while chat is quiet
MAX_GAP = 90.0  # seconds: the longest wait for a quiet chat, and how late MAX_AGE forgives
WAIT_FOR_LIVE = 60.0  # seconds between checks while the video isn't live yet
BACKOFF = (2.0, 120.0)  # after an error: first and longest wait
TOO_SOON_WAIT = 30.0  # seconds after YouTube says a call came too soon after the last
SEEN_IDS = 2000  # message IDs remembered to drop duplicates
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

TEXT = pb.LiveChatMessageSnippet.TypeWrapper.TEXT_MESSAGE_EVENT
SUPER_CHAT = pb.LiveChatMessageSnippet.TypeWrapper.SUPER_CHAT_EVENT
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


class QuotaOut(Exception):
    """The day's quota is used up: the adapter waits for the reset, then goes on."""


class Quota:
    """Units spent in YouTube's day (midnight Pacific to midnight), kept across restarts.

    load/save read and write "DAY UNITS" (brain's store, setting youtube_quota)."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        load: Callable[[], str | None] = lambda: None,
        save: Callable[[str], None] = lambda _value: None,
    ) -> None:
        self.clock, self._save = clock, save
        day, _, used = (load() or "").partition(" ")
        self.day, self.used = (day, int(used)) if used.isdigit() else ("", 0)

    def today(self) -> str:
        return datetime.fromtimestamp(self.clock(), QUOTA_DAY).date().isoformat()

    def _roll(self) -> None:
        if self.day != (day := self.today()):
            self.day, self.used = day, 0

    def spent(self) -> int:
        self._roll()
        return self.used

    def spend(self, units: int) -> None:
        """Count units before a call; QuotaOut if they'd take us past QUOTA_STOP."""
        self._roll()
        if self.used + units > QUOTA_STOP:
            how = "used up" if self.used >= QUOTA_DAILY else "nearly used"
            raise QuotaOut(f"the daily quota is {how} ({self.used} units)")
        self.used += units
        self._save(f"{self.day} {self.used}")

    def used_up(self) -> None:
        """Google says the day's quota is gone: nothing more until the reset."""
        self._roll()
        self.used = max(self.used, QUOTA_DAILY)
        self._save(f"{self.day} {self.used}")

    def resets_at(self) -> float:
        """The next midnight Pacific, as a Unix time."""
        return self._day()[1]

    def _day(self) -> tuple[float, float]:
        """Today's quota day: its first and last moment (midnight Pacific to midnight, 23 to 25
        hours around daylight saving changes)."""
        today = datetime.fromtimestamp(self.clock(), QUOTA_DAY).date()
        start, end = (
            datetime.combine(d, datetime.min.time(), QUOTA_DAY) for d in (today, today + timedelta(1))
        )
        return start.timestamp(), end.timestamp()

    def rate(self) -> float:
        """Units a second the day's allowance grows by."""
        start, end = self._day()
        return (QUOTA_STOP - QUOTA_BURST) / (end - start)

    def ready_at(self, units: int) -> float:
        """When spending `units` more keeps to the day's allowance: QUOTA_BURST at midnight
        Pacific, growing evenly to QUOTA_STOP at the next. What quiet hours don't spend, busy
        ones can; the day never runs out early."""
        self._roll()
        return self._day()[0] + max(0, self.used + units - QUOTA_BURST) / self.rate()


@dataclass
class Counters:
    messages: int = 0  # chat messages handed to brain
    stale: int = 0  # dropped as too old
    duplicates: int = 0
    calls: int = 0  # streamList calls
    closed: int = 0  # of them, those that ended
    stream_seconds: float = 0.0  # how long the closed ones stayed open, in all
    starts: deque[float] = field(default_factory=lambda: deque(maxlen=4096))  # monotonic
    open_since: float | None = None  # monotonic, while a call is open
    next_call: float | None = None  # monotonic, while waiting to spread the quota
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
        quota: Quota | None = None,
        on_quota: Callable[[str], None] = lambda _text: None,
        on_super_chat: Callable[[str, str, int], None] = lambda _user, _name, _tier: None,
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
        self.quota = quota or Quota(clock)
        self.on_quota = on_quota  # brain: a line in #ops when the quota runs out
        self.on_super_chat = on_super_chat  # brain: (user, name, tier) -> a chaos modifier
        self._quota_told = ""  # the quota day #ops was told about
        self.video_id: str | None = None
        self.state = "off"  # off, looking, waiting, streaming, ended, stopped
        self.reason = ""
        self.counters = Counters()
        self._task: asyncio.Task[None] | None = None
        self._seen: deque[str] = deque(maxlen=SEEN_IDS)
        self._seen_set: set[str] = set()
        self._last_call = 0.0
        self._last_message = 0.0  # clock(): the last chat message delivered (active or quiet)
        self._paced = 0.0  # seconds waited by choice before the call now open (MAX_AGE starts after)

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
        c = self.counters
        now = time.monotonic()
        return {
            "state": self.state,
            "reason": self.reason,
            "video": self.video_id,
            "messages": c.messages,
            "stale": c.stale,
            "calls": c.calls,  # since brain started
            "calls_last_hour": sum(1 for t in c.starts if now - t <= 3600),
            "average_stream_s": round(c.stream_seconds / c.closed) if c.closed else None,  # closed ones
            "open_s": round(now - c.open_since) if c.open_since is not None else None,
            "next_s": round(max(0.0, c.next_call - now)) if c.next_call is not None else None,
            "quota_used_today": self.quota.spent(),
            "recent_errors": list(c.errors),
        }

    async def viewers(self) -> int | None:
        """How many watch the followed video now (videos.list, 1 unit): None if not known."""
        if self.video_id is None or self.state != "streaming":
            return None
        try:
            self.quota.spend(COST_VIDEOS_LIST)
        except QuotaOut:
            return None
        params = {"part": "liveStreamingDetails", "id": self.video_id, "key": self.api_key}
        try:
            async with (
                aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session,
                session.get(f"{self.api}/videos", params=params) as response,
            ):
                data: dict[str, Any] = await response.json(content_type=None)
                if response.status != 200:
                    return None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        items: list[dict[str, Any]] = data.get("items") or []
        details: dict[str, Any] = (items[0].get("liveStreamingDetails") or {}) if items else {}
        count = details.get("concurrentViewers")  # a string; absent when YouTube hides it
        return int(count) if isinstance(count, str) and count.isdigit() else None

    # --- the work ---------------------------------------------------------------

    async def _run(self, video_id: str) -> None:
        while True:
            try:
                chat_id = await self._find_chat(video_id)
                await self._stream(chat_id)
            except QuotaOut as out:
                await self._wait_for_quota(str(out))
                continue  # the same video, from the start: its chat may have changed
            except Stop as stop:
                self.reason = str(stop)
                if self.state != "ended":
                    self.state = "stopped"
                log.warning("YouTube chat for %s: %s", video_id, stop)
            except Exception as err:  # never die silently: show it in status
                self.state, self.reason = "stopped", f"unexpected error: {type(err).__name__}"
                log.exception("YouTube chat for %s stopped", video_id)
            return

    async def _wait_for_quota(self, why: str) -> None:
        resets = self.quota.resets_at()
        at = datetime.fromtimestamp(resets, UTC).strftime("%H:%M UTC")
        self.state = "waiting"
        self.reason = f"{why}: YouTube chat is paused until the quota resets at midnight Pacific ({at})"
        log.warning("YouTube chat: %s", self.reason)
        if self._quota_told != self.quota.day:
            self._quota_told = self.quota.day
            self.on_quota(
                f"⏸️ YouTube chat is paused: {why} ({self.quota.spent()} units today). "
                f"It goes on by itself when the quota resets, at midnight Pacific ({at})."
            )
        await asyncio.sleep(max(0.0, resets - self.clock()) + QUOTA_RESUME_AFTER)

    def _error(self, text: str) -> None:
        self.counters.errors.append(text)
        log.warning("YouTube chat: %s", text)

    async def _find_chat(self, video_id: str) -> str:
        """The video's live chat ID, waiting while the video isn't live yet."""
        url = f"{self.api}/videos"
        params = {"part": "liveStreamingDetails", "id": video_id, "key": self.api_key}
        while True:
            self.quota.spend(COST_VIDEOS_LIST)
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
            if status == 403 and (reason := _google_reason(data)) in QUOTA_REASONS:
                self.quota.used_up()
                raise QuotaOut(f"YouTube says the daily quota is used up ({reason})")
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
                self.quota.spend(COST_STREAM_LIST)
                self._last_call = time.monotonic()
                self.counters.calls += 1
                self.counters.starts.append(self._last_call)
                self.counters.open_since = self._last_call
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
                    self._paced = 0.0  # a backlog after an error is late: MAX_AGE as it is
                    backoff = self._rpc_error(err, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF[1])
                    continue
                finally:
                    opened = time.monotonic() - self._last_call
                    self.counters.stream_seconds += opened
                    self.counters.closed += 1
                    self.counters.open_since = None
                # The server closed the stream normally: resume from the token, in a while.
                self._paced = self._pace(opened)
                if self._paced > 0:
                    self.counters.next_call = time.monotonic() + self._paced
                    try:
                        await asyncio.sleep(self._paced)
                    finally:
                        self.counters.next_call = None

    def _pace(self, opened: float) -> float:
        """Seconds to wait before the next call, after one YouTube kept open `opened` seconds:
        until the day's allowance covers it, and while chat is quiet, longer."""
        if self.quota.spent() + COST_STREAM_LIST > QUOTA_STOP:
            return 0.0  # the next spend() pauses until the reset
        now = self.clock()
        wait = max(0.0, self.quota.ready_at(COST_STREAM_LIST) - now)
        if now - self._last_message >= ACTIVE_FOR:  # quiet: leave quota for when chat plays
            even = COST_STREAM_LIST / self.quota.rate()
            wait = max(wait, min(MAX_GAP, PACE_QUIET * even - opened))
        return wait

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
            if "quota" in detail.lower():  # not "too soon after the last call": the day's quota
                self.quota.used_up()
                raise QuotaOut(f"YouTube says the daily quota is used up ({detail[:80]})")
            return max(backoff, TOO_SOON_WAIT)
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
        if snippet.type not in (TEXT, SUPER_CHAT):
            return  # memberships, bans, deletions...: later phases
        now = self.clock()
        published = _published(snippet.published_at)
        if published is not None and now - published > MAX_AGE + min(self._paced, MAX_GAP):
            self.counters.stale += 1
            return
        self._last_message = now
        author = item.author_details
        try:
            uid = user_id("yt", author.channel_id or snippet.author_channel_id)
        except ValueError:
            return
        if snippet.type == SUPER_CHAT:  # its tier only: never the amount, nor its message
            tier = snippet.super_chat_details.tier if snippet.HasField("super_chat_details") else 0
            if tier:
                self.on_super_chat(uid, author.display_name, int(tier))
            return
        text = snippet.text_message_details.message_text or snippet.display_message
        self.counters.messages += 1
        self.deliver(ChatMessage(uid, author.display_name, text, now, roles_of(author)))


def _google_reason(data: dict[str, Any]) -> str:
    error: dict[str, Any] = data.get("error") or {}
    errors: list[dict[str, Any]] = error.get("errors") or [{}]
    return str(errors[0].get("reason") or error.get("message") or "unknown")[:120]
