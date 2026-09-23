When the context has a `token_normalize_func`, a case-sensitive `click.Choice` whose choices are changed by that function rejects every spelling of them, including the declared one.

```python
import click

@click.command(context_settings={"token_normalize_func": lambda t: t.lower()})
@click.option("--level", type=click.Choice(["Debug", "Info"]))
def cli(level):
    print(level)
```

```
$ cli --level Debug
Error: Invalid value for '--level': 'Debug' is not one of 'debug', 'info'.
$ cli --level debug
Error: Invalid value for '--level': 'debug' is not one of 'debug', 'info'.
$ cli --help
Usage: cli [OPTIONS]

Options:
  --level [debug|info]
```

The help text and the error message both list the choices in their normalized form, and yet typing exactly one of the listed values is rejected. The typed value is normalized, but it is being compared against the choices as declared; the same option with `case_sensitive=False` happens to work because case folding hides the difference. Expected: `Debug`, `debug` and `DEBUG` all select the choice, and the command receives the original spelling `Debug`. The same holds for a normalization function that rewrites characters (`dry_run` should select a choice declared as `dry-run`) and for enum choices, where the member name goes through the function like any other choice.
