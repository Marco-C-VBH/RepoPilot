A `TIMESTAMP '…'` literal is parsed as two unrelated tokens, while `DATE '…'` is still grouped into one.

```python
import sqlparse


def tokens(sql):
    return [str(t) for t in sqlparse.parse(sql)[0].tokens if not t.is_whitespace]


print(tokens("x > DATE '2020-01-01'"))
print(tokens("x > TIMESTAMP '2020-01-01 00:00:00'"))
print(tokens("select timestamp '2020-01-01' + interval '1 day' from t"))
```

```
['x', '>', "DATE '2020-01-01'"]
['x', '>', 'TIMESTAMP', "'2020-01-01 00:00:00'"]
['select', 'timestamp', "'2020-01-01' + interval '1 day'", 'from', 't']
```

The second line should be `['x', '>', "TIMESTAMP '2020-01-01 00:00:00'"]`: the keyword and the string used to form a single typed-literal group, exactly like the `DATE` case on the first line, and the third line should keep `timestamp '2020-01-01'` together as the left operand of the addition (`"timestamp '2020-01-01' + interval '1 day'"`), instead of leaving `timestamp` outside the operation and starting it at the bare string. Anything that walks the tree sees the same split — in a `WHERE` clause, `created > timestamp '2020-01-01 00:00:00'` yields the keyword and the string as two separate plain tokens. `DATE`, `INTERVAL` and the other typed literals are unaffected; only `TIMESTAMP` regressed.
