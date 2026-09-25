After an `{% autoescape %}` block ends, filters and functions that receive the evaluation context still see autoescaping switched on.

```python
from jinja2 import Environment, pass_eval_context

env = Environment()
env.filters["mode"] = pass_eval_context(lambda eval_ctx, value: f"{value}:{eval_ctx.autoescape}")

tmpl = env.from_string(
    "{{ x|mode }} {% autoescape true %}{{ x|mode }}{% endautoescape %} {{ x|mode }}"
)
print(tmpl.render(x="v"))
```

Expected `v:False v:True v:False`; printed `v:False v:True v:True`. The filter after `{% endautoescape %}` is handed an evaluation context whose `autoescape` is still `True`, and it stays that way for the rest of the template. Plain output is not affected — `{{ '<b>' }}` after the block is not escaped — which is why this went unnoticed; it shows up through anything that consults the evaluation context at render time: a `pass_eval_context` global (`{{ mode() }}` after the block reports `True`), and built-in filters such as `join`, which after the block escapes the plain strings in a list that also contains `Markup` values (`{{ parts|join(', ') }}` with `[Markup("<b>"), "<i>"]` renders `<b>, &lt;i&gt;` instead of `<b>, <i>`). Nested blocks show the same drift: after `{% autoescape true %}{% autoescape false %}{% endautoescape %}…{% endautoescape %}` the mode is `False` where it should be `True` again, and with the block inside a `{% for %}` loop every iteration reports `True` after it.
