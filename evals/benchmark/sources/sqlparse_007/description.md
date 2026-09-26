Removing comments and whitespace in the same call leaves runs of spaces where the comments were.

```python
import sqlparse

sql = "select a, /* c */ b from t  where x = 1"
print(repr(sqlparse.format(sql, strip_comments=True, strip_whitespace=True)))
print(repr(sqlparse.format("select /* c */ a from t", strip_comments=True, strip_whitespace=True)))
```

```
'select a,   b from t where x = 1'
'select   a from t'
```

Expected `'select a, b from t where x = 1'` and `'select a from t'`, which is what the same two options gave before — and what `strip_whitespace=True` still gives on input that has no comments (the double space before `where` is collapsed as it should be). `reindent=True` together with `strip_comments=True` is hit too: `select a /* c */ from t where /* d */ x = 1` comes out with `where   x = 1`, and `select a, -- c\n b from t` is now split onto two lines (`select a,` / ` b`) instead of `select a, b`. Each option on its own behaves normally; it is the combination that leaves the leftovers.
