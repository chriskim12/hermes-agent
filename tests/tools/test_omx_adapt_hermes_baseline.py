from tools.omx_adapt_hermes_baseline import AdaptResult, classify_baseline


def test_classify_running_runtime_with_uninitialized_adapter_is_warn():
    results = [
        AdaptResult(
            command="omx adapt hermes probe --json",
            exit_code=0,
            data={
                "targetRuntime": {
                    "state": "running",
                    "evidence": {"hermesRoot": "/home/ubuntu/.hermes/hermes-agent"},
                }
            },
        ),
        AdaptResult(
            command="omx adapt hermes status --json",
            exit_code=0,
            data={"adapter": {"state": "not-initialized"}},
        ),
        AdaptResult(
            command="omx adapt hermes doctor --json",
            exit_code=0,
            data={
                "issues": [
                    {"code": "adapter_not_initialized"},
                    {"code": "planning_artifacts_missing"},
                ]
            },
        ),
    ]

    classification = classify_baseline(results)

    assert classification == {
        "status": "warn",
        "runtime_state": "running",
        "hermes_root": "/home/ubuntu/.hermes/hermes-agent",
        "adapter_state": "not-initialized",
        "expected_setup_gaps": ["adapter_not_initialized", "planning_artifacts_missing"],
        "unexpected_issues": [],
        "command_failures": [],
    }


def test_classify_runtime_or_doctor_problem_is_fail():
    results = [
        AdaptResult(
            command="omx adapt hermes probe --json",
            exit_code=0,
            data={"targetRuntime": {"state": "missing", "evidence": {}}},
        ),
        AdaptResult(
            command="omx adapt hermes status --json",
            exit_code=0,
            data={"adapter": {"state": "ready"}},
        ),
        AdaptResult(
            command="omx adapt hermes doctor --json",
            exit_code=0,
            data={"issues": [{"code": "gateway_state_missing"}]},
        ),
    ]

    classification = classify_baseline(results)

    assert classification["status"] == "fail"
    assert classification["runtime_state"] == "missing"
    assert classification["unexpected_issues"] == ["gateway_state_missing"]
