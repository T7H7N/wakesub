"""Evaluate conditions and advance subscriptions by one polling cycle."""

import logging
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .adapters import Adapter, Delivery, DeliveryError, DeliveryUncertain
from .store import Store


LOGGER = logging.getLogger("wakesub")


@dataclass
class Cycle:
    attempted: int = 0
    fired: int = 0
    expired: int = 0
    failed: int = 0


def condition_met(record, records, now):
    if record.kind == "at":
        return now >= float(record.value)
    if record.kind == "after":
        parent = records.get(record.value)
        return parent is not None and parent.stage == "fired"
    try:
        details = Path(record.value).stat()
    except OSError:
        return False
    return stat.S_ISREG(details.st_mode) and (record.kind == "file" or details.st_size > 0)


def finish(store, record, cycle, stage, clock, error=None):
    record.stage = stage
    record.in_flight = False
    record.last_error = error
    record.finished_at = clock()
    store.save(record)
    if stage == "failed":
        cycle.failed += 1
        LOGGER.error("subscription %s failed after %d attempt(s): %s", record.id, record.attempts, error)
    elif stage == "expired":
        cycle.expired += 1
    else:
        cycle.fired += 1


def attempt(store, record, adapter, cycle, max_attempts, clock):
    record.attempts += 1
    record.in_flight = True
    store.save(record)
    cycle.attempted += 1
    message = record.say
    if record.event == "expired":
        message = f"[wakesub] Subscription expired before the condition was observed.\n\nOriginal message:\n{message}"
    delivery = Delivery(record.id, record.target, record.event, message)
    try:
        adapter.deliver(delivery)
    except DeliveryError as exc:
        reason = " ".join(str(exc).split())[:200] or "delivery failed"
        if record.attempts >= max_attempts:
            finish(store, record, cycle, "failed", clock, reason)
        else:
            record.in_flight = False
            record.last_error = reason
            store.save(record)
    except DeliveryUncertain as exc:
        reason = " ".join(str(exc).split())[:200] or "delivery outcome is unknown"
        finish(store, record, cycle, "failed", clock, reason)
    except Exception:
        finish(store, record, cycle, "failed", clock, "unexpected adapter error; delivery outcome is unknown")
    else:
        finish(store, record, cycle, record.event, clock)


def run_once(
    store: Store, adapter: Adapter, *, budget=5, target_interval=30, max_attempts=3, clock=time.time
):
    """Poll the store once. The clock returns seconds since the Unix epoch."""
    if budget < 1 or target_interval < 0 or max_attempts < 1:
        raise ValueError("invalid cycle limits")
    cycle = Cycle()
    with store.locked():
        records = store.load()
        attempt_times = store.load_attempt_times()
        for record in sorted(records.values(), key=lambda item: (item.created_at, item.id)):
            if record.stage != "open":
                continue
            if record.in_flight:
                finish(store, record, cycle, "failed", clock, "interrupted attempt; delivery outcome is unknown")
                continue
            now = clock()
            if record.event is None:
                if record.expires_at is not None and now >= record.expires_at:
                    record.event = "expired"
                    if record.quiet_expire:
                        finish(store, record, cycle, "expired", clock)
                        continue
                elif condition_met(record, records, now):
                    record.event = "fired"
                else:
                    continue
                store.save(record)
            if record.attempts >= max_attempts:
                finish(store, record, cycle, "failed", clock, record.last_error or "attempt limit reached")
                continue
            last_attempt = attempt_times.get(record.target)
            if cycle.attempted >= budget or (
                target_interval and last_attempt is not None and now - last_attempt < target_interval
            ):
                continue
            attempt_times[record.target] = clock()
            store.save_attempt_times(attempt_times)
            attempt(store, record, adapter, cycle, max_attempts, clock)
    return cycle
