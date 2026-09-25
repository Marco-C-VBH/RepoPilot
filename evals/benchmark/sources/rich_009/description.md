When a panel has a background style, its title and subtitle are drawn without that background: the words sit in a gap of the terminal's own colour in the middle of the coloured border.

```python
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console(force_terminal=True, color_system="truecolor", width=40)
console.print(
    Panel("Hello, World", style="on blue", title=Text("title", style="red"), subtitle="plain subtitle")
)
```

Captured to a file, the top border line is:

```
\x1b[44m╭─\x1b[0m\x1b[44m──────────────\x1b[0m\x1b[31m title \x1b[0m\x1b[44m───────────────\x1b[0m\x1b[44m─╮\x1b[0m
```

The border segments carry the blue background (`44`) and so does every cell of the body, but ` title ` is emitted with only its own red foreground (`31`), and ` plain subtitle ` on the bottom line has no style at all. Expected: the title keeps its red but on the blue background (`31;44`), and the plain subtitle gets the blue background, so that the whole panel is blue from edge to edge. A `border_style` alone (for example `border_style="green"` with no panel style) is already applied to the title correctly; it is the panel-level style that does not reach it.
