`Environment.compile_templates` writes different files for the same templates from one run to the next, which breaks a reproducible build that checks the compiled templates into version control.

```python
from jinja2 import DictLoader, Environment

src = "{{ a|upper }}{{ b|lower }}{{ c|trim }}{{ d|title }}{{ e|first }}{{ f|last }}"
env = Environment(loader=DictLoader({"page": src}))
env.compile_templates("build", zip=None)
```

Running this twice (in two processes) and diffing `build/`: the module is the same up to the block that fetches the filters, where the `environment.filters['upper']` … `environment.filters['last']` lookups are bound to `t_1` … `t_6` in a different order each time, so every use of a filter in the body changes its temporary name as well. With `PYTHONHASHSEED=0` the output is stable, which is the only reason our CI passed. The same happens with several `{% import "x" as y with context %}` statements: the names that are handed to the imported module are emitted in an order that changes between runs. Templates that use a single filter, or that import nothing, compile identically every time.

Compiled output should not depend on the hash seed of the process: the same source must produce byte-identical compiled modules.
