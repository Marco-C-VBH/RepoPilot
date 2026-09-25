`get_template` raises a `KeyError` out of the template cache after a template has been edited and reloaded.

```python
from jinja2 import DictLoader, Environment

sources = {"a": "a1", "b": "b1", "c": "c1"}
env = Environment(loader=DictLoader(sources), cache_size=2)

env.get_template("a")
env.get_template("b")
sources["a"] = "a2"            # edit the template; auto_reload picks it up
env.get_template("a").render()  # 'a2' as expected
env.get_template("c")           # the cache is full, the oldest entry goes
env.get_template("b")
env.get_template("a")           # KeyError
```

The last call fails with

```
KeyError: (<weakref at 0x...; to 'DictLoader' at 0x...>, 'a')
```

raised while the cache makes room for the entry. Nothing is wrong with the templates themselves: with `cache_size=-1` (unbounded) or without the edit in between, the same sequence renders every template correctly. It only takes one reload of a template that is already cached, followed by enough other templates to fill the cache, and from then on evictions can hit a key that is no longer there. The cache is visibly off right after the reload: `len(env.cache)` is `1` although two templates had been loaded into a cache of two, and the other template that was cached before the edit is already gone.
