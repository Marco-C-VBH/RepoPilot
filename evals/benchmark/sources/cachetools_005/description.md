Applying `@cached(...)` with a newly created cache has no effect.

```python
cache = LRUCache(maxsize=8)

@cached(cache)
def square(n):
    print("computing", n)
    return n * n

square(3)
square(3)  # prints "computing 3" a second time; len(cache) is still 0
```

The wrapped function runs on every call and nothing is ever stored, although `square.cache` is the cache object that was passed in. A plain `dict` used as the cache shows the same behaviour. Repeated calls with the same arguments should be served from the cache.
