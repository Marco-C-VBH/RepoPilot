`no_wrap=True` on a table column has stopped doing anything: the column is squeezed like any other when the table is too wide, and its text wraps.

```python
from rich.console import Console
from rich.table import Table

table = Table(box=None, show_header=False, padding=(0, 1))
table.add_column(no_wrap=True)
table.add_column()
table.add_row("2024-01-01 12:00:00", "a long description that needs to wrap over several lines")
Console(width=40).print(table)
```

```
 2024-01-01          a long description 
 12:00:00            that needs to wrap 
                     over several lines 
```

The timestamp column should keep its full 19 cells and only the second column should be narrowed and wrapped:

```
 2024-01-01 12:00:00  a long            
                      description that  
                      needs to wrap     
                      over several      
                      lines             
```

When the column cannot keep its width at all — the same column declared with `width=8` — its content should be cropped to a single line (`2024-01…`, or `2024-01-` with `overflow="crop"`), but the rest of the text now wraps onto a second line (`12:00:00`) under it. Both halves of the option are lost: the column no longer resists being shrunk, and what is in it is wrapped anyway.
