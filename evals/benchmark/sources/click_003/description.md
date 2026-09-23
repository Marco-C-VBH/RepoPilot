Numeric ranges declared with an open bound (`min_open=True` or `max_open=True`) accept the excluded bound, and with `clamp=True` they clamp onto it.

```python
import click

@click.command()
@click.option("--ratio", type=click.FloatRange(0, 1, min_open=True, max_open=True))
@click.option("--level", type=click.IntRange(0, 5, min_open=True, max_open=True, clamp=True))
def cli(ratio, level):
    print(ratio, level)
```

```
$ cli --ratio 0 --level 0
0.0 0
$ cli --ratio 1 --level 9
1.0 5
```

Both invocations should behave differently. `0` and `1` are outside `0<x<1` and should be rejected with `0.0 is not in the range 0<x<1.`, the same way `-1` is; the help text and the error message already describe the range with `<`, so only the check disagrees with them. With clamping, a value at or beyond an open bound should land on the nearest value inside the range: `--level 0` and `--level -3` should give `1`, `--level 5` and `--level 9` should give `4`. Ranges declared with closed bounds (the default) still behave correctly: `click.IntRange(0, 5, clamp=True)` gives `0` and `5` at the edges.
