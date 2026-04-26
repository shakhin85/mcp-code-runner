---
name: schema_dump
description: Render column metadata (name/type/nullable/...) as a fixed-width table.
---

# schema_dump

Useful when exploring a new database via information_schema.

```python
cols = await postgres_x.execute_sql(sql="""
    SELECT column_name AS name, data_type AS type, is_nullable AS nullable
    FROM information_schema.columns
    WHERE table_name = 'orders'
""")
print(skills.schema_dump.render_columns(cols))
```
