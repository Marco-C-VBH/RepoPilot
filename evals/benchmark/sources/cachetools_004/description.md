Caches hold one entry fewer than their `maxsize`.

`LRUCache(maxsize=3)` evicts an entry as soon as the third key is inserted, so it never holds more than two items: `len(cache)` and `cache.currsize` top out at 2. With a custom `getsizeof`, values whose sizes add up to exactly `maxsize` do not fit together either — in a cache with `maxsize=10`, storing a value of size 4 and then one of size 6 evicts the first.

Every cache class is affected, which points at the shared eviction logic in `Cache.__setitem__`; the existing tests in `tests/test_cache.py` and `tests/test_lru.py` fail (`test_insert`, `test_lru`, `test_update`, ...). A cache must be allowed to fill up to exactly `maxsize`.
