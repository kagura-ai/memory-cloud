"""Operations status (Issue #2768): scheduled job runs, work queues, LLM lanes.

Aggregates only. Nothing in this package reads or returns a user, workspace or
context identifier, an exception message or a traceback.

- :mod:`services.operations.job_status`: the ``scheduled_job_status`` rows
  (registration, run writes) and the overdue rule.
"""
