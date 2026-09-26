`sqlparse.split` no longer ends a stored procedure that contains a while-loop: every statement after it is merged into the procedure.

```python
import sqlparse

script = """\
CREATE PROCEDURE countdown(IN n INT)
BEGIN
  WHILE n > 0 DO
    SET n = n - 1;
  END WHILE;
END;
SELECT 1;
SELECT 2;"""
for statement in sqlparse.split(script):
    print(repr(statement))
```

```
'CREATE PROCEDURE countdown(IN n INT)\nBEGIN\n  WHILE n > 0 DO\n    SET n = n - 1;\n  END WHILE;\nEND;\nSELECT 1;\nSELECT 2;'
```

Expected three statements: the procedure, then the two selects. Two such procedures in one script come back as one statement as well. The same procedure body with an if-block instead of the loop is split correctly:

```
CREATE PROCEDURE p()
BEGIN
  IF n > 0 THEN
    SET n = n - 1;
  END IF;
END;
SELECT 1;
```

gives two statements, as it should — and a while-loop nested inside such an if-block shows the problem again.
