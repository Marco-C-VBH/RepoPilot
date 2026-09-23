An option with `prompt=True` and a callable `default` shows the function object in the prompt, and accepting the default hands the function object to the command.

```python
import click

def guess_name():
    return "world"

@click.command()
@click.option("--name", prompt=True, default=guess_name)
@click.option("--count", prompt=True, default=lambda: 3, type=int)
def cli(name, count):
    print(f"Hello, {name}! ({count})")
```

```
$ cli --count 2
Name [<function guess_name at 0x7f88f6ddd6c0>]:
Hello, <function guess_name at 0x7f88f6ddd6c0>! (2)
$ cli --name Ada
Count [<function <lambda> at 0x7f88f6a80cc0>]:
Traceback (most recent call last):
  ...
TypeError: int() argument must be a string, a bytes-like object or a real number, not 'function'
```

The first prompt should read `Name [world]:` and pressing Enter should give `world`; the second should read `Count [3]:` and give `3`. The same callable defaults are evaluated correctly when the option is not prompted for (`cli --name x --count 2`, or the same declarations without `prompt=True`), and typing an explicit answer at the prompt still works. A boolean flag declared with `prompt=True` and `default=lambda: False` is affected too: it prompts with `[Y/n]` instead of `[y/N]`.
