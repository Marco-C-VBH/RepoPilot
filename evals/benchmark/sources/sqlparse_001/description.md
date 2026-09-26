The `except` keyword is no longer treated as the start of a new clause: the first query's where-clause swallows it, and neither reindent mode puts it on its own line.

```python
import sqlparse

sql = "select 1 from foo where 2 = 3 except select 2 from bar where 1 = 2"
print(sqlparse.format(sql, reindent=True))
```

```
select 1
from foo
where 2 = 3 except
  select 2
  from bar where 1 = 2
```

Expected, as before:

```
select 1
from foo
where 2 = 3
except
select 2
from bar
where 1 = 2
```

The second query is indented as if it were part of the first query's condition, and its own where-clause is no longer broken out. With `reindent_aligned=True` the same input keeps the keyword and the second select on the line of the first condition:

```
select 1
  from foo
 where 2 = 3 except select 2
  from bar
```

and without any condition before it (`select a from foo except select a from bar`) the aligned output runs the first from-clause, the keyword and the second select together on one line. The parse tree shows the cause of the first half: `sqlparse.parse(sql)[0]` now has a single clause group spanning everything from the first where-clause to the end of the statement, where there used to be one group for the first condition, the keyword as a top-level token, and a second group for the second condition. `union` is not affected: `select a from foo union select a from bar` still formats with the keyword on its own line, also after a where-clause, and the aligned mode still breaks before it.
