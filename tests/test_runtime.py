import pytest

from agentchaos.clock import ManualClock
from agentchaos.ids import SeededIdGenerator
from agentchaos.scheduler import DeterministicScheduler


def test_clock_only_moves_when_advanced() -> None:
    clock = ManualClock()
    assert clock.now_ms() == 0
    assert clock.now_ms() == 0
    clock.advance(250)
    assert clock.now_ms() == 250
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        clock.set(100)
    clock.reset()
    assert clock.now_ms() == 0


def test_scheduler_fires_in_due_then_insertion_order_at_event_time() -> None:
    clock = ManualClock()
    scheduler = DeterministicScheduler(clock)
    seen: list[tuple[str, int]] = []
    scheduler.schedule(300, "c", lambda: seen.append(("c", clock.now_ms())))
    scheduler.schedule(100, "a", lambda: seen.append(("a", clock.now_ms())))
    scheduler.schedule(100, "b", lambda: seen.append(("b", clock.now_ms())))

    assert scheduler.run_due() == []
    assert scheduler.advance(99) == []
    assert scheduler.advance(101) == ["a", "b"]
    assert seen == [("a", 100), ("b", 100)]
    assert clock.now_ms() == 200
    assert [e.name for e in scheduler.pending()] == ["c"]
    assert scheduler.advance(1000) == ["c"]
    assert seen[-1] == ("c", 300)
    assert clock.now_ms() == 1200


def test_scheduler_event_can_schedule_a_follow_up() -> None:
    clock = ManualClock()
    scheduler = DeterministicScheduler(clock)

    def first() -> None:
        scheduler.schedule(10, "second", lambda: None)

    scheduler.schedule(10, "first", first)
    assert scheduler.advance(20) == ["first", "second"]


def test_scheduler_clear_drops_pending_events() -> None:
    scheduler = DeterministicScheduler(ManualClock())
    scheduler.schedule(10, "x", lambda: None)
    scheduler.clear()
    assert scheduler.advance(100) == []


def test_seeded_ids_are_reproducible_unique_and_seed_dependent() -> None:
    a = SeededIdGenerator(42)
    b = SeededIdGenerator(42)
    c = SeededIdGenerator(43)
    ids_a = [a.new("ord") for _ in range(200)]
    assert ids_a == [b.new("ord") for _ in range(200)]
    assert ids_a != [c.new("ord") for _ in range(200)]
    assert len(set(ids_a)) == 200
    assert ids_a[0].startswith("ord_")
    a.reseed(42)
    assert a.new("ord") == ids_a[0]
