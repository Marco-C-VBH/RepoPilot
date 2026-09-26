`wrap_after` wraps one column too early: a select list that fits the limit exactly is broken before its last item.

```python
import sqlparse

print(sqlparse.format("select aaaa, bbbb, cccc, dddd from t", reindent=True, wrap_after=17))
```

```
select aaaa, bbbb,
       cccc, dddd
from t
```

Expected `select aaaa, bbbb, cccc,` on the first line (that line is 24 characters, but the width counts from the first column, and `aaaa, bbbb, cccc,` is 17 — exactly the limit, which used to be allowed) with only `dddd` wrapped. The same off-by-one shows with `wrap_after=10` on `select a, bb, ccc, dddd from t`: `a, bb,` (6) fits, `a, bb, ccc,` (11) does not, so the expected first line is `select a, bb,` — instead the break comes after `a,`, as if the limit were 9. Function arguments are affected in the same way: `select foo(aaaa, bbbb, cccc, dddd) from t` with `wrap_after=22` used to keep `bbbb, cccc, dddd)` on the second line and now wraps `dddd)` onto a third, and with `wrap_after=17` the break moves from after `cccc,` to after `bbbb,`. Lists that are clearly over or clearly under the limit wrap as before; only inputs that land exactly on the limit changed.
