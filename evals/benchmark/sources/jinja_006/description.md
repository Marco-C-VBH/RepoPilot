`loop.revindex` counts down to `0` instead of `1` when the loop runs over a generator and `loop.last` (or `loop.nextitem`) has been looked at earlier in the same iteration.

```python
from jinja2 import Environment


def rows():
    yield from ["a", "b", "c"]


tmpl = Environment().from_string(
    "{% for x in items %}{% if loop.last %}!{% endif %}{{ loop.revindex }} {% endfor %}"
)
print(tmpl.render(items=rows()))
print(tmpl.render(items=list(rows())))
```

```
2 1 !0 
3 2 !1 
```

The second line (a list) is right; the first (a generator) is one short everywhere: `loop.revindex0` ends at `-1`, and the value the loop reports for its size comes out as `2` for three items. Checking `loop.last` first is what triggers it — `{{ loop.revindex }}{% if loop.last %}!{% endif %}` in this order prints `3 2 1!` for the generator too, and a loop that never asks about `loop.last` or `loop.nextitem` counts down correctly. Async rendering (`enable_async=True`, an async generator or a plain one) shows the same off-by-one.
