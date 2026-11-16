from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import pytest

from chatuses.commands import Meta, TypeText
from chatuses.pipeline import Censor, ChatMessage, Pipeline
from chatuses.safety.textfilter import HATE, PERSONAL, Kind
from chatuses.store import Store


@dataclass
class Stub:
    """A stand-in text filter: "bad" is hate, "@" personal; remembers what it's told."""

    seen: list[str] = field(default_factory=lambda: [])

    def kind(self, text: str) -> Kind | None:
        return "hate" if "bad" in text else "personal" if "@" in text else None

    def typed_kind(self, text: str) -> Kind | None:
        return self.kind(text)

    def typed(self, text: str) -> None:
        self.seen.append(f"typed {text}")

    def key(self, name: str, times: int = 1) -> None:
        self.seen.append(f"key {name}")


def msg(text: str, ts: float = 0.0, user: str = "yt:a") -> ChatMessage:
    return ChatMessage(user, "Alice", text, ts)


_stores: list[Store] = []


@pytest.fixture(autouse=True)
def close_stores() -> Iterator[None]:
    yield
    while _stores:
        _stores.pop().close()


def pipeline(
    censor: Censor | None = None, is_banned: Callable[[str], bool] = lambda _user: False
) -> Pipeline:
    _stores.append(Store(":memory:"))
    return Pipeline(_stores[-1], censor=censor or Stub(), is_banned=is_banned)


def test_chat_is_presence_only() -> None:
    pipe = pipeline()
    assert pipe.handle(msg("hello everyone")).kind == "chat"
    assert pipe.presence.active(1.0) == {"yt:a"}
    assert pipe.store.outcome_counts() == {}  # ordinary chat isn't stored


def test_accepted_and_recorded() -> None:
    pipe = pipeline()
    outcome = pipe.handle(msg("!type hello"))
    assert (outcome.kind, outcome.command) == ("accepted", TypeText("hello"))
    assert pipe.store.outcome_counts() == {"accepted": 1}
    assert [e[2] for e in pipe.store.events()] == ["command"]


def test_invalid_has_a_reason() -> None:
    outcome = pipeline().handle(msg("!click Z99"))
    assert outcome.kind == "invalid" and outcome.reason


def test_banned_users_are_stopped_first() -> None:
    pipe = pipeline(is_banned=lambda user: user == "yt:bad")
    assert pipe.handle(msg("!type hi", user="yt:bad")).kind == "banned"
    assert pipe.handle(msg("!type hi", user="yt:good")).kind == "accepted"


def test_input_is_rate_limited_but_meta_is_not() -> None:
    pipe = pipeline()
    kinds = [pipe.handle(msg("!enter", 0.0)).kind for _ in range(4)]
    assert kinds == ["accepted"] * 3 + ["rate_limited"]
    assert pipe.handle(msg("!help", 0.0)).command == Meta("help")  # votes and the meter have their own limits


def test_typed_text_and_save_names_are_filtered() -> None:
    pipe = pipeline()  # one user each: three refusals from one would be a timeout
    outcome = pipe.handle(msg("!type bad word", user="yt:a"))
    assert (outcome.kind, outcome.reason) == ("filtered", HATE)
    assert pipe.handle(msg("!save bad-name", user="yt:b")).kind == "filtered"
    assert pipe.handle(msg("!type me@example", user="yt:c")).reason == PERSONAL
    assert pipe.handle(msg("!type fine", user="yt:d")).kind == "accepted"


def test_the_filter_learns_what_is_typed_and_pressed() -> None:
    stub = Stub()
    pipe = pipeline(stub)
    for i, text in enumerate(["!type ab", "!type bad", "!bksp 2", "!enter", "!key ctrl+c", "!click F7"]):
        pipe.handle(msg(text, 10.0 * i))
    assert stub.seen == ["typed ab", "key bksp", "key bksp", "key enter"]  # not refused text, not combos


def test_three_slurs_in_ten_minutes_earn_a_timeout() -> None:
    pipe = pipeline()
    first, second = pipe.handle(msg("!type bad", 0.0)), pipe.handle(msg("!type bad", 100.0))
    assert first.timed_out_until is None and second.timed_out_until is None
    third = pipe.handle(msg("!type bad", 200.0))
    assert (third.kind, third.timed_out_until) == ("filtered", 800.0)
    assert pipe.handle(msg("!enter", 300.0)).kind == "banned"
    assert pipe.handle(msg("!enter", 801.0)).kind == "accepted"
    [timeout] = [e for e in pipe.store.events() if e[2] == "timeout"]
    assert timeout[3]["user"] == "yt:a" and timeout[3]["until"] == 800.0


def test_strikes_expire_and_personal_details_dont_count() -> None:
    pipe = pipeline()
    pipe.handle(msg("!type bad", 0.0))
    pipe.handle(msg("!type bad", 100.0))
    assert pipe.handle(msg("!type bad", 700.0)).timed_out_until is None  # the first is 700 s old
    for i in range(5):
        assert pipe.handle(msg("!type me@example", 800.0 + 10 * i)).timed_out_until is None


def test_the_real_filter_is_the_default() -> None:
    store = Store(":memory:")
    _stores.append(store)
    pipe = Pipeline(store)
    kinds = [
        pipe.handle(msg(t, 10.0 * i)).kind
        for i, t in enumerate(["!type echo 06 12", "!type  34 56", "!type  78"])
    ]
    assert kinds == ["accepted", "accepted", "filtered"]  # a phone number, typed in pieces


def test_rate_limited_and_filtered_reasons_never_repeat_chat() -> None:
    pipe = pipeline()
    reasons = [pipe.handle(msg(f"!type xyzzy bad {i}", 0.0)).reason for i in range(5)]
    assert all(r and "xyzzy" not in r for r in reasons)
