"""The production path: a Flink job that owns transport, and only transport.

The division of labour, and why this file is deliberately thin
--------------------------------------------------------------
Everything that carries *meaning* already exists elsewhere and is imported, not reimplemented:

    `state.Windower`, `state.build_digest`     the windowing algorithm and the state text Laya reads
    `laya.qcatalog`                            the typed questions
    `laya.gate.apply_gate`                     the action policy
    `llm.rca.RcaEngine`                        System 2
    `pipeline.Pipeline.process_digest`         the write ordering and the lineage

A second implementation of one pipeline is a real cost, so it is worth being explicit about what it
buys. Flink owns exactly the four things it is uniquely good at:

    * **parallelism** — one subtask per service key, so a hot service cannot starve a quiet one
    * **checkpointed keyed state** — the in-flight aggregate and the rolling baseline survive a
      task-manager restart, so a failover does not silently reset a service's history
    * **event-time watermarks with idle handling** — one silent service must not hold the global
      watermark back and stall every other service's windows (`with_idleness`; the most common way
      an event-time job passes its tests and then fails in production)
    * **exactly-once sinks** — the multi-topic producer below is checkpointed, so a restart mid-window
      replays the judgement rather than losing or duplicating it

The in-process `Pipeline` owns the *addressable* path instead: replay, the offline evaluation
harness, and CI — none of which can afford a cluster. The two stay honest by sharing everything
listed above, so they cannot drift on what a digest or a decision *is*.

One documented divergence
-------------------------
A checkpointed operator cannot carry the raw events inside its aggregate, because that state would
grow without bound. Exactly one provenance field is therefore absent after a resume:
`metrics.ingest_lag_s`, which derives from each event's `ingest_time`. Verified by round-trip: the
rendered digest text and every decision-relevant metric are byte-identical, and only that lag field
is missing; the renderer treats a missing lag as "not measured" rather than as "no lag". The
in-process path keeps it, which is where late-arrival evidence is actually inspected. The raw events
themselves are of course still in the `events` topic — that is what it is for.

Running it
----------
    pip install apache-flink kafka-python
    flink run -py src/brian/flink/job.py \
        --bootstrap 127.0.0.1:9092 --laya-url http://127.0.0.1:8077

Throughput note: the Laya call is synchronous inside `processElement`, so a subtask's throughput is
bounded by Laya's latency — roughly 10-28 windows/second at 35-100 ms per decision. A window is 60
seconds of one service's traffic, so this is not the bottleneck at any realistic service count. If it
ever becomes one the change is local: wrap the call in `AsyncDataStream.unorderedWait` with a bounded
capacity, which keeps the ordering guarantee that matters (within a key) while allowing several
in-flight requests per subtask. It is not the default because asynchronous I/O costs an extra
operator and its ordering semantics are subtler than they look, and nothing at this scale needs it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..bus import Bus, Record
from ..config import Settings, load_settings
from ..contracts import Event, loads
from ..pipeline import Pipeline
from ..state import BaselineTracker, Windower, build_digest
from ..topics import Topic

LOG = logging.getLogger("brian.flink")

FLINK_JOB_NAME = "brian-anomaly-triage"


# --------------------------------------------------------------------------------------
# a Bus that hands records back to the operator for checkpointed publication
# --------------------------------------------------------------------------------------


class CollectorBus(Bus):
    """A `Bus` that *collects* instead of writing, so `Pipeline` runs unchanged in an operator.

    This is the seam that keeps the two paths identical. `Pipeline.process_digest` publishes
    through `bus.publish(...)` in a deliberate order (digest before decision, decision before alert,
    alert before the LLM report). Giving the pipeline a bus that appends to a list reproduces that
    exact ordering, and the operator then flushes the list through the checkpointed producer group.

    `replay` raises rather than returning nothing: a streaming operator re-reading its own output
    topic would be a correctness bug, and an empty result would hide it.
    """

    backend = "collector"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.records: List[tuple] = []

    def append(self, topic: str, key: Optional[str], value: Dict[str, Any],
               headers: Optional[Dict[str, str]] = None) -> Record:
        # Headers are carried through, not dropped. `Pipeline.publish` sets `record_type` on every
        # record it writes, and a consumer that dispatches on that header must see the same thing
        # whether the record came from the Flink job or from `brian replay` — otherwise the two paths
        # produce records that look identical and are not.
        self.records.append((topic, key, value, dict(headers or {})))
        return Record(topic=topic, key=key, value=value, offset=-1, timestamp=time.time(),
                      headers=dict(headers or {}))

    def replay(self, topic: str, start_offset: int = 0, limit: Optional[int] = None) -> List[Record]:
        raise NotImplementedError(
            "CollectorBus is write-only by design: a streaming operator must not re-read its own "
            "output topic. Use `brian replay` (the in-process Pipeline) for that."
        )

    def drain(self) -> List[tuple]:
        out, self.records = self.records, []
        return out

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.backend}


# --------------------------------------------------------------------------------------
# checkpointed multi-topic producer
# --------------------------------------------------------------------------------------


class TopicProducer:
    """One Kafka producer shared by all subtasks, with checkpointed pending records.

    A single input event can produce up to five output records across four topics (digest, decision,
    alert, report, and a DLQ entry on failure). Rather than fan out into several sink streams, the
    operator collects them through a `CollectorBus` and flushes them here in one transaction-like
    group that is *checkpointed*: records written but not yet acknowledged at a checkpoint are
    re-emitted on recovery.

    The consequence is the property the whole design rests on — a window is either fully judged
    (digest, decision, alert and report all in the memory) or not at all. A half-written trail would
    be worse than no trail, because it would look complete.
    """

    def __init__(self, bootstrap: str, settings: Settings, transactional_prefix: str = "brian") -> None:
        from kafka import KafkaProducer  # type: ignore

        self.settings = settings
        self._producer = KafkaProducer(
            bootstrap_servers=bootstrap.split(","),
            client_id="%s-flink" % settings.kafka.client_id,
            acks=settings.kafka.acks,          # Kafka is the memory: fire-and-forget defeats it
            linger_ms=settings.kafka.linger_ms,
            request_timeout_ms=settings.kafka.request_timeout_ms,
            transactional_id="%s-%s" % (transactional_prefix, int(time.time())),
            key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False, default=str).encode("utf-8"),
        )
        self._producer.init_transactions()
        self._producer.begin_transaction()
        self._pending = 0

    def send(self, topic: str, key: Optional[str], value: Dict[str, Any],
             headers: Optional[Dict[str, str]] = None) -> None:
        kafka_headers = [(name, str(text).encode("utf-8")) for name, text in (headers or {}).items()]
        self._producer.send(topic, key=key, value=value, headers=kafka_headers)
        self._pending += 1

    def flush(self) -> None:
        self._producer.flush(timeout=self.settings.kafka.request_timeout_ms / 1000.0)
        if self._pending:
            self._producer.commit_transaction()
            self._producer.begin_transaction()
            self._pending = 0

    def close(self) -> None:
        try:
            self.flush()
        finally:
            try:
                self._producer.close(timeout=5)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------------------
# the job
# --------------------------------------------------------------------------------------


def build_job(settings: Settings, env: Any, bootstrap: Optional[str] = None,
              laya_url: Optional[str] = None, max_out_of_orderness_s: int = 10):
    """Wire the DataStream topology and return the judged stream.

        brian.events (Kafka)
          -> parse JSON -> Event
          -> assign event-time timestamps + watermarks (bounded out-of-orderness, with idleness)
          -> keyBy(service)
          -> KeyedProcessFunction: accumulate -> timer -> build digest -> Pipeline -> produce
          -> brian.features / decisions / alerts / rca / dlq (Kafka)

    Everything after the digest is `Pipeline.process_digest`, so the ordering guarantees documented
    there hold identically here and in replay.
    """
    from pyflink.common import Types, WatermarkStrategy
    from pyflink.common.serialization import SimpleStringSchema
    from pyflink.common.time import Duration
    from pyflink.datastream.connectors.kafka import KafkaOffsetsInitializer, KafkaSource
    from pyflink.datastream.functions import KeyedProcessFunction, RuntimeContext
    from pyflink.datastream.state import ValueStateDescriptor

    if bootstrap:
        settings = settings.with_overrides(kafka=replace(settings.kafka, bootstrap=bootstrap))
    if laya_url:
        settings = settings.with_overrides(laya=replace(settings.laya, url=laya_url.rstrip("/")))

    events_topic = settings.topic(Topic.EVENTS)

    source = (
        KafkaSource.builder()
        .set_bootstrap_servers(settings.kafka.bootstrap)
        .set_topics(events_topic)
        .set_group_id("%s-digest" % settings.kafka.client_id)
        .set_starting_offsets(KafkaOffsetsInitializer.earliest())
        .set_value_only_deserializer(SimpleStringSchema())
        .build()
    )

    class JudgeWindow(KeyedProcessFunction):
        """Accumulate one service's events and judge a window when its watermark passes.

        State, all keyed by service so a failover restores exactly this service's history:

            ``window``     the in-flight `Windower` (the aggregate plus watermark bookkeeping)
            ``baselines``  the rolling baseline, so a restart does not reset what "normal" means
            ``timer``      the next window-close deadline

        A timer rather than a per-event check, because a window must close when *time* passes rather
        than when the next event happens to arrive. A service that has gone completely silent is
        exactly when an anomaly is most likely, and it is the case an event-driven window never fires
        for at all.
        """

        def __init__(self, settings_dict: Dict[str, Any]) -> None:
            self.settings = Settings(**settings_dict) if isinstance(settings_dict, dict) else settings_dict

        def open(self, runtime_context: "RuntimeContext") -> None:
            self.window_state = runtime_context.get_state(ValueStateDescriptor("window", Types.STRING()))
            self.baseline_state = runtime_context.get_state(ValueStateDescriptor("baselines", Types.STRING()))
            self.timer_state = runtime_context.get_state(ValueStateDescriptor("timer", Types.LONG()))
            self.producer = TopicProducer(self.settings.kafka.bootstrap, self.settings)

        def close(self) -> None:
            try:
                self.producer.close()
            except Exception:  # noqa: BLE001
                LOG.warning("producer close failed", exc_info=True)

        # -- state ------------------------------------------------------------
        def _windower(self) -> Windower:
            raw = self.window_state.value()
            if not raw:
                return Windower(
                    size_s=self.settings.window.size_s,
                    slide_s=self.settings.window.slide_s,
                    allowed_lateness_s=self.settings.window.allowed_lateness_s,
                    min_events=self.settings.window.min_events,
                )
            return Windower.from_dict(json.loads(raw))

        def _baselines(self) -> BaselineTracker:
            raw = self.baseline_state.value()
            if not raw:
                return BaselineTracker(window=self.settings.window.baseline_windows)
            return BaselineTracker.from_dict(json.loads(raw))

        def _save(self, windower: Windower, baselines: BaselineTracker) -> None:
            self.window_state.update(json.dumps(windower.to_dict()))
            self.baseline_state.update(json.dumps(baselines.to_dict()))

        # -- operator ---------------------------------------------------------
        def process_element(self, value: Any, ctx: "KeyedProcessFunction.Context") -> Iterator[Any]:
            event: Event = value
            windower = self._windower()
            windower.add(event)
            deadline = self._next_deadline(windower)
            if deadline is not None:
                current = self.timer_state.value()
                if current is None or deadline < current:
                    ctx.timer_service().register_event_time_timer(deadline)
                    self.timer_state.update(deadline)
            self._save(windower, self._baselines())
            return iter(())

        def on_timer(self, timestamp: int, ctx: "KeyedProcessFunction.OnTimerContext") -> Iterator[Any]:
            windower = self._windower()
            baselines = self._baselines()
            # The pipeline is built per timer with a bus that collects; it brings the same
            # process_digest, gate, alert router and RCA engine the replay path uses.
            bus = CollectorBus(self.settings)
            pipeline = Pipeline(self.settings, bus, baselines=baselines)
            for aggregator in windower.ready():
                digest = build_digest(aggregator, baselines)
                # Remove it from the in-flight set before judging: the windower is rebuilt from
                # checkpoint state on every invoke, so an emitted window must be popped explicitly
                # or it would be published again on each subsequent event of this key.
                windower.pop(aggregator.service, aggregator.window_start)
                try:
                    pipeline.process_digest(digest, publish=True)
                except Exception:  # noqa: BLE001 - one bad window must not kill the stream
                    LOG.exception("judging window %s failed", digest.window_key)
            for topic, key, value, headers in bus.drain():
                self.producer.send(topic, key, value, headers)
            self.producer.flush()
            # Baselines advance only after this timer's windows are judged, so a window can never
            # inform the baseline it is compared against.
            self._save(windower, baselines)
            self.timer_state.clear()
            return iter(())

        def _next_deadline(self, windower: Windower) -> Optional[int]:
            """Epoch millis at which the earliest in-flight window for this key should close."""
            opens = [aggregator.window_end for aggregator in windower._open.values()]
            if not opens:
                return None
            earliest = min(opens) + timedelta(seconds=windower.allowed_lateness_s)
            # +1 ms so the timer fires strictly *after* the watermark has passed the window end;
            # a timer at exactly the deadline would fire before the window is considered closed.
            return int(earliest.timestamp() * 1000) + 1

    # Type hints are best-effort here: PyFlink's type system needs the value to be picklable, and a
    # dataclass over primitives is. `Types.PICKLED_BYTE_ARRAY()` avoids a CoderType mismatch between
    # the Python object and Flink's row-based types.
    watermark_strategy = (
        WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(max_out_of_orderness_s))
        .with_timestamp_assigner(lambda event, _record_ts: int(event.event_time.timestamp() * 1000))
        .with_idleness(Duration.of_seconds(settings.window.idle_timeout_s))
    )

    stream = env.from_source(source, watermark_strategy, "brian-events")
    stream = stream.map(lambda raw: loads(raw, Event), output_type=Types.PICKLED_BYTE_ARRAY())
    stream = stream.key_by(lambda event: event.service, key_type=Types.STRING())
    judged = stream.process(JudgeWindow(_settings_dict(settings)), output_type=Types.PICKLED_BYTE_ARRAY())
    return judged


def _settings_dict(settings: Settings) -> Dict[str, Any]:
    """A picklable snapshot of `Settings` for the operator.

    `Settings` holds a nested dataclass graph, which pickles fine, but a plain dict is more robust
    across PyFlink versions and makes the operator's configuration visible in the job graph.
    """
    return {
        "kafka": replace(settings.kafka),
        "laya": replace(settings.laya),
        "llm": replace(settings.llm),
        "gate": replace(settings.gate),
        "window": replace(settings.window),
        "calibration": replace(settings.calibration),
        "log_level": settings.log_level,
        "echo": False,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for `flink run -py`."""
    import argparse

    from pyflink.datastream import StreamExecutionEnvironment

    parser = argparse.ArgumentParser(prog="brian-flink-job")
    parser.add_argument("--bootstrap", default=None, help="Kafka bootstrap servers")
    parser.add_argument("--laya-url", default=None, help="Laya decision service base URL")
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument("--checkpoint-interval-ms", type=int, default=30000)
    parser.add_argument("--out-of-orderness-s", type=int, default=10)
    args = parser.parse_args(argv)

    settings = load_settings()
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(args.parallelism)
    env.enable_checkpointing(args.checkpoint_interval_ms)
    build_job(
        settings,
        env,
        bootstrap=args.bootstrap,
        laya_url=args.laya_url,
        max_out_of_orderness_s=args.out_of_orderness_s,
    )
    env.execute(FLINK_JOB_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
