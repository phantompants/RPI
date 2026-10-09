import datetime

from src.scheduled_jobs import should_fire

AT_TIME = datetime.time(20, 0)


def test_does_not_fire_before_threshold_time():
    now = datetime.datetime(2026, 7, 9, 19, 59)
    assert should_fire(now, AT_TIME, last_run_date=None, guard_ok=True) is False


def test_fires_after_threshold_time_when_guard_ok_and_not_run_today():
    now = datetime.datetime(2026, 7, 9, 20, 0)
    assert should_fire(now, AT_TIME, last_run_date=None, guard_ok=True) is True


def test_fires_well_after_threshold_time_too():
    now = datetime.datetime(2026, 7, 9, 23, 30)
    assert should_fire(now, AT_TIME, last_run_date=None, guard_ok=True) is True


def test_does_not_refire_same_day():
    now = datetime.datetime(2026, 7, 9, 21, 0)
    already_ran = datetime.date(2026, 7, 9)
    assert should_fire(now, AT_TIME, last_run_date=already_ran, guard_ok=True) is False


def test_fires_again_on_a_new_day():
    now = datetime.datetime(2026, 7, 10, 20, 5)
    ran_yesterday = datetime.date(2026, 7, 9)
    assert should_fire(now, AT_TIME, last_run_date=ran_yesterday, guard_ok=True) is True


def test_does_not_fire_when_guard_fails():
    now = datetime.datetime(2026, 7, 9, 20, 5)
    assert should_fire(now, AT_TIME, last_run_date=None, guard_ok=False) is False
