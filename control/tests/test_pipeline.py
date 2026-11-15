from collections.abc import Callable

from chatuses.commands import Meta, TypeText
from chatuses.pipeline import ChatMessage, Pipeline, TextFilter, allow_all
from chatuses.store import Store


def msg(text: str, ts: float = 0.0, user: str = "yt:a") -> ChatMessage:
    return ChatMessage(user, "Alice", text, ts)


def pipeline(
    text_filter: TextFilter = allow_all, is_banned: Callable[[str], bool] = lambda _user: False
) -> Pipeline:
    return Pipeline(Store(":memory:"), text_filter=text_filter, is_banned=is_banned)


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


def test_text_filter_hook() -> None:
    pipe = pipeline(text_filter=lambda _user, text: "filtered" if "bad" in text else None)
    assert pipe.handle(msg("!type bad word")).kind == "filtered"
    assert pipe.handle(msg("!save bad-name", 5.0)).kind == "filtered"
    assert pipe.handle(msg("!type fine", 10.0)).kind == "accepted"


def test_rate_limited_and_filtered_reasons_never_repeat_chat() -> None:
    pipe = pipeline(text_filter=lambda _user, _text: "filtered")
    reasons = [pipe.handle(msg(f"!type xyzzy {i}", 0.0)).reason for i in range(5)]
    assert all(r and "xyzzy" not in r for r in reasons)
