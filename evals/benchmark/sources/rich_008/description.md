Printing a container that holds the same object more than once shows the object only the first time; every later occurrence is printed as `...`, as if it were a reference cycle.

```python
from rich.console import Console

console = Console()
shared = {"id": 1}
console.print([shared, shared])
console.print({"a": shared, "b": shared})
row = (1, 2)
console.print([row, row, [3]])
```

```
[{'id': 1}, ...]
{'a': {'id': 1}, 'b': ...}
[(1, 2), ..., [3]]
```

Expected `[{'id': 1}, {'id': 1}]`, `{'a': {'id': 1}, 'b': {'id': 1}}` and `[(1, 2), (1, 2), [3]]`: a repeated reference is not a cycle and used to be printed in full each time, while a real cycle (`cycle = [1]; cycle.append(cycle)`) is still shown as `[1, ...]`. It happens for lists, tuples, dicts and sets alike, and also when the repeated object is nested deeper in the structure.
