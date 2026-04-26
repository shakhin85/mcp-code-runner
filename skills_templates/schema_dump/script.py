def render_columns(columns):
    """Pretty-print a column list-of-dicts as a fixed-width table.

    Each column dict needs at least 'name' and 'type'. Other fields
    (nullable, default, ...) are rendered if present.
    """
    if not columns:
        return "(no columns)"
    keys = list(columns[0].keys())
    widths = {
        k: max(len(str(k)), max(len(str(c.get(k, ""))) for c in columns))
        for k in keys
    }
    header = "  ".join(str(k).ljust(widths[k]) for k in keys)
    sep = "  ".join("-" * widths[k] for k in keys)
    body = "\n".join(
        "  ".join(str(c.get(k, "")).ljust(widths[k]) for k in keys)
        for c in columns
    )
    return f"{header}\n{sep}\n{body}"
