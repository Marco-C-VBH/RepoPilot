Once a template file has been deleted, asking for it again raises `FileNotFoundError` instead of `TemplateNotFound`.

```python
import os
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("templates"))   # auto_reload is on by default
env.get_template("page.html").render()
os.remove("templates/page.html")
env.get_template("page.html")
```

```
FileNotFoundError: [Errno 2] No such file or directory: 'templates/page.html'
```

The second `get_template` used to raise `TemplateNotFound: page.html`, like a template that never existed still does. It only happens for a template that was loaded before its file went away; a fresh environment raises `TemplateNotFound` for the same name. Everything that relies on that exception is affected: with a `ChoiceLoader` whose second loader (a `DictLoader`) also provides `page.html`, the fallback was used after the file was removed and now the `FileNotFoundError` escapes; `select_template(["page.html", "other.html"])` used to move on to `other.html` and to raise `TemplatesNotFound` when nothing is left, and now fails with the `FileNotFoundError` as well. With `auto_reload=False` the cached template keeps rendering after the file is removed, as before.
