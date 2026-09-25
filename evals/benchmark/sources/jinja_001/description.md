A property that raises while a template renders no longer raises.

```python
from jinja2 import Environment


class Order:
    def __init__(self, total, count):
        self.total = total
        self.count = count

    @property
    def average(self):
        return self.total / self.count


Environment().from_string("{{ order.average }}").render(order=Order(10, 0))
```

`render` used to propagate the `ZeroDivisionError` raised by the property. Now it returns `''`: the placeholder renders as if `average` were not an attribute of `order` at all, and with strict undefined handling the failure is reported as an undefined value (`'Order object' has no attribute 'average'`) instead of the division error. Objects that also support item access are worse — the item lookup runs instead, so `{{ row.average }}` on a row object that answers `row["average"]` silently renders whatever that lookup returns. A `KeyError` raised inside a property disappears the same way. `SandboxedEnvironment` behaves identically.

Only a missing attribute should fall back to the item lookup and then to the undefined value; any other error raised while reading the attribute must reach the caller of `render`, as it did before.
