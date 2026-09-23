An option that may be given with or without a value (`is_flag=False` together with a `flag_value`) no longer accepts a lone `-` as its value.

```python
import click

@click.command()
@click.option("--log", is_flag=False, flag_value="app.log", type=click.File("w"))
def cli(log):
    print("hello", file=log)
```

```
$ cli --log -
Usage: cli [OPTIONS]
Try 'cli --help' for help.

Error: Got unexpected extra argument (-)
```

The `-` is not taken as the option's value: the option falls back to `app.log` as if no value had been given, and the `-` is left over as a stray argument. The same happens with a plain string option declared the same way (`--out -` gives the fallback instead of `-`), and with the short form `-o -`. A single dash is the usual way to name standard output or standard input and should be accepted as the value, while `--log --help` should keep treating `--help` as the next option rather than as the value.
