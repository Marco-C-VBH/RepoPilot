`--help` and `--version` no longer take precedence over an invalid option that appears before them on the command line.

```python
import click

@click.command()
@click.version_option("1.2.3", prog_name="tool")
@click.option("--count", type=int, default=1)
@click.option("--name", required=True)
def cli(count, name):
    print(name * count)
```

```
$ cli --count x --help
Usage: cli [OPTIONS]
Try 'cli --help' for help.

Error: Invalid value for '--count': 'x' is not a valid integer.
$ cli --help --count x
Usage: cli [OPTIONS]

Options:
  --version        Show the version and exit.
  ...
$ cli --count x --version
Error: Invalid value for '--count': 'x' is not a valid integer.
```

Both orders used to print the help (and the version), because the options that must act first are processed before the others regardless of where they appear. Now they only do so when they come first: whatever precedes them is processed first, in command-line order. The same shows in callbacks: an option declared with `is_eager=True` has its callback run after the callbacks of the options that were typed before it, instead of before all of them.
