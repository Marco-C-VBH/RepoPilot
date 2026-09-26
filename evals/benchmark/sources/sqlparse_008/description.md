Reindenting a `CASE` expression glues the `ELSE` branch onto the last `WHEN` line.

```python
import sqlparse

sql = "select case when a then 1 when b then 2 else 3 end from t"
print(sqlparse.format(sql, reindent=True))
```

```
select case
           when a then 1
           when b then 2 else 3
       end
from t
```

Expected `else 3` on its own line under the `when` lines, which is how it used to come out:

```
select case
           when a then 1
           when b then 2
           else 3
       end
from t
```

`reindent_aligned=True` shows the same thing (`when b then 2 else 3` on one line instead of `else 3` aligned under the `when` lines), and a `CASE` with a single `WHEN` followed by `ELSE` (`case when a then 1 else 3 end as x`) is affected too. A `CASE` without an `ELSE` is formatted exactly as before, so only the `ELSE` branch lost its line.
