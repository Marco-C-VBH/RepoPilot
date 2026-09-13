`unique` no longer removes duplicates when a `key` function is given.

`tuple(unique(['cat', 'mouse', 'dog', 'hen'], key=len))` returns all four words instead of `('cat', 'mouse')`, and `tuple(unique(['A', 'a', 'b', 'B'], key=str.lower))` returns `('A', 'a', 'b')` instead of `('A', 'b')`. Without a `key` argument deduplication still works as documented.

Expected: for every distinct key value, only the first element with that key is yielded, in order of first appearance, whether the input is a list or an iterator.
