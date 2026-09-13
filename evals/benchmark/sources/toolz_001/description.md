`sliding_window` drops the first window.

`list(sliding_window(2, [1, 2, 3, 4]))` returns `[(2, 3), (3, 4)]` instead of `[(1, 2), (2, 3), (3, 4)]`, and a sequence of exactly `n` elements produces no window at all: `list(sliding_window(3, [7, 8, 9]))` is `[]` where `[(7, 8, 9)]` is expected. In general a sequence of length `L` should give `L - n + 1` windows, the first one starting at the first element.

`toolz/tests/test_itertoolz.py::test_sliding_window` fails.
