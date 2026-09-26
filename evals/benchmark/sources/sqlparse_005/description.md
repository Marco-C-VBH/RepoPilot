`indent_columns=True` on its own does nothing any more: the statement comes back exactly as it went in.

```python
import sqlparse

print(sqlparse.format("select a, b from t where c = 1", indent_columns=True))
```

```
select a, b from t where c = 1
```

This used to print

```
select
  a,
  b
from t
where c = 1
```

and still does when `reindent=True` is passed alongside — `sqlparse.format(sql, indent_columns=True, reindent=True)` is unchanged. The option ("indent all columns by indent_width instead of keyword length", also `--indent_columns` on the command line) always implied reindentation, `indent_width` included: `indent_columns=True, indent_width=4` indented the columns by four spaces on its own. Only the case where `indent_columns` is passed without `reindent` regressed; no error is raised, the option is silently ignored.
