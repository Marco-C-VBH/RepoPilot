In `--help` output, once a command's docstring contains an indented paragraph, every later paragraph that wraps has its continuation lines indented by the same extra amount.

```python
import click

@click.command(context_settings={"terminal_width": 60})
def cli():
    """Sync the local checkout with the server.

        Note: paths are relative to the checkout root.

    This paragraph is long enough that the formatter has to wrap it onto a second and a third line at the configured width.

    And so is this one, which should look exactly like the previous paragraph in the help output.
    """
```

```
$ cli --help
Usage: cli [OPTIONS]

  Sync the local checkout with the server.

      Note: paths are relative to the checkout root.

  This paragraph is long enough that the formatter has to
      wrap it onto a second and a third line at the
      configured width.

  And so is this one, which should look exactly like the
      previous paragraph in the help output.
```

The first line of each later paragraph starts in the right place, but the wrapped lines that follow it are pushed in by four spaces, the indentation of the note above them; they should line up with the paragraph's first line, as they do when the docstring has no indented paragraph. The same thing happens outside help formatting: `click.wrap_text(text, 30, preserve_paragraphs=True)` on a text with an indented middle paragraph wraps the last paragraph as `Second paragraph that is also` / `    long enough to wrap at a` / `    narrow width.` instead of keeping the continuation lines flush left. With two indented paragraphs the extra indentation adds up.
