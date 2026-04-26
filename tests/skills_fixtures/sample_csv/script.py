import csv

def write_csv(rows, path):
    """Write a list of dicts to a CSV file in the workspace."""
    if not rows:
        return 0
    keys = list(rows[0].keys())
    with open(path, "w") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
