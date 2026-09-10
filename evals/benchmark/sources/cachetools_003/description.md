`LRUCache` evicts entries that were just read.

With `LRUCache(maxsize=3)`: insert `a`, `b`, `c`, read `cache["a"]`, then insert `d`. The cache evicts `a` — the key that was accessed a moment ago — and keeps `b`, which has not been touched since it was inserted. The same happens when the read goes through `cache.get("a")`.

Eviction order seems to depend only on insertion order, so the cache behaves like a FIFO queue instead of a least-recently-used cache. Reading an entry should count as using it.
