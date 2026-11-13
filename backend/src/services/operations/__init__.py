"""Operations status (Issue #2768): scheduled job runs, work queues, LLM lanes.

Aggregates only. Nothing in this package reads or returns a user, workspace or
context identifier, an exception message or a traceback.

- :mod:`services.operations.job_status`: the ``scheduled_job_status`` rows
  (registration, run writes) and the overdue rule.
- :mod:`services.operations.backlogs`: the work queue registry and readers.
- :mod:`services.operations.llm_lanes`: the leader's platform LLM lane
  snapshot and the ledger usage per lane.
- :mod:`services.operations.probe`: the opt-in lane probe (platform
  credentials only, :mod:`services.platform_credentials`).
- :mod:`services.operations.status`: the status document the admin route
  returns.
"""
