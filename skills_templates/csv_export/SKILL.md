---
name: csv_export
description: Write list-of-dicts rows to CSV inside the session workspace.
---

# csv_export

Use after a SQL query to dump rows for the next step.

```python
rows = await postgres_x.execute_sql(sql="SELECT id, total FROM orders LIMIT 100")
n = skills.csv_export.write_rows(rows, "orders.csv")
```
