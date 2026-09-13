`dissoc` raises `KeyError` when asked to remove a key that is not in the dictionary, although the docstring says missing keys are ignored.

```
>>> dissoc({'a': 1, 'b': 2, 'c': 3}, 'z')
KeyError: 'z'
```

Oddly, `dissoc({'a': 1}, 'z')` returns `{'a': 1}` as expected, and so does `dissoc({'a': 1, 'b': 2}, 'a', 'b', 'z')`.

Removing keys that are absent should never raise, whatever the size of the dictionary; the result is a copy of the input with the present keys removed and the input itself left untouched.
