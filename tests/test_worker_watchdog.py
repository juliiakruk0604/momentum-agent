from service import worker_should_restart


def test_worker_watchdog_restarts_stale_worker():
    assert worker_should_restart({"status": "stale"}, 10, 300) is True


def test_worker_watchdog_restarts_worker_that_never_heartbeats():
    assert worker_should_restart({"status": "starting"}, 301, 300) is True


def test_worker_watchdog_allows_startup_grace_and_healthy_worker():
    assert worker_should_restart({"status": "starting"}, 299, 300) is False
    assert worker_should_restart({"status": "healthy"}, 999, 300) is False
    assert worker_should_restart({"status": "degraded"}, 999, 300) is False
