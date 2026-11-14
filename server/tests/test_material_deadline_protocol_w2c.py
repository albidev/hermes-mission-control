import threading
import time

import pytest

from invite_rooms.control.dispatcher import (
    CLOCK_MAX_SKEW_MS,
    CLOCK_MAX_UNCERTAINTY_MS,
    EXPIRY_LOSS_MS,
    ClockProbeError,
    local_deadline_from_utc,
    validate_clock_probe,
)
from invite_rooms.pairing_journal import E_DEADLINE, Journal, JournalError


def probe(*, t1, t2, t3, t4, endpoint="endpoint", generation=7, request_id="a" * 32):
    return {
        "op": "material_probe",
        "v": 1,
        "operation": "material_request",
        "nonce": "b" * 32,
        "endpoint_id": endpoint,
        "generation": generation,
        "request_id": request_id,
        "t1_utc_ms": t1,
        "t2_utc_ms": t2,
        "t3_utc_ms": t3,
        "t4_utc_ms": t4,
    }


def test_four_timestamp_probe_uses_full_asymmetric_interval():
    # Sender RTT is 50 ms; receiver clock is 0 ms behind sender.  No
    # midpoint/symmetry assumption is made, so the interval is [-50, 0].
    assert validate_clock_probe(
        probe(t1=1_000, t2=1_000, t3=1_005, t4=1_050),
        endpoint_id="endpoint", generation=7, request_id="a" * 32,
        now_utc_ms=1_050,
    ) == (-50, 0)


def test_probe_accepts_exact_uncertainty_and_skew_boundaries():
    assert validate_clock_probe(
        probe(t1=1_000, t2=1_050, t3=1_050, t4=1_050),
        endpoint_id="endpoint", generation=7, request_id="a" * 32,
        now_utc_ms=1_050,
    ) == (0, 50)


def test_probe_rejects_uncertainty_skew_binding_and_staleness():
    cases = [
        (probe(t1=1_000, t2=1_000, t3=1_000, t4=1_051), 1_051),
        (probe(t1=1_000, t2=1_051, t3=1_051, t4=1_051), 1_051),
        (probe(t1=1_000, t2=1_000, t3=1_000, t4=1_000, generation=8), 1_000),
        (probe(t1=1_000, t2=1_000, t3=1_000, t4=1_000), 2_001),
    ]
    for item, now in cases:
        with pytest.raises(ClockProbeError):
            validate_clock_probe(item, endpoint_id="endpoint", generation=7,
                                 request_id="a" * 32, now_utc_ms=now)


def test_local_conversion_rejects_at_and_below_conservative_loss():
    with pytest.raises(ClockProbeError):
        local_deadline_from_utc(10_000 + EXPIRY_LOSS_MS, utc_ms=10_000, monotonic=4.0)
    with pytest.raises(ClockProbeError):
        local_deadline_from_utc(10_000 + 40, utc_ms=10_000, monotonic=4.0)
    assert local_deadline_from_utc(10_000 + EXPIRY_LOSS_MS + 1,
                                   utc_ms=10_000, monotonic=4.0) == pytest.approx(4.001)


def test_no_monotonic_value_is_part_of_probe_shape():
    value = probe(t1=1_000, t2=1_000, t3=1_000, t4=1_000)
    assert all("mono" not in key and "deadline" not in key for key in value)


def test_material_origin_journal_inherits_deadline_through_real_flock(tmp_path):
    journal = Journal(tmp_path / "journal", anchor=tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def hold_exclusive_lock():
        with journal.transaction():
            entered.set()
            release.wait(2)

    holder = threading.Thread(target=hold_exclusive_lock)
    holder.start()
    assert entered.wait(1)
    deadline = time.monotonic() + 0.04
    try:
        with pytest.raises(JournalError, match=E_DEADLINE):
            journal.record_material_origin(
                endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
                request_id="a" * 32, expires_at_utc_ms=int(time.time() * 1000) + 1_000,
                deadline=deadline)
    finally:
        release.set()
        holder.join(2)
    assert journal.material_origin("endpoint", "lobby", "invite") is None


def test_material_origin_lookup_and_consume_refuse_after_deadline(tmp_path):
    journal = Journal(tmp_path / "journal", anchor=tmp_path)
    origin = journal.record_material_origin(
        endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
        request_id="a" * 32, expires_at_utc_ms=int(time.time() * 1000) + 10_000)
    assert origin["status"] == "pending"
    expired = time.monotonic() - 1
    with pytest.raises(JournalError, match=E_DEADLINE):
        journal.material_origin("endpoint", "lobby", "invite", deadline=expired)
    assert journal.consume_material_origin(
        endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
        request_id="a" * 32, expires_at_utc_ms=origin["expires_at_utc_ms"], deadline=expired) is False
    pending = journal.material_origin("endpoint", "lobby", "invite")
    assert pending is not None and pending["status"] == "pending"


def _pause_real_hash_after_cutoff(monkeypatch, journal, deadline, *, statuses=("pending",)):
    import invite_rooms.pairing_journal as pairing_journal

    entered = threading.Event()
    release = threading.Event()
    original = pairing_journal.record_hash

    def delayed_record_hash(record):
        if (record.get("type") == "material_origin"
                and record.get("data", {}).get("status") in statuses):
            entered.set()
            release.wait(2)
        return original(record)

    monkeypatch.setattr(pairing_journal, "record_hash", delayed_record_hash)
    return entered, release


def test_material_origin_append_checks_inherited_deadline_after_real_hash(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal", anchor=tmp_path)
    deadline = time.monotonic() + 0.04
    entered, release = _pause_real_hash_after_cutoff(monkeypatch, journal, deadline)
    result = []

    def worker():
        try:
            result.append(("ok", journal.record_material_origin(
                endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
                request_id="a" * 32, expires_at_utc_ms=int(time.time() * 1000) + 10_000,
                deadline=deadline)))
        except Exception as exc:  # assertion below preserves the production exception contract
            result.append(("error", exc))

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    time.sleep(0.06)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert result and result[0][0] == "error"
    assert isinstance(result[0][1], JournalError)
    assert str(result[0][1]) == E_DEADLINE

    reopened = Journal(tmp_path / "journal", anchor=tmp_path)
    assert reopened.material_origin("endpoint", "lobby", "invite") is None


def test_material_consume_append_checks_inherited_deadline_after_real_hash(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal", anchor=tmp_path)
    origin = journal.record_material_origin(
        endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
        request_id="a" * 32, expires_at_utc_ms=int(time.time() * 1000) + 10_000)
    deadline = time.monotonic() + 0.04
    entered, release = _pause_real_hash_after_cutoff(monkeypatch, journal, deadline, statuses=("consumed",))
    append_seams = []
    import invite_rooms.pairing_journal as pairing_journal
    original_append = pairing_journal.Txn.append

    def observed_append(txn, kind, data):
        if kind == "material_origin":
            append_seams.append(data["status"])
        return original_append(txn, kind, data)

    monkeypatch.setattr(pairing_journal.Txn, "append", observed_append)
    result = []

    def worker():
        result.append(journal.consume_material_origin(
            endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
            request_id="a" * 32, expires_at_utc_ms=origin["expires_at_utc_ms"], deadline=deadline))

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    time.sleep(0.06)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert result == [False]
    assert append_seams == ["consumed"]

    reopened = Journal(tmp_path / "journal", anchor=tmp_path)
    pending = reopened.material_origin("endpoint", "lobby", "invite")
    assert pending is not None and pending["status"] == "pending"


def test_refused_origin_append_does_not_mutate_retained_transaction_state(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal", anchor=tmp_path)
    entered = threading.Event()
    release = threading.Event()
    import invite_rooms.pairing_journal as pairing_journal

    def delayed_hook(point):
        if point == "before_append:material_origin":
            entered.set()
            release.wait(2)

    monkeypatch.setattr(pairing_journal, "_FAULT", delayed_hook)
    deadline = time.monotonic() + 0.04
    retained = {}

    def worker():
        try:
            with journal.transaction(deadline=deadline) as txn:
                retained["txn"] = txn
                retained["state"] = txn.state
                retained["origins"] = txn.state.material_origins
                txn.append("material_origin", {
                    "endpoint_id": "endpoint", "generation": 7, "lobby_id": "lobby", "invite_id": "invite",
                    "request_id": "a" * 32, "expires_at_utc_ms": int(time.time() * 1000) + 10_000,
                    "status": "pending",
                })
        except JournalError as exc:
            retained["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    time.sleep(0.06)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(retained["error"], JournalError)
    assert str(retained["error"]) == E_DEADLINE
    assert retained["txn"].state is retained["state"]
    assert retained["state"].seq == 0
    assert retained["state"].material_origins is retained["origins"]
    assert retained["origins"] == {}
    assert journal.material_origin("endpoint", "lobby", "invite") is None


def test_refused_consume_does_not_mutate_retained_transaction_state(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "journal", anchor=tmp_path)
    origin = journal.record_material_origin(
        endpoint_id="endpoint", generation=7, lobby_id="lobby", invite_id="invite",
        request_id="a" * 32, expires_at_utc_ms=int(time.time() * 1000) + 10_000)
    entered = threading.Event()
    release = threading.Event()
    import invite_rooms.pairing_journal as pairing_journal

    def delayed_hook(point):
        if point == "before_append:material_origin":
            entered.set()
            release.wait(2)

    monkeypatch.setattr(pairing_journal, "_FAULT", delayed_hook)
    deadline = time.monotonic() + 0.04
    retained = {}

    def worker():
        try:
            with journal.transaction(deadline=deadline) as txn:
                retained["state"] = txn.state
                retained["origins"] = txn.state.material_origins
                txn.append("material_origin", {
                    "endpoint_id": "endpoint", "generation": 7, "lobby_id": "lobby", "invite_id": "invite",
                    "request_id": "a" * 32, "expires_at_utc_ms": origin["expires_at_utc_ms"],
                    "status": "consumed",
                })
        except JournalError as exc:
            retained["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(1)
    time.sleep(0.06)
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(retained["error"], JournalError)
    assert str(retained["error"]) == E_DEADLINE
    assert retained["state"].seq == 1
    assert retained["state"].material_origins is retained["origins"]
    assert retained["origins"][("endpoint", "lobby", "invite")]["status"] == "pending"
    pending = journal.material_origin("endpoint", "lobby", "invite")
    assert pending is not None and pending["status"] == "pending"
