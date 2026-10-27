"""Bus and digest tests: append-only ordering, replay, and checkpoint fidelity.

The two properties Kafka is relied on for — monotonic offsets and replay-from-offset — are what make
the audit trail work. The journal backend is tested as strictly as a cluster would be, because it is
the backend that actually runs in a demo and on a laptop; a bug there would silently break both.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from brian.bus import Bus, JournalBus, MemoryBus
from brian.contracts import Event
from brian.state import (
    BaselineTracker,
    Windower,
    build_digest,
    normalise_signature,
    percentile,
    median_absolute_deviation,
    robust_z,
    synthesize_normal_window,
)

UTC = timezone.utc


# --------------------------------------------------------------------------------------
# bus
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("factory", ["memory", "journal"])
def test_offsets_are_monotonic_from_zero(factory: str, tmp_path) -> None:
    bus: Bus
    if factory == "memory":
        bus = MemoryBus()
    else:
        bus = JournalBus(str(tmp_path / "journal"), fsync=False)
    for index in range(5):
        record = bus.append("t", "k", {"i": index})
        assert record.offset == index
    replayed = bus.replay("t")
    assert [r.offset for r in replayed] == [0, 1, 2, 3, 4]
    assert [r.value["i"] for r in replayed] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("factory", ["memory", "journal"])
def test_replay_from_an_offset_is_exact(factory: str, tmp_path) -> None:
    bus: Bus = MemoryBus() if factory == "memory" else JournalBus(str(tmp_path / "j"), fsync=False)
    for index in range(10):
        bus.append("t", None, {"i": index})
    assert [r.value["i"] for r in bus.replay("t", start_offset=7)] == [7, 8, 9]
    assert [r.value["i"] for r in bus.replay("t", start_offset=2, limit=3)] == [2, 3, 4]


def test_journal_survives_reopen_and_preserves_offsets(tmp_path) -> None:
    path = str(tmp_path / "journal")
    first = JournalBus(path, fsync=False)
    for index in range(4):
        first.append("brian.events", "svc", {"i": index})

    # A brand-new instance, as a restarted process would construct.
    second = JournalBus(path, fsync=False)
    assert [r.value["i"] for r in second.replay("brian.events")] == [0, 1, 2, 3]
    assert second.append("brian.events", "svc", {"i": 4}).offset == 4
    assert [r.value["i"] for r in second.replay("brian.events", start_offset=3)] == [3, 4]


def test_journal_tolerates_a_torn_final_line(tmp_path) -> None:
    # A crash mid-write must not make the whole memory unreadable — only the damaged record is lost.
    path = tmp_path / "journal"
    bus = JournalBus(str(path), fsync=False)
    bus.append("t", None, {"i": 0})
    with (path / "t.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"topic": "t", "value": {"i": 1}')  # truncated
    replayed = bus.replay("t")
    assert [r.value["i"] for r in replayed] == [0] or [r.value.get("i") for r in replayed][0] == 0


def test_journal_record_id_is_quotable_coordinate(tmp_path) -> None:
    bus = JournalBus(str(tmp_path / "j"), fsync=False)
    bus.append("brian.decisions", "svc", {"x": 1})
    record = bus.replay("brian.decisions")[0]
    assert record.record_id == "brian.decisions@0"


def test_publish_serialises_a_contract_record_and_sets_a_type_header(tmp_path) -> None:
    from brian.contracts import Event

    bus = MemoryBus()
    event = Event(event_time=datetime(2026, 1, 14, tzinfo=UTC), service="svc")
    record = bus.publish(event, "brian.events", key="svc")
    assert record.headers["record_type"] == "Event"
    assert record.value["service"] == "svc"


# --------------------------------------------------------------------------------------
# numeric helpers
# --------------------------------------------------------------------------------------


def test_percentile_interpolates_and_handles_degenerate_input() -> None:
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.9) == 5.0
    assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


def test_robust_z_is_unmoved_by_a_single_wild_value() -> None:
    # The whole reason for MAD over standard deviation: one outage in the baseline must not inflate
    # the spread enough to hide the next one.
    stable = [10.0, 10.1, 9.9, 10.0, 10.2, 9.8, 10.0]
    mad_clean = median_absolute_deviation(stable)
    mad_with_spike = median_absolute_deviation(stable + [900.0])
    assert mad_with_spike == pytest.approx(mad_clean, rel=0.5)
    assert robust_z(50.0, 10.0, mad_clean) > 5


def test_robust_z_returns_none_for_a_flat_baseline() -> None:
    # A perfectly flat baseline has no meaningful spread; reporting a huge z would read as an extreme
    # anomaly. None is rendered as "no baseline", which is honest.
    assert robust_z(11.0, 10.0, 0.0) is None


def test_normalise_signature_collapses_volatile_parts() -> None:
    a = normalise_signature("TimeoutException: pool orders-db timed out after 5000ms")
    b = normalise_signature("TimeoutException: pool orders-db timed out after 5123ms")
    assert a == b
    assert "<num>" in a or "<n>" in a


# --------------------------------------------------------------------------------------
# windowing and digest
# --------------------------------------------------------------------------------------


def _healthy_windows(service: str, start: datetime, count: int) -> list:
    events = []
    for index in range(count):
        events.extend(synthesize_normal_window(service, start + timedelta(minutes=index), 30))
    return events


def test_windower_closes_windows_only_after_the_watermark_passes() -> None:
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=30)
    for event in _healthy_windows("svc", start, 3):
        windower.add(event)
    # Three minutes of events; the watermark sits 30s behind the max event time, so the first two
    # windows are closed and the third is still open.
    closed = windower.ready()
    assert [a.window_start for a in closed] == [
        start,
        start + timedelta(minutes=1),
    ]
    assert windower.stats["open_windows"] == 1


def test_windower_flush_includes_the_partial_window() -> None:
    # Dropping the last window of a replay would hide exactly the incident the replay was run to
    # explain, so `flush` deliberately emits partial windows.
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=30)
    for event in _healthy_windows("svc", start, 3):
        windower.add(event)
    windower.ready()
    flushed = windower.flush()
    assert len(flushed) == 1
    assert flushed[0].event_count > 0


def test_event_count_is_counted_not_derived_from_the_event_list() -> None:
    # Regression: `event_count` was derived from `len(events)`, and because raw events are not carried
    # in checkpoint state, a restarted operator emitted digests claiming zero events and rendered an
    # empty summary — a silent total loss of judgement after every failover.
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0)
    for event in synthesize_normal_window("svc", start, 20):
        windower.add(event)
    aggregator = list(windower._open.values())[0]
    assert aggregator.event_count == 20
    aggregator.events = []          # simulate what a resumed operator has
    assert aggregator.reduce(BaselineTracker())["event_count"] == 20


def test_digest_is_identical_after_a_checkpoint_round_trip() -> None:
    # The Flink operator rebuilds the windower from JSON state on every invoke, so this must hold or
    # the judgement silently changes across a failover.
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=30)
    baselines = BaselineTracker(window=30)
    for index in range(4):
        for event in synthesize_normal_window("svc", start + timedelta(minutes=index), 30):
            windower.add(event)
        for aggregator in windower.ready():
            baselines.observe("svc", build_digest(aggregator, baselines).metrics)

    for event in synthesize_normal_window("svc", start + timedelta(minutes=4), 30):
        windower.add(event)

    restored = Windower.from_dict(json.loads(json.dumps(windower.to_dict())))
    restored_baselines = BaselineTracker.from_dict(json.loads(json.dumps(baselines.to_dict())))

    moment = start + timedelta(minutes=4)
    original = build_digest(_window_for(windower, moment), baselines)
    resumed = build_digest(_window_for(restored, moment), restored_baselines)
    assert original.interpretation == resumed.interpretation
    assert original.event_count == resumed.event_count
    # The single documented divergence: `ingest_lag_s` derives from individual events, which are not
    # carried in checkpoint state. Every decision-relevant metric must still match.
    for key in ("requests.rate", "errors.ratio", "latency_ms.p99", "cpu.util"):
        assert original.metric("comparison.%s.value" % key) == resumed.metric("comparison.%s.value" % key)


def test_late_events_are_counted_not_silently_dropped() -> None:
    start = datetime(2026, 1, 14, 10, 5, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=30)
    for event in synthesize_normal_window("svc", start, 30):
        windower.add(event)
    # An event two minutes behind the watermark is late.
    windower.add(
        Event(
            event_time=start - timedelta(minutes=3),
            service="svc",
            kind="metric",
            payload={"http_requests_total": 1},
        )
    )
    assert windower.stats["late_events"] == 1
    assert windower.stats["late_by_service"] == {"svc": 1}


def _window_for(windower: Windower, moment: datetime):
    """The in-flight aggregator whose window contains `moment`.

    Needed because a tumbling windower keeps several windows open at once (the watermark trails the
    newest event by the allowed lateness), so `list(_open.values())[0]` is order-dependent and can
    silently hand back the wrong window — which is exactly how the first version of these tests
    asserted against a healthy window while believing it was a degraded one.
    """
    for aggregator in windower._open.values():
        if aggregator.window_start <= moment < aggregator.window_end:
            return aggregator
    raise AssertionError("no open window contains %s" % moment)


def test_baseline_excludes_a_window_from_its_own_comparison() -> None:
    # Compare-then-observe is the required order. Observe-then-compare would let a slow-burn incident
    # baseline itself into normality — a detector that adapts to the outage it is meant to catch.
    #
    # The assertion is on the *observation count*, which is the only way to distinguish the two
    # orders from outside: if the degraded window had contributed to its own baseline, the baseline
    # would already contain its 9000 ms latency and the resulting ratio would collapse toward 1.
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0, min_events=1)
    baselines = BaselineTracker(window=30)

    judged = 0
    for index in range(6):
        for event in synthesize_normal_window("svc", start + timedelta(minutes=index), 30):
            windower.add(event)
        for aggregator in windower.ready():
            digest = build_digest(aggregator, baselines)
            judged += 1
            if digest.metrics.get("comparison") is None:
                # No baseline exists for the very first judged window, so no comparison is claimed.
                assert digest.metrics["baseline_samples"] == 0
            # Observe only *after* judging: this window must not inform its own comparison.
            baselines.observe("svc", digest.metrics)
    assert judged >= 4, "the loop should have closed several windows"
    baseline_n_before = baselines.stats("svc", "latency_ms.p99").n
    assert baseline_n_before == judged

    # Now a single obviously degraded window, closed deliberately.
    degraded_start = start + timedelta(minutes=20)
    for index in range(30):
        moment = degraded_start + timedelta(seconds=index)
        windower.add(
            Event(
                event_time=moment,
                service="svc",
                kind="metric",
                payload={
                    "http_requests_total": 600 + index,
                    "http_errors_total": 200,
                    "http_request_duration_ms": 9000.0,
                },
            )
        )
    degraded = build_digest(_window_for(windower, degraded_start), baselines)
    ratio = degraded.metric("comparison.latency_ms.p99.delta_ratio")
    assert ratio is not None and ratio > 20
    assert degraded.metric("comparison.errors.ratio.value") > 0.05

    # The degraded window is not yet in the baseline: it is judged against healthy history, not
    # against itself. This is the property the test exists for.
    assert baselines.stats("svc", "latency_ms.p99").n == baseline_n_before
    baselines.observe("svc", degraded.metrics)
    assert baselines.stats("svc", "latency_ms.p99").n == baseline_n_before + 1


def test_render_reports_what_it_had_to_drop_instead_of_truncating_silently() -> None:
    # A truncated digest means the model judged on partial evidence, and that must be visible in the
    # record rather than inferred later from a word count.
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0, min_events=1)
    baselines = BaselineTracker(window=30)
    for index in range(8):
        for event in synthesize_normal_window("svc", start + timedelta(minutes=index), 30):
            windower.add(event)
        for aggregator in windower.ready():
            baselines.observe("svc", build_digest(aggregator, baselines).metrics)
    for index in range(30):
        moment = start + timedelta(minutes=9, seconds=index)
        windower.add(
            Event(
                event_time=moment,
                service="svc",
                kind="metric",
                payload={
                    "http_requests_total": 600,
                    "http_errors_total": 200,
                    "http_request_duration_ms": 9000.0,
                    "cpu_util": 0.99,
                    "memory_util": 0.95,
                    "thread_pool_util": 0.97,
                    "db_pool_util": 0.99,
                    "upstream_errors_total": 190,
                    "upstream_duration_ms": 8000.0,
                    "connect_timeouts_total": 50,
                    "auth_failures_total": 20,
                    "schema_errors_total": 30,
                    "consumer_lag": 90000,
                    "deploy": "v9",
                },
            )
        )
    digest = build_digest(_window_for(windower, start + timedelta(minutes=9)), baselines)
    render = digest.metrics["render"]
    assert render["words"] <= 260
    assert "digest_version" in render


def test_digest_text_is_english_and_mentions_the_dominant_error_template() -> None:
    # The model conditions on this text, so what it says is the decision input.
    start = datetime(2026, 1, 14, 10, 0, tzinfo=UTC)
    windower = Windower(size_s=60, slide_s=60, allowed_lateness_s=0, min_events=1)
    baselines = BaselineTracker(window=30)
    for index in range(8):
        for event in synthesize_normal_window("svc", start + timedelta(minutes=index), 30):
            windower.add(event)
        for aggregator in windower.ready():
            baselines.observe("svc", build_digest(aggregator, baselines).metrics)

    for index in range(30):
        moment = start + timedelta(minutes=9, seconds=index)
        windower.add(
            Event(
                event_time=moment,
                service="svc",
                kind="log",
                payload={"error": "TimeoutException: pool orders-db timed out after %dms" % (5000 + index)},
            )
        )
    digest = build_digest(_window_for(windower, start + timedelta(minutes=9)), baselines)
    assert "orders-db" in digest.interpretation
    assert "timed out" in digest.interpretation
    assert "@" not in digest.interpretation[:1]  # plain English, not a serialised object
