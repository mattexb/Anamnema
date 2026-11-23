"""Archive one prepared window, then recover its pinned order without a native file dependency."""

from anamnema.archive_models import Record, Snapshot
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import Metrics
from anamnema.batch_prepare import PreparedInputs
from anamnema.processing import Window, prepare
from anamnema.setup_config import SetupError
from anamnema.window_store import WindowStore


class ArchiveWindow:
    """No raw-payload deletion until the archive and durable stage success are both verified."""

    def __init__(
        self,
        inputs: PreparedInputs,
        instance: str,
        identity: str,
        archive: MongoArchive,
        windows: WindowStore,
    ) -> None:
        if windows.archive is not archive:
            raise SetupError("batch window references must use the same archive")
        self.inputs, self.instance, self.identity, self.archive, self.windows = (
            inputs,
            instance,
            identity,
            archive,
            windows,
        )
        self.manifest = inputs.manifest(instance, identity)

    def archive_input(self) -> Metrics:
        """One verified receipt and captured-order reference precede downstream model work."""
        payload = (
            self.inputs.path(self.instance, self.identity).parent / self.inputs.policy.payload_file
        )
        if not payload.exists() and not payload.is_symlink():
            window = self.load()
            return {
                "verified": True,
                "records": len(window.records) + 1,
                "inserted": 0,
                "already_present": len(window.records) + 1,
                "recovered_from_archive": True,
            }
        window = self.inputs.load(self.instance, self.identity)
        records = (*window.records, window.boundary)
        snapshot = Snapshot(
            self.inputs.path(self.instance, self.identity).parent / self.inputs.policy.payload_file,
            self.manifest.payload_sha256,
            records,
            0,
            0,
            window.boundary.kind == "closing",
        )
        report = import_snapshot(snapshot, self.archive)
        self.windows.save(window)
        inserted, already = report["inserted"], report["already_present"]
        if type(inserted) is not int or type(already) is not int:
            raise SetupError("archive returned invalid verified progress counters")
        return {
            "verified": True,
            "records": len(records),
            "inserted": inserted,
            "already_present": already,
            "recovered_from_archive": False,
        }

    def load(self) -> Window:
        """Reconstruct only the manifest's exact owned ids; never scan by timestamps or lines."""
        reference = self.windows.load(
            self.instance, self.manifest.session, "claude", self.manifest.window
        )
        if (
            reference is None
            or reference.boundary != self.manifest.records[-1]
            or reference.records != self.manifest.records[:-1]
        ):
            raise SetupError("archived batch window reference is absent or differs")
        records: list[Record] = []
        size = self.archive.settings.batch_size
        for start in range(0, len(reference.records), size):
            records.extend(
                self.archive.restore(
                    self.instance, "conversation", reference.records[start : start + size]
                )
            )
        boundary = self.archive.restore(
            self.instance, self.manifest.boundary_kind, (reference.boundary,)
        )[0]
        snapshot = Snapshot(
            self.inputs.path(self.instance, self.identity),
            self.manifest.raw_sha256,
            (*records, boundary),
            0,
            0,
            boundary.kind == "closing",
        )
        found = prepare(snapshot)
        if (
            len(found.windows) != 1
            or found.pending
            or found.repeats
            or str(found.windows[0].report()["key"]) != self.manifest.window
            or boundary.session != self.manifest.session
        ):
            raise SetupError("archived batch window scope or order differs")
        return found.windows[0]

    def retire_payload(self, journal: BatchJournal) -> bool:
        """Verify durable success and the archive before removing only the local raw payload."""
        journal.verify_input()
        state = journal.inspect()
        stages = [stage for stage in state.stages if stage.name == "archive"]
        if (
            state.plan.instance != self.instance
            or state.plan.session != self.manifest.session
            or state.plan.input_path != self.inputs.path(self.instance, self.identity)
            or state.plan.input_digest != self.manifest.identity
            or len(stages) != 1
            or not stages[0].complete
        ):
            raise SetupError("batch archive success is not durably recorded; payload retained")
        self.load()
        return self.inputs.remove_payload(self.instance, self.identity)
