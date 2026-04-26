import csv


def write_rows(rows, path):
    """Write a list of dicts to a CSV inside the workspace.

    Returns the number of rows written. Empty input creates a 0-byte file.
    """
    if not rows:
        with open(path, "w") as f:
            pass
        return 0
    keys = list(rows[0].keys())
    with open(path, "w") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
