Re-assigning a key that is already present in a `TLRUCache` does not extend its lifetime.

With a `ttu` callback that returns `now + 10` and a controllable timer: set `cache["k"] = 1` at t=0, then `cache["k"] = 2` at t=5. The entry should now live until t=15, but at t=12 `len(cache)` is `0` and the key is gone — it was evicted at the original t=10 deadline.

The second assignment should behave like a fresh insert: the new value stays readable until t=15, and the cache keeps working normally after that.
