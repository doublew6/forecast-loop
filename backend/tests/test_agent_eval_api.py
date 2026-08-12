from __future__ import annotations


def test_agent_eval_read_routes_are_available(client) -> None:
    suites = client.get("/api/agent-evals/suites")
    assert suites.status_code == 200
    assert "items" in suites.json()

    experiments = client.get("/api/agent-evals/experiments")
    assert experiments.status_code == 200
    assert experiments.json() == {"items": []}

    bad_cases = client.get("/api/agent-bad-cases")
    assert bad_cases.status_code == 200
    assert bad_cases.json() == {"items": []}
