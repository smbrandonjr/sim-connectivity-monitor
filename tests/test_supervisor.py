from sim_monitor.core.supervisor import LADDER, RecoveryAction, Supervisor


def test_ladder_escalation_order():
    sup = Supervisor(backoff_base=10, backoff_max=300)
    now = 0.0
    actions = [sup.on_failure(now).action for _ in range(4)]
    assert actions == list(LADDER)


def test_backoff_doubles_and_caps():
    sup = Supervisor(backoff_base=10, backoff_max=300)
    waits = [sup.on_failure(0.0).not_before for _ in range(6)]
    # 10, 20, 40, 80 then parked at parked_interval (300)
    assert waits[:4] == [10, 20, 40, 80]
    assert waits[4] == 300
    assert waits[5] == 300


def test_parked_after_ladder_keeps_gentle_reconnect():
    sup = Supervisor()
    for _ in range(6):
        planned = sup.on_failure(0.0)
    assert sup.parked
    assert planned.action is RecoveryAction.RECONNECT


def test_due_respects_backoff():
    sup = Supervisor(backoff_base=10)
    sup.on_failure(100.0)
    assert sup.due(105.0) is None
    planned = sup.due(110.0)
    assert planned is not None
    assert planned.action is RecoveryAction.RECONNECT
    sup.consume()
    assert sup.due(999.0) is None  # consumed, nothing planned until next failure


def test_stable_connection_resets_ladder():
    sup = Supervisor(stable_seconds=600)
    sup.on_failure(0.0)
    sup.on_failure(0.0)
    assert sup.failures == 2
    sup.on_connected(1000.0)
    assert sup.failures == 2  # not yet stable
    sup.on_connected(1700.0)
    assert sup.failures == 0  # 700s stable -> reset
    # next failure starts at rung one again
    assert sup.on_failure(2000.0).action is RecoveryAction.RECONNECT


def test_brief_connection_does_not_reset():
    sup = Supervisor(stable_seconds=600)
    sup.on_failure(0.0)
    sup.on_connected(100.0)
    sup.on_connected(200.0)  # only 100s stable
    assert sup.failures == 1
    assert sup.on_failure(300.0).action is RecoveryAction.MODEM_DISABLE_ENABLE
