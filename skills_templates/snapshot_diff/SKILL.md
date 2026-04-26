---
name: snapshot_diff
description: Diff two row-lists by key; return added/removed/changed.
---

# snapshot_diff

Use before and after a fix to prove what actually changed.

```python
diff = skills.snapshot_diff.diff(rows_before, rows_after, key="id")
print(diff)
```
