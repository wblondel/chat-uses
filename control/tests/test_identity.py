import pytest

from chatuses.identity import Presence, user_id


def test_user_ids() -> None:
    assert user_id("yt", "UCabc123") == "yt:UCabc123"
    assert user_id("dc", "123456789") == "dc:123456789"
    for bad in ("", "a:b", "x" * 65, "line\nbreak"):
        with pytest.raises(ValueError):
            user_id("yt", bad)


def test_active_means_a_message_in_the_last_5_minutes() -> None:
    presence = Presence()
    presence.saw("yt:a", 0.0)
    assert presence.is_active("yt:a", 299.0)
    assert not presence.is_active("yt:a", 301.0)
    assert presence.active(100.0) == {"yt:a"}
    assert not presence.is_active("yt:nobody", 0.0)


def test_banned_users_are_not_active() -> None:
    presence = Presence(is_banned=lambda user: user == "yt:bad")
    presence.saw("yt:bad", 0.0)
    presence.saw("yt:good", 0.0)
    assert presence.active(1.0) == {"yt:good"}


def test_eligible_voters_have_been_around_for_2_minutes() -> None:
    presence = Presence()
    presence.saw("yt:regular", 0.0)
    presence.saw("yt:regular", 190.0)
    presence.saw("yt:raider", 150.0)
    assert presence.is_eligible("yt:regular", 200.0)
    assert not presence.is_eligible("yt:raider", 200.0)  # here since 150: needs <= 80
    assert presence.is_eligible("yt:raider", 290.0)


def test_coming_back_after_a_break_keeps_eligibility() -> None:
    presence = Presence()
    presence.saw("yt:a", 0.0)
    presence.forget_idle(400.0)
    assert presence.active(400.0) == set()
    presence.saw("yt:a", 401.0)
    assert presence.is_eligible("yt:a", 401.0)
