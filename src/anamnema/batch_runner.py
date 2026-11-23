"""Ordered stage execution over the durable journal, with no invented successful stages."""

from collections.abc import Callable, Mapping

from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import BatchState, Metrics
from anamnema.setup_config import SetupError


class BatchRunner:
    """Operations own their deadlines and verified effects; the runner owns ordered progress."""

    def __init__(
        self, journal: BatchJournal, operations: Mapping[str, Callable[[], Metrics]]
    ) -> None:
        if set(operations) != set(journal.plan.stages):
            raise SetupError("batch operations must match every pinned stage exactly")
        self.journal, self._operations = journal, dict(operations)

    def run(self) -> BatchState:
        """A failure blocks later stages; interrupted effects replay through their own guards."""
        with self.journal.worker() as execution:
            while (stage := execution.begin()) is not None:
                try:
                    metrics = self._operations[stage]()
                    execution.succeed(stage, metrics)
                except KeyboardInterrupt:
                    execution.fail(stage, "interrupted")
                    raise
                except SetupError:
                    execution.fail(stage, "operation_failed")
                    raise
        return self.journal.inspect()
