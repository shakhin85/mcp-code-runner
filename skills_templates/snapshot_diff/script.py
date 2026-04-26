def diff(before, after, key):
    """Return {added, removed, changed} for two lists of dicts keyed by `key`.

    `changed` lists keys whose row changed; `added`/`removed` list keys
    appearing only in one snapshot.
    """
    by_b = {r[key]: r for r in before}
    by_a = {r[key]: r for r in after}
    bk, ak = set(by_b), set(by_a)
    return {
        "added": sorted(ak - bk),
        "removed": sorted(bk - ak),
        "changed": sorted(k for k in bk & ak if by_b[k] != by_a[k]),
    }
