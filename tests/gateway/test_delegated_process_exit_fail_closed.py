from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from gateway.work_state import WorkRecord, WorkStateStore
from tools.process_registry import ProcessRegistry, ProcessSession


def _delegated_record(*, executor_session_id: str = "proc_omx", state: str = "running") -> WorkRecord:
    now = datetime.now(timezone.utc)
    return WorkRecord(
        work_id="CH-358",
        title="Delegated OMX closeout",
        objective="fail closed on raw process exits",
        owner="hermes",
        executor="omx",
        mode="delegated",
        owner_session_id="agent:main:discord:chat:thread",
        executor_session_id=executor_session_id,
        state=state,
        started_at=now,
        last_progress_at=now,
        next_action="Wait for OMX completion signal",
    )


def test_clean_delegated_process_exit_fail_closes_instead_of_finished(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(_delegated_record())

    result = store.fail_close_delegated_process_exit(
        executor_session_id="proc_omx",
        exit_code=0,
    )

    assert result["updated"] is True
    [record] = store.list_records()
    assert record.state == "handoff_needed"
    assert record.state != "finished"
    assert record.usable_outcome == "no_progress_theater"
    assert record.close_disposition == "close"
    assert record.proof == "background_process_exit:0"
    assert record.next_action == "Inspect the OMX run diff before claiming progress"


def test_nonzero_delegated_process_exit_records_runtime_contamination(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(_delegated_record())

    result = store.fail_close_delegated_process_exit(
        executor_session_id="proc_omx",
        exit_code=2,
    )

    assert result["updated"] is True
    [record] = store.list_records()
    assert record.state == "failed"
    assert record.usable_outcome == "runtime_contamination"
    assert record.close_disposition == "close"
    assert record.proof == "background_process_exit:2"


def test_process_exit_does_not_clobber_explicit_closed_usable_outcome(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    record = _delegated_record()
    record.state = "handoff_needed"
    record.usable_outcome = "red_only_partial_handoff"
    record.close_disposition = "close"
    record.proof = "delegated_ingress:red_tests_only"
    store.upsert(record)

    result = store.fail_close_delegated_process_exit(
        executor_session_id="proc_omx",
        exit_code=0,
    )

    assert result["updated"] is False
    [saved] = store.list_records()
    assert saved.usable_outcome == "red_only_partial_handoff"
    assert saved.proof == "delegated_ingress:red_tests_only"


def test_process_registry_move_to_finished_uses_fail_closed_model(monkeypatch, tmp_path):
    import gateway.work_state as work_state

    monkeypatch.setattr(work_state, "get_hermes_home", lambda: tmp_path)
    store = WorkStateStore()
    store.upsert(_delegated_record(executor_session_id="proc_omx"))

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_omx",
        command="omx --madmax --high exec 'do the work'",
        started_at=1.0,
        exited=True,
        exit_code=0,
    )
    registry._running[session.id] = session

    registry._move_to_finished(session)

    [saved] = WorkStateStore().list_records()
    assert saved.state == "handoff_needed"
    assert saved.usable_outcome == "no_progress_theater"
    assert saved.close_disposition == "close"
    assert saved.proof == "background_process_exit:0"


def test_gateway_watcher_exit_path_uses_fail_closed_model(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    import gateway.work_state as work_state

    monkeypatch.setattr(work_state, "get_hermes_home", lambda: tmp_path)
    store = WorkStateStore()
    store.upsert(_delegated_record(executor_session_id="proc_omx"))

    runner = SimpleNamespace()
    gateway_run.GatewayRunner._update_delegated_work_for_process(runner, "proc_omx", 0)

    [saved] = WorkStateStore().list_records()
    assert saved.state == "handoff_needed"
    assert saved.usable_outcome == "no_progress_theater"
    assert saved.close_disposition == "close"
    assert saved.proof == "background_process_exit:0"
