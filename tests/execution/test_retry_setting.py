"""max_retries: how often run-plan retries a failed task, kept in the task's record."""

from orchestrator.execution.retry import retry_problem


def failed(attempt=1, max_retries=None, failed_before=0):
    record = {"task_id": "T1", "status": "failed", "attempt": attempt, "branch": "b",
              "previous_attempts": [{"attempt": n, "status": "failed"}
                                    for n in range(1, failed_before + 1)]}
    if max_retries is not None:
        record["max_retries"] = max_retries
    return record


def test_the_default_is_one_retry():
    assert retry_problem(failed()) is None
    assert "already been retried once" in retry_problem(failed(2, failed_before=1))


def test_three_retries_mean_four_attempts():
    for attempt in (1, 2, 3):
        assert retry_problem(failed(attempt, 3, failed_before=attempt - 1)) is None
    assert "already been retried 3 times" in retry_problem(failed(4, 3, failed_before=3))


def test_no_retries():
    assert "max_retries is 0" in retry_problem(failed(max_retries=0))


def test_a_bad_value_in_an_old_record_falls_back_to_the_default():
    assert retry_problem(failed(max_retries="many")) is None
    assert "already been retried once" in retry_problem(failed(2, "many", failed_before=1))
