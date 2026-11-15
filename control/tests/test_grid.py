import pytest

from chatuses.grid import Grid


def test_default_grid_is_16_by_9_cells_of_100_px() -> None:
    grid = Grid()
    assert (grid.cols, grid.rows) == (16, 9)
    assert grid.column_labels == list("ABCDEFGHIJKLMNOP")
    assert grid.row_labels == [str(n) for n in range(1, 10)]


@pytest.mark.parametrize(
    ("ref", "point"),
    [
        ("F7", (550, 650)),  # a bare cell is its centre
        ("F7.5", (550, 650)),
        ("f7.5", (550, 650)),
        (" F7 ", (550, 650)),
        ("A1.1", (16, 16)),  # sub-cells: phone keypad, 1 top-left
        ("A1.3", (83, 16)),
        ("A1.7", (16, 83)),
        ("P9.9", (1583, 883)),  # 9 bottom-right, still on screen
    ],
)
def test_locate(ref: str, point: tuple[int, int]) -> None:
    assert Grid().locate(ref) == point


@pytest.mark.parametrize(
    "ref", ["Q1", "Z9", "A0", "A10", "F7.0", "F7.10", "F7.", "", "77", "FF7", "F7.5.5", "é1"]
)
def test_locate_refuses_bad_cells(ref: str) -> None:
    with pytest.raises(ValueError):
        Grid().locate(ref)


@pytest.mark.parametrize("ref", ["xyzzy", "F7xyzzy", "xyzzy.5"])
def test_refusals_never_repeat_what_chat_wrote(ref: str) -> None:
    # Messages appear on stream: chat must not be able to put words there.
    with pytest.raises(ValueError) as err:
        Grid().locate(ref)
    assert "xyzzy" not in str(err.value).lower()


def test_other_screens() -> None:
    hd = Grid(1920, 1080)  # 16:9: still 16 x 9, bigger cells
    assert (hd.cols, hd.rows) == (16, 9)
    assert hd.locate("A1") == (60, 60)
    retro = Grid(800, 600)  # 4:3: about 100 px cells
    assert (retro.cols, retro.rows) == (8, 6)
    assert retro.locate("H6") == (750, 550)
    with pytest.raises(ValueError):
        retro.locate("I1")


def test_unusable_screens() -> None:
    with pytest.raises(ValueError):
        Grid(50, 50)
    with pytest.raises(ValueError):
        Grid(4000, 900)  # more than 26 columns
