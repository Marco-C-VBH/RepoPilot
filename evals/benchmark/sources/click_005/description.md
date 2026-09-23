The descriptions in the options list of `--help` can run past the terminal width; every other part of the help text stays within it.

```python
import click

@click.command(context_settings={"terminal_width": 80})
@click.option("--name", help=" ".join(["wordy"] * 16))
@click.option("--a-considerably-longer-option-name", help="short")
def cli(name, a_considerably_longer_option_name):
    """A command whose description is long enough to be wrapped by the help formatter at the terminal width."""
```

```
$ cli --help | awk '{ print length($0) "  " $0 }'
20  Usage: cli [OPTIONS]
0
80    A command whose description is long enough to be wrapped by the help formatter
24    at the terminal width.
0
8   Options:
81    --name TEXT                     wordy wordy wordy wordy wordy wordy wordy wordy
81                                    wordy wordy wordy wordy wordy wordy wordy wordy
42    --a-considerably-longer-option-name TEXT
39                                    short
61    --help                          Show this message and exit.
```

The command description wraps so that no line is longer than 80 characters, but the `--name` description is wrapped too generously and produces 81-character lines, which a real 80-column terminal then breaks in the middle of the last word. The expected output puts seven `wordy` per line (75 characters) and continues on a third line; whatever the word lengths, no line of the options list should exceed the configured width. The same happens with any `terminal_width` and with the default terminal size.
