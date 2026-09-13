Functions decorated with `@memoize` return each other's results.

```python
@memoize
def double(x):
    return 2 * x

@memoize
def square(x):
    return x * x

double(3)   # 6
square(3)   # 6  -- expected 9; the body of square never runs
```

The same happens with any two memoized functions whose arguments coincide, in whichever order they are first called. Each memoized function is supposed to remember only the results it computed itself.
