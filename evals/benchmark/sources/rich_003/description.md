A panel with a fixed height and vertical padding loses its bottom padding: the space goes to one more line of content instead.

```python
from rich.console import Console
from rich.panel import Panel

console = Console(width=20)
console.print(Panel("a\nb\nc\nd\ne\nf", padding=(1, 2), height=7))
```

```
╭──────────────────╮
│                  │
│  a               │
│  b               │
│  c               │
│  d               │
╰──────────────────╯
```

The panel is 7 lines tall as asked, and the top padding line is there, but the last line inside the border is `d` rather than the blank line that `padding=(1, 2)` asks for below the content; the content is cut one line later than it should be. Expected inside the border: a blank line, `a`, `b`, `c`, a blank line. With `padding=(2, 1, 1, 1)` the two top lines are right and the one bottom line is missing in the same way, and without a `height=` the padding is complete on both sides.
