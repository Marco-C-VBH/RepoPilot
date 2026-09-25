Loading a template now fails when a custom filter applied to literal arguments raises, even if the filter sits in a branch that never renders.

```python
from jinja2 import Environment


class LookupFailed(Exception):
    pass


def lookup(key):
    raise LookupFailed(f"no entry for {key!r}")


env = Environment()
env.filters["lookup"] = lookup
env.from_string("{% if false %}{{ 'x'|lookup }}{% endif %}ok")
```

This raises `LookupFailed: no entry for 'x'` from `from_string` — before, the template loaded and rendered `ok`, and the filter only ran (and only raised) when its branch actually rendered. The same template with a variable instead of the literal (`{{ key|lookup }}`) still loads fine. A custom test used on a literal shows it too: `{% if false %}{{ 'b' is registered }}{% endif %}` with a `registered` test that raises `KeyError` for unknown names now fails at load time with that `KeyError`, and a filter that raises `StopIteration` on `{{ {}|first_key }}` fails the same way. Built-in filters given the wrong kind of literal argument (`{{ 'abc'|center('w') }}` inside a dead branch) still load and render as before.

Rendering `{{ 'x'|lookup }}` in a live branch should still raise `LookupFailed` — but from `render`, not from loading the template.
