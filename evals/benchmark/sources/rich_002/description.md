A hyperlink nested inside another hyperlink is printed with the outer URL: the inner link never replaces the one already in effect.

```python
from rich.console import Console

console = Console(force_terminal=True, color_system="truecolor", width=80)
console.print(
    "[link=https://outer.example]outer [link=https://inner.example]inner[/link] outer[/link]"
)
```

Run with output captured to a file, the OSC 8 sequences around the three runs of text carry these URLs (link ids removed):

```
outer  -> https://outer.example
inner  -> https://outer.example
 outer -> https://outer.example
```

The middle run should point at `https://inner.example`. The same happens without markup: `Text.stylize("link https://first.example", 0, 13)` followed by `Text.stylize("link https://second.example", 4, 7)` on `one two three` prints `two` with the first URL, although a style applied later normally wins over an earlier one, and it does for colours and attributes: `[red]a [blue]b[/blue] c[/red]` prints `b` in blue as expected. Only the link is stuck with whichever URL was set first.
