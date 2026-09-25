Included templates and modules imported `with context` no longer see the variables of the block they are used in — loop variables, `{% with %}` assignments and variables assigned inside a block are all missing.

```python
from jinja2 import DictLoader, Environment

env = Environment(loader=DictLoader({
    "inc": "[{{ x }}]",
    "mod": "{% macro show() %}<{{ x }}>{% endmacro %}",
}))
print(env.from_string("{% for x in [1, 2] %}{% include 'inc' %}{% endfor %}").render())
print(env.from_string(
    "{% for x in [1, 2] %}{% import 'mod' as m with context %}{{ m.show() }}{% endfor %}"
).render())
print(env.from_string("{% with x = 'w' %}{% include 'inc' %}{% endwith %}").render())
```

```
[][]
<><>
[]
```

Expected `[1][2]`, `<1><2>` and `[w]`. Variables of the top-level context still get through — a variable assigned at the top of the template, outside any block, is visible to the include as before — and `{% from 'mod' import show with context %}` inside the loop is broken the same way as the `import`. `import ... without context` correctly sees nothing, as before.
