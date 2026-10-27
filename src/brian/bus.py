"""The memory: an append-only log behind one interface, with three interchangeable backends.

Kafka is the production memory. The `Bus` interface exists because the *contract* that
makes the trail traceable is not "we use Kafka" — it is:

    1. append-only, monotonically offset per topic
    2. replayable from any offset, in order, indefinitely
    3. the same record bytes whatever the transport

So there are three backends that all honour it:

    KafkaBus     a real cluster (kafka-python). Production.
    JournalBus   one append-only JSONL file per topic. Local, survives restart, replayable.
    MemoryBus    in-process, for tests.

`BRIAN_BUS_BACKEND=auto` (the default) tries Kafka and falls back to the journal, so the
same pipeline code runs in a demo, in CI, and against a cluster without a branch anywhere
above this module. The journal is not a stub: it is what makes "can I still explain what
the system decided last Tuesday?" answerable with no infrastructure at all.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from .config import KafkaSettings, Settings
from .topics import Topic, topic_purpose


# --------------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------------


@dataclass
class Record:
    """One immutable log entry.

    `offset` is the record's position in its topic and is the unit of replay and of
    checkpointing: a consumer that records the offset of the last record it processed can
    resume exactly, with no duplicates and no gaps.
    """

    topic: str
    key: Optional[str]
    value: Dict[str, Any]
    offset: int
    timestamp: float
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        """Human-quotable coordinate: `topic@offset` is how a decision is cited."""
        return "%s@%d" % (self.topic, self.offset)


class BusError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# interface
# --------------------------------------------------------------------------------------


class Bus:
    """Append-only, offset-addressable log over a set of topics."""

    backend = "abstract"

    def append(self, topic: str, key: Optional[str], value: Dict[str, Any],
               headers: Optional[Dict[str, str]] = None) -> Record:
        raise NotImplementedError

    def replay(self, topic: str, start_offset: int = 0, limit: Optional[int] = None) -> List[Record]:
        """All records from `start_offset`, in order. The audit/rebuild entry point."""
        raise NotImplementedError

    def tail(self, topic: str, start_offset: int = 0, stop: Optional[Callable[[], bool]] = None,
             timeout_s: float = 1.0) -> Iterator[Record]:
        """Follow a topic from `start_offset` until `stop()` returns True.

        `stop` is polled between reads, so a caller can bound a follow loop by wall clock
        or by a record budget without the bus knowing anything about the caller's policy.
        """
        offset = start_offset
        while stop is None or not stop():
            batch = self.replay(topic, offset, limit=1000)
            if not batch:
                time.sleep(timeout_s)
                continue
            for record in batch:
                offset = record.offset + 1
                yield record

    def latest_offset(self, topic: str) -> int:
        records = self.replay(topic)
        return records[-1].offset if records else -1

    def count(self, topic: str) -> int:
        return len(self.replay(topic))

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.backend}

    def close(self) -> None:
        pass

    #: convenience: append a contract record and print it when echoing
    def publish(self, record: Any, topic: str, key: Optional[str] = None,
                settings: Optional[Settings] = None) -> Record:
        from .contracts import dumps

        payload = json.loads(dumps(record))
        headers = {"record_type": type(record).__name__}
        if settings is not None and settings.echo:
            print("[%s] key=%s %s" % (topic, key, json.dumps(payload, ensure_ascii=False)), flush=True)
        return self.append(topic, key, payload, headers=headers)


# --------------------------------------------------------------------------------------
# in-process
# --------------------------------------------------------------------------------------


class MemoryBus(Bus):
    """Volatile log. Correct semantics, no durability — for tests and one-shot runs."""

    backend = "memory"

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._log: Dict[str, List[Record]] = {}
        self._lock = threading.RLock()
        self._clock = clock

    def append(self, topic: str, key: Optional[str], value: Dict[str, Any],
               headers: Optional[Dict[str, str]] = None) -> Record:
        with self._lock:
            entries = self._log.setdefault(topic, [])
            record = Record(
                topic=topic,
                key=key,
                value=value,
                offset=len(entries),
                timestamp=self._clock(),
                headers=dict(headers or {}),
            )
            entries.append(record)
            return record

    def replay(self, topic: str, start_offset: int = 0, limit: Optional[int] = None) -> List[Record]:
        with self._lock:
            entries = list(self._log.get(topic, []))
        window = entries[max(0, start_offset):]
        return window if limit is None else window[:limit]

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "topics": {t: len(v) for t, v in sorted(self._log.items())},
        }


# --------------------------------------------------------------------------------------
# journal: append-only JSONL, one file per topic
# --------------------------------------------------------------------------------------


class JournalBus(Bus):
    """Append-only JSONL journal: the same guarantees as Kafka, on a local disk.

    One file per topic, one JSON object per line, and the line number *is* the offset.
    That is not a coincidence — it is what lets `replay(topic, offset)` mean the same thing
    here as on a cluster, so a decision made offline can be re-derived byte-for-byte later.

    Records are flushed and fsynced on every append. The durability is the point: this is
    the memory, and a memory that loses its tail on a crash cannot be used to explain an
    incident after one.
    """

    backend = "journal"

    def __init__(self, directory: str, fsync: bool = True, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._clock = clock
        self._lock = threading.RLock()
        self._counts: Dict[str, int] = {}

    def _path(self, topic: str) -> Path:
        # Topic names contain dots (`brian.events`); keep them as the file stem verbatim so
        # the file name is the topic name and can be grepped for.
        return self.dir / ("%s.jsonl" % topic)

    def _count(self, topic: str) -> int:
        if topic not in self._counts:
            path = self._path(topic)
            count = 0
            if path.is_file():
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.strip():
                            count += 1
            self._counts[topic] = count
        return self._counts[topic]

    def append(self, topic: str, key: Optional[str], value: Dict[str, Any],
               headers: Optional[Dict[str, str]] = None) -> Record:
        with self._lock:
            offset = self._count(topic)
            stamp = self._clock()
            line = json.dumps(
                {
                    "topic": topic,
                    "key": key,
                    "value": value,
                    "headers": dict(headers or {}),
                    "timestamp": stamp,
                    "offset": offset,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            path = self._path(topic)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                if self._fsync:
                    os.fsync(fh.fileno())
            self._counts[topic] = offset + 1
            return Record(topic=topic, key=key, value=value, offset=offset,
                          timestamp=stamp, headers=dict(headers or {}))

    def replay(self, topic: str, start_offset: int = 0, limit: Optional[int] = None) -> List[Record]:
        path = self._path(topic)
        out: List[Record] = []
        if not path.is_file():
            return out
        with self._lock, path.open("r", encoding="utf-8", errors="replace") as fh:
            for position, line in enumerate(fh):
                if position < start_offset:
                    continue
                if limit is not None and len(out) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line (crash mid-write). Tolerate it: a single damaged
                    # record must not make the whole memory unreadable.
                    continue
                out.append(
                    Record(
                        topic=raw.get("topic", topic),
                        key=raw.get("key"),
                        value=raw.get("value") or {},
                        offset=int(raw.get("offset", position)),
                        timestamp=float(raw.get("timestamp", 0.0)),
                        headers=dict(raw.get("headers") or {}),
                    )
                )
        return out

    def describe(self) -> Dict[str, Any]:
        topics = {}
        for path in sorted(self.dir.glob("*.jsonl")):
            name = path.name[: -len(".jsonl")]
            topics[name] = self._count(name)
        return {"backend": self.backend, "dir": str(self.dir), "topics": topics}

    def reset(self, topic: Optional[str] = None) -> None:
        """Delete the journal. Only ever called explicitly by `brian reset`."""
        with self._lock:
            if topic is None:
                for path in self.dir.glob("*.jsonl"):
                    path.unlink()
                self._counts.clear()
            else:
                self._path(topic).unlink(missing_ok=True)
                self._counts.pop(topic, None)


# --------------------------------------------------------------------------------------
# kafka
# --------------------------------------------------------------------------------------


class KafkaBus(Bus):
    """A real cluster via kafka-python.

    Records are read with `auto_offset_reset="earliest"` and decoded into the same
    `Record` shape the other backends use, so nothing above this class can tell which
    backend it is talking to.

    `replay` reads a bounded slice rather than the whole topic: the offset is still the
    coordinate, but a consumer that asks for 100 records from offset 5,000 pays for 100
    records, not 5,100. `max_replay_records` caps a single call so an unbounded `replay`
    on a large topic cannot exhaust memory.
    """

    backend = "kafka"

    def __init__(self, bootstrap: str, settings: KafkaSettings) -> None:
        try:
            from kafka import KafkaConsumer, KafkaProducer  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise BusError(
                "kafka-python is not installed; `pip install kafka-python` or set "
                "BRIAN_BUS_BACKEND=journal"
            ) from exc

        self.bootstrap = bootstrap
        self.settings = settings
        self._KafkaConsumer = KafkaConsumer
        self._producer = KafkaProducer(
            bootstrap_servers=bootstrap.split(","),
            client_id=settings.client_id,
            acks=settings.acks,
            linger_ms=settings.linger_ms,
            request_timeout_ms=settings.request_timeout_ms,
            key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
            value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
            retries=3,
        )

    def _consumer(self, topic: str, offset: int = 0, limit: Optional[int] = None):
        return self._KafkaConsumer(
            topic,
            bootstrap_servers=self.bootstrap.split(","),
            client_id=self.settings.client_id,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            group_id=None,          # assignment-free read: we address by offset, not by group
            consumer_timeout_ms=800,
            key_deserializer=lambda k: k.decode("utf-8", errors="replace") if k else None,
            value_deserializer=lambda v: json.loads(v.decode("utf-8", errors="replace")),
        )

    def append(self, topic: str, key: Optional[str], value: Dict[str, Any],
               headers: Optional[Dict[str, str]] = None) -> Record:
        kafka_headers = [(k, v.encode("utf-8")) for k, v in (headers or {}).items()]
        meta = self._producer.send(topic, key=key, value=value, headers=kafka_headers).get(
            timeout=self.settings.request_timeout_ms / 1000.0
        )
        return Record(topic=topic, key=key, value=value, offset=meta.offset,
                      timestamp=time.time(), headers=dict(headers or {}))

    def replay(self, topic: str, start_offset: int = 0, limit: Optional[int] = None) -> List[Record]:
        cap = limit if limit is not None else self.settings.max_replay_records
        consumer = self._consumer(topic, start_offset, cap)
        out: List[Record] = []
        try:
            from kafka import TopicPartition  # type: ignore

            partitions = consumer.partitions_for_topic(topic) or set()
            if not partitions:
                return out
            tps = [TopicPartition(topic, p) for p in sorted(partitions)]
            consumer.assign(tps)
            for tp in tps:
                # Per-partition offsets are independent; `start_offset` addresses the
                # partition's own log, which is what a rebuild needs.
                consumer.seek(tp, start_offset)
            for message in consumer:
                out.append(
                    Record(
                        topic=message.topic,
                        key=message.key,
                        value=message.value if isinstance(message.value, dict) else {"raw": message.value},
                        offset=message.offset,
                        timestamp=(message.timestamp or 0) / 1000.0,
                        headers={
                            (h[0] if isinstance(h, tuple) else h): (
                                h[1].decode("utf-8", errors="replace") if isinstance(h, tuple) and h[1] else ""
                            )
                            for h in (message.headers or [])
                        },
                    )
                )
                if len(out) >= cap:
                    break
        finally:
            consumer.close()
        # Kafka interleaves partitions; replay order must be deterministic for a rebuild,
        # so the caller gets a stable (timestamp, offset) order rather than broker arrival.
        out.sort(key=lambda r: (r.timestamp, r.offset))
        return out

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.backend, "bootstrap": self.bootstrap}

    def close(self) -> None:
        try:
            self._producer.flush(timeout=5)
            self._producer.close(timeout=5)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------------------


def _kafka_reachable(bootstrap: str, timeout_s: float = 2.0) -> bool:
    """Cheap TCP probe. Avoids a 30 s kafka-python retry storm when nothing is listening."""
    import socket

    host, _, port = bootstrap.split(",")[0].partition(":")
    try:
        with socket.create_connection((host, int(port or "9092")), timeout=timeout_s):
            return True
    except OSError:
        return False


def open_bus(settings: Settings, verbose: bool = True) -> Bus:
    """Open the configured bus, honouring `auto` fallback.

    The fallback is announced on stderr rather than swallowed: silently judging without the
    cluster would be the worst outcome, because the trail would look complete while missing
    everything another process wrote.
    """
    backend = (settings.kafka.backend or "auto").lower()

    if backend == "memory":
        return MemoryBus()
    if backend == "journal":
        return JournalBus(settings.kafka.journal_dir)
    if backend == "kafka":
        return KafkaBus(settings.kafka.bootstrap, settings.kafka)
    if backend != "auto":
        raise BusError("unknown BRIAN_BUS_BACKEND=%r (auto|kafka|journal|memory)" % backend)

    try:
        bus = KafkaBus(settings.kafka.bootstrap, settings.kafka)
        return bus
    except Exception as exc:
        if _kafka_reachable(settings.kafka.bootstrap):
            raise
        if verbose:
            print(
                "[brian] Kafka at %s is not reachable (%s); using the append-only journal at %s.\n"
                "        Set BRIAN_BUS_BACKEND=kafka to fail instead of falling back."
                % (settings.kafka.bootstrap, type(exc).__name__, settings.kafka.journal_dir),
                flush=True,
            )
        return JournalBus(settings.kafka.journal_dir)


def ensure_topics(bus: Bus, settings: Settings, partitions: int = 3, replication: int = 1) -> List[str]:
    """Create the topic set. On a journal/memory backend this is a no-op that reports intent.

    On Kafka the topics are created explicitly so partitioning is chosen by the design
    (keyed by service) rather than by a producer's default.
    """
    names = [settings.topic(t) for t in _all_topic_constants()]
    if bus.backend == "kafka":
        from kafka.admin import KafkaAdminClient, NewTopic  # type: ignore

        admin = KafkaAdminClient(bootstrap_servers=settings.kafka.bootstrap.split(","),
                                 client_id=settings.kafka.client_id)
        try:
            existing = set(admin.list_topics())
            new = [
                NewTopic(name=n, num_partitions=partitions, replication_factor=replication)
                for n in names
                if n not in existing
            ]
            if new:
                admin.create_topics(new_topics=new, validate_only=False)
        finally:
            admin.close()
    return names


def _all_topic_constants() -> Sequence[str]:
    return (
        Topic.EVENTS,
        Topic.FEATURES,
        Topic.DECISIONS,
        Topic.ALERTS,
        Topic.RCA,
        Topic.OUTCOMES,
        Topic.DLQ,
    )


def topic_report(prefix: str) -> List[Dict[str, str]]:
    return [{"topic": "%s.%s" % (prefix, t), "purpose": topic_purpose(t)} for t in _all_topic_constants()]
