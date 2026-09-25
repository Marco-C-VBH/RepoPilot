A hyperlink decoded from ANSI text does not stop where the terminal sequence closed it: the text after the link is printed as part of the link, and an HTML export drops the link altogether.

```python
from rich.console import Console
from rich.text import Text

console = Console(record=True, force_terminal=True, color_system="truecolor", width=40)
text = Text.from_ansi("plain \x1b[1mbold\x1b[0m \x1b]8;;https://example.com\x1b\\linked\x1b]8;;\x1b\\ end")
console.print(text)
print(console.export_html(inline_styles=True).count("href="))
```

Written to a file, the printed line opens the link twice — once around `linked`, as expected, and again around ` end`, which was outside the link in the input — and the export contains no `href=` at all (expected: one). The decoded spans look right (`linked` carries the link, ` end` carries an empty style), yet `text.get_style_at_offset(console, 18)` for a character of ` end` compares equal to the linked style at offset 12 even though it carries no link. Building the same text by hand with `Text.stylize("link https://example.com", 11, 17)` behaves correctly, so the problem is specific to styles produced while decoding.
