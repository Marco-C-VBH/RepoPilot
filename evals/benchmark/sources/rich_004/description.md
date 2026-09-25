A closing markup tag is only accepted when it repeats the opening tag word for word: a different word order or a different case is reported as a tag that was never opened.

```python
from rich.console import Console

console = Console()
console.print("[bold red]alert[/red bold] done")
```

```
Traceback (most recent call last):
  ...
rich.errors.MarkupError: closing tag '[/red bold]' at position 15 doesn't match any open tag
```

`[bold red]alert[/bold red] done` prints fine, and so does the shorthand `[/]`. The same error comes from `[bold]shout[/Bold]`, `[bold]a[/BOLD]`, `[red on blue]x[/on blue red]` and `[not bold italic]a[/italic not bold]`, although each closing tag names exactly the style that is open; `Text.from_markup` raises the same way. Only spellings that differ from the opening tag are affected, and a closing tag for a style that really is not open (`[bold]a[/red]`) is still, correctly, an error.
