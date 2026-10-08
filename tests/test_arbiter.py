from elliproxy.arbiter import LimitArbiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_lowest_active_limit_wins():
    a = LimitArbiter(clock=Clock())
    assert a.effective() is None
    a.set("a", 7000, True)
    a.set("b", 5000, True)
    assert a.effective() == 5000
    a.set("b", 5000, False)
    assert a.effective() == 7000


def test_limit_with_duration_ends():
    clock = Clock()
    a = LimitArbiter(clock=clock)
    a.set("a", 4200, True, duration=60)
    assert a.next_deadline() == 1060
    clock.now = 1059
    assert a.expire() == [] and a.effective() == 4200
    clock.now = 1060
    assert a.expire() == ["a"]
    assert a.effective() is None and a.next_deadline() is None


def test_lost_manager_gets_failsafe_for_its_duration():
    clock = Clock()
    a = LimitArbiter(clock=clock)
    assert not a.lost("never-wrote", 3000, 7200)
    a.set("a", 6000, True)
    assert a.lost("a", 3000, 7200)
    assert not a.lost("a", 3000, 7200)  # already in failsafe
    assert a.effective() == 3000
    clock.now += 7200
    a.expire()
    assert a.effective() is None and "a" not in a.limits


def test_new_limit_ends_failsafe_and_state_survives_restart(tmp_path):
    clock = Clock()
    path = tmp_path / "limits.json"
    a = LimitArbiter(path, clock=clock)
    a.set("a", 6000, True)
    a.lost("a", 3000, 7200)
    b = LimitArbiter(path, clock=clock)
    assert b.effective() == 3000 and b.limits["a"].failsafe
    b.set("a", 5000, True)
    assert b.effective() == 5000 and not b.limits["a"].failsafe
