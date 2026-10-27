"""Flink integration: the production transport for the same judgement pipeline.

Importing this package requires nothing beyond the standard library — `apache-flink` is imported
inside `job.build_job()`, so a machine without PyFlink can still import `brian`, run the replay
path, and evaluate the detector.
"""
from .job import FLINK_JOB_NAME, CollectorBus, TopicProducer, build_job

__all__ = ["build_job", "CollectorBus", "TopicProducer", "FLINK_JOB_NAME"]
