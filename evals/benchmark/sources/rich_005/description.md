In an expanded table whose columns have ratios, a column with a small ratio can be made narrower than its own padding, and its content disappears.

```python
from rich.console import Console
from rich.table import Table

table = Table(expand=True, box=None, show_header=False)
table.add_column(ratio=1)
table.add_column(ratio=1)
table.add_column(ratio=10)
table.add_row("a", "b", "cccccccccccccccc")
Console(width=14).print(table)
```

```
     ccccccc… 
```

`a` and `b` are gone: the two ratio-1 columns are 2 cells wide, which their padding alone uses up, so nothing of the text is left to show. Until recently this printed ` a  b  ccccc… ` — a ratio column was never squeezed below the width needed for one character plus its padding (3 cells here), and the wide column absorbed the difference. The same happens at width 20 (` a  b  ccccccccccc… ` expected) and with `padding=(0, 2)`, where a ratio-1 column next to a ratio-30 one ends up narrower than the four cells of padding.
