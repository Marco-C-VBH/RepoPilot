Shortening a `Text` leaves its length at the old value, so a later `set_length` cuts the text instead of padding it, and styles added afterwards land on the wrong characters.

```python
from rich.text import Text

text = Text("hello world")
text.set_length(5)
print(repr(text.plain), len(text))
text.set_length(8)
print(repr(text.plain), len(text))
```

```
'hello' 11
'he' 11
```

After the first call the text really is `hello`, but `len(text)` still reports 11, and the second call — which should pad `hello` to `hello   ` — removes three more characters instead. The same stale length shows up after `Text.remove_suffix` and `Text.rstrip_end`: `Text("report.txt")` with the suffix removed still has length 10, so `text.append("!", "bold")` records the bold span at offset 10 rather than 6, and `text.stylize("italic", 0, len(text))` on a text trimmed with `rstrip_end` reaches past its end. Lengthening a text (`set_length` on a shorter text, `pad_right`) keeps the length correct; only the operations that trim characters off the end leave it stale.
