from code_runner.metrics import SANDBOX_MEDIAN_BYTES, summarize_servers


def _ev(server, tool, nbytes):
    return {"kind": "tool_call", "server": server, "tool": tool, "bytes": nbytes}


def test_server_verdicts():
    events = (
        [_ev("big", "fetch", 20_000)] * 3
        + [_ev("small", "get", 100)] * 2
        + [_ev("mssql", "execute_sql", 500)] * 4
        + [_ev("pg", "execute_sql", SANDBOX_MEDIAN_BYTES)] * 2
        + [{"kind": "execute_code"}]
    )
    rows = {r["server"]: r for r in summarize_servers(events)}
    assert len(rows) == 4
    assert rows["big"]["verdict"] == "sandbox"
    assert rows["small"]["verdict"] == "direct"
    assert rows["pg"]["verdict"] == "sandbox"
    assert rows["mssql"] == {
        "server": "mssql", "calls": 4, "median_bytes": 500, "p90_bytes": 500,
        "verdict": "direct-sql",
    }
