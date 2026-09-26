`strip_whitespace=True` glues the end of a where-clause to the keyword that follows it.

```python
import sqlparse

print(sqlparse.format("select * from t where a = 1 order by b", strip_whitespace=True))
```

```
select * from t where a = 1order by b
```

The space between the `1` and the order-by is gone, so the output is no longer valid SQL. The same happens before every keyword that can end a where-clause — limit, group by, union, having, and returning:

```
select * from t where a = 1limit 10
select * from t where a = 1group by b
select * from t where a = 1union select 2
select * from t where a = 1having b > 2
delete from t where a = 1returning id
```

A where-clause inside a subquery loses its space too, and so does the closing parenthesis after it:

```
select * from t where a in (select b from u where c = 1order by c)order by b
```

Inputs without a where-clause are formatted correctly, runs of spaces are still collapsed to one, and the whitespace at the very end of the statement is still removed — those parts of the option work as before.
