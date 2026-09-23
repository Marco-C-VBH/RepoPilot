A flag option declared with an explicit `type` never turns on: passing the flag leaves the parameter at its default.

```python
import click

@click.command()
@click.option("--verbose", is_flag=True, type=bool)
def cli(verbose):
    print(repr(verbose))
```

```
$ cli --verbose
False
$ cli
False
```

Without the `type=bool` the same declaration prints `True` for `cli --verbose` and `False` for `cli`. The explicit type is redundant here, but it is documented as accepted and used to behave the same; it also makes a difference for a flag whose value should be converted, and `type=click.BOOL` behaves the same way as `type=bool`. With `default=True` and `type=bool`, `cli --verbose` prints `True` instead of `False`, so the flag cannot negate its default either, and with `type=str` passing the flag gives `None` where `True` is expected. Expected: with an explicit `type`, `--verbose` still sets the parameter to the flag's value (the opposite of the default, `True` by default), and the value is then converted by the type as usual.
