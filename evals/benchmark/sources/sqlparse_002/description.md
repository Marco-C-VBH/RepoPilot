Both reindent modes now break a `BETWEEN … AND …` range across two lines: the `AND` that belongs to the range is treated like the boolean `AND` between conditions.

```python
import sqlparse

sql = "select * from t where a between 1 and 2 and b = 3"
print(sqlparse.format(sql, reindent=True))
print(sqlparse.format(sql, reindent_aligned=True))
```

```
select *
from t
where a between 1
  and 2
  and b = 3
```

```
select *
  from t
 where a between 1
   and 2
   and b = 3
```

Expected `where a between 1 and 2` on one line followed by `and b = 3` (aligned: ` where a between 1 and 2` / `   and b = 3`), which is what these options produced before. A second range in the same condition (`and b between 3 and 4`) is split the same way, and so is a range inside a `JOIN … ON` condition with `reindent=True`. Only the `AND` directly after a `BETWEEN` range should stay on the line; the boolean `AND`/`OR` that follow must still start new lines.
