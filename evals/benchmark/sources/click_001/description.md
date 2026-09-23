With token normalization turned on, an option whose declared name contains capital letters cannot be given on the command line at all.

```python
import click

@click.command(context_settings={"token_normalize_func": lambda t: t.lower()})
@click.option("--Greeting", default="hi")
@click.option("-N", "--Name", default="world")
def cli(greeting, name):
    click.echo(f"{greeting} {name}")
```

```
$ cli --greeting hello --name click
Error: No such option: --greeting
$ cli --Greeting hello --Name click
Error: No such option: --greeting
$ cli -n click
Error: No such option: -n
```

Every spelling is rejected, including the one that matches the declaration exactly, and the same happens with a flag declared as `--Shout/--Whisper`. Options declared entirely in lower case work in any case the user types (`--FOO` still finds `--foo`), which is what the normalization function is for; it should let a declaration with capitals be typed as `--greeting`, `--GREETING` or `--Greeting` alike, and `-n` or `-N` for the short form.
