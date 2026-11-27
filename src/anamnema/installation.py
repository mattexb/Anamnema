"""First logged-in-user installation, reusing each component's ownership/recovery boundary."""

import http.client
import os
import platform
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from anamnema.action_hook import action_binding, action_hook_policy
from anamnema.archive_models import digest
from anamnema.batch_input import input_digest
from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import BatchPlan, Metrics, batch_policy
from anamnema.batch_runner import BatchRunner
from anamnema.briefing_refresh import RefreshDeployment
from anamnema.capture_install import capture_installation
from anamnema.capture_queue_config import capture_deployment, capture_queue_policy
from anamnema.claude_install import (
    ClaudeHookInstaller,
    ContinuityHooks,
    claude_hook_policy,
    continuity_bindings,
)
from anamnema.continuity_config import continuity_config
from anamnema.continuity_state import source_text
from anamnema.database_install import DatabaseInstaller, DatabaseTargets, database_policy
from anamnema.graph_store import connect_graph
from anamnema.hook_merge import HookBinding
from anamnema.install_profile import InstallProfile
from anamnema.judge_config import JudgeConfig
from anamnema.memory_config import load_memory
from anamnema.native_commands import NativeProbe, NativeRunner
from anamnema.nightly import nightly_config
from anamnema.nightly_install import NightlyInstaller, nightly_timer_policy, render_nightly
from anamnema.readiness import ServiceNotReady, wait_ready
from anamnema.recall_hook import recall_binding, recall_deployment, recall_policy
from anamnema.runtime_install import RuntimeInstaller
from anamnema.service_config import load_service
from anamnema.setup_config import SetupError
from anamnema.snapshot_policy import producer_policy, worker_config
from anamnema.user_services import UserServiceInstaller
from anamnema.worker_unit import WorkerUnit, worker_unit_policy


class NativeInstaller:
    """A failed stage retains its journal and stops dependent stages; no data rollback."""

    def __init__(self, profile: InstallProfile, profile_path: Path) -> None:
        if not profile_path.is_absolute():
            raise SetupError("installation profile path must be absolute")
        if platform.system() != "Linux":
            raise SetupError("native installation is supported on Debian 13 Linux only")
        self.profile, self.profile_path = profile, profile_path
        self.runner = NativeRunner(profile.runtime.runtime.cwd, profile.native_commands)
        self.runtime = RuntimeInstaller(profile.runtime, self.runner)
        self.setup, self.archive = self.runtime.setup, self.runtime.archive
        self.memory = load_memory(profile.memory_file)
        self.service = load_service(profile.memory_service_file)
        targets = DatabaseTargets(
            mongo_port=self.setup.services.archive.port,
            neo4j_port=self.setup.services.graph.port,
            archive_database=self.archive.database,
            archive_entry=self.archive.keyring_entry,
            archive_field=self.archive.keyring_field,
            graph_entry=self.memory.keyring_entry,
            graph_user_field=self.memory.user_field,
            graph_password_field=self.memory.password_field,
        )
        self.databases = DatabaseInstaller(
            database_policy(profile.database_policy_file),
            targets,
            self.runner,
            NativeProbe(self.runner, profile.dpkg_query),
            self.runtime.secrets,
            profile.database_state,
        )
        self.capture = capture_deployment(profile.capture_deployment_file)
        self.capture_policy = capture_queue_policy(profile.capture_policy_file)
        self.worker_policy = worker_unit_policy(profile.worker_policy_file)
        self.continuity = continuity_config(profile.continuity_file)
        self.snapshot_worker = worker_config(profile.snapshot_worker_file)
        self.recall = recall_deployment(profile.recall_deployment_file)
        try:
            self.refresh = RefreshDeployment.model_validate_json(
                profile.refresh_deployment_file.read_bytes()
            )
        except (OSError, ValidationError) as exc:
            raise SetupError(
                "installation briefing refresh deployment cannot be read or validated"
            ) from exc
        self.nightly = nightly_config(profile.nightly_file)
        self.hook_policy = claude_hook_policy(profile.hook_policy_file)
        self.hooks = ClaudeHookInstaller(
            profile.claude_settings, profile.state_root / "hooks", self.hook_policy
        )
        self.timer = NightlyInstaller(
            profile.runtime.manager.units_directory,
            profile.state_root / "nightly-units",
            profile.runtime.manager.systemctl,
            nightly_timer_policy(profile.nightly_policy_file),
        )
        self.capture_install = capture_installation(
            profile.runtime.runtime.executable,
            profile.capture_deployment_file,
            profile.capture_policy_file,
            self.capture,
            self.capture_policy,
            self.worker_policy,
            scope="user",
            account=None,
        )
        manager = profile.runtime.manager.model_copy(
            update={"state_directory": profile.state_root / "application-units"}
        )
        self.application_units = UserServiceInstaller(manager, self.worker_policy)
        self._validate()
        self._inputs = self._fingerprints()

    def _validate(self) -> None:
        p, runtime = self.profile, self.profile.runtime.runtime
        if any(
            value != p.instance
            for value in (
                self.capture.instance,
                self.continuity.instance,
                self.recall.instance,
                self.refresh.instance,
                self.nightly.instance,
            )
        ):
            raise SetupError("installation deployment owners differ")
        if self.capture.config != runtime.setup_file or self.recall.config != runtime.setup_file:
            raise SetupError("native hooks must use the installed setup profile")
        if (
            self.capture.archive_config != runtime.archive_file
            or self.capture.memory_config != p.memory_file
            or self.capture.search_config != runtime.search_file
            or self.recall.archive_config != runtime.archive_file
            or self.recall.service_config != p.memory_service_file
        ):
            raise SetupError(
                "native hooks must use the installed archive, graph and service profiles"
            )
        if self.capture.cwd != runtime.cwd or self.recall.cwd != runtime.cwd:
            raise SetupError("hook and runtime working directories differ")
        if self.refresh.continuity_config != p.continuity_file:
            raise SetupError("briefing refresh uses a different continuity deployment")
        if self.nightly.executable != runtime.executable or any(
            path != p.runtime.notes_identity_file
            for path in (
                self.nightly.model_file,
                self.nightly.card_model_file,
                self.nightly.lesson_model_file,
            )
        ):
            raise SetupError("nightly command or notes identity differs from the installed runtime")
        if tuple(self.nightly.common) != self._common():
            raise SetupError("nightly stages must use the installed store configuration")
        if (
            self.nightly.extraction_config != p.runtime.extraction_file
            or self.nightly.search_config != runtime.search_file
        ):
            raise SetupError("nightly stages must use the installed execution and search policies")
        if (
            len(
                {
                    self.service.bind.port,
                    *(
                        endpoint.port
                        for endpoint in (
                            self.setup.services.archive,
                            self.setup.services.graph,
                            self.setup.services.encoder,
                            self.setup.services.reranker,
                            self.setup.services.notes,
                        )
                    ),
                }
            )
            != 6
        ):
            raise SetupError("memory service and runtime ports must be distinct")
        self._validate_registry()

    def _validate_registry(self) -> None:
        registries = [self.continuity.registry, self.snapshot_worker.registry]
        if self.nightly.briefings is not None:
            try:
                judge = JudgeConfig.model_validate_json(
                    source_text(self.nightly.briefings.judge_config, self.profile.max_profile_bytes)
                )
            except (OSError, ValidationError) as exc:
                raise SetupError("installed nightly judge configuration is invalid") from exc
            registries.append(judge.registry)
        selected = next((registry for registry in registries if registry is not None), None)
        if selected is None:
            return
        if any(registry != selected for registry in registries):
            raise SetupError("continuity, snapshot and nightly judge registries must agree")
        if selected.records_dir != self.profile.state_root / "worker-records":
            raise SetupError("worker registry records must use the installation state directory")
        if selected.folder_roots != (self.snapshot_worker.input_root,):
            raise SetupError("worker registry cleanup roots must match snapshot input root only")

    def _fingerprints(self) -> dict[str, str]:
        p, r = self.profile, self.profile.runtime.runtime
        paths = {
            self.profile_path,
            r.setup_file,
            r.archive_file,
            r.search_file,
            r.service_file,
            r.onnx_file,
            p.memory_file,
            p.memory_service_file,
            p.continuity_file,
            p.snapshot_worker_file,
            p.capture_deployment_file,
            p.capture_policy_file,
            p.recall_deployment_file,
            p.refresh_deployment_file,
            p.nightly_file,
            p.runtime.extraction_file,
        }
        paths.update(
            path
            for path in (
                p.database_policy_file,
                p.worker_policy_file,
                p.nightly_policy_file,
                p.hook_policy_file,
            )
            if path is not None
        )
        if self.nightly.briefings is not None:
            paths.add(self.nightly.briefings.judge_config)
        policy = batch_policy()
        return {str(path): input_digest(path, policy) for path in sorted(paths)}

    def _common(self) -> tuple[str, ...]:
        return (
            "--config",
            str(self.profile.runtime.runtime.setup_file),
            "--archive-config",
            str(self.profile.runtime.runtime.archive_file),
            "--memory-config",
            str(self.profile.memory_file),
        )

    def _units(self) -> dict[str, WorkerUnit]:
        p, r = self.profile, self.profile.runtime.runtime
        service = WorkerUnit(
            description="Anamnema memory service",
            cwd=r.cwd,
            scope="user",
            command=(
                str(r.executable),
                "serve",
                *self._common(),
                "--search-config",
                str(r.search_file),
                "--service-config",
                str(p.memory_service_file),
                "--extraction-config",
                str(p.runtime.extraction_file),
                "--lesson-model-file",
                str(p.runtime.notes_identity_file),
                "--card-model-file",
                str(p.runtime.notes_identity_file),
            ),
        )
        return {
            "anamnema-memory.service": service,
            "anamnema-capture.service": self.capture_install.watcher,
        }

    def _bindings(self) -> tuple[HookBinding, ...]:
        p, r = self.profile, self.profile.runtime.runtime
        continuity = continuity_bindings(
            ContinuityHooks(
                executable=r.executable,
                continuity_config=p.continuity_file,
                worker_config=p.snapshot_worker_file,
                producer_timeout_seconds=producer_policy().timeout_seconds,
            ),
            self.hook_policy,
        )
        refresh = HookBinding(
            event="UserPromptSubmit",
            command=(
                str(r.executable),
                "briefing-refresh",
                "--deployment",
                str(p.refresh_deployment_file),
            ),
            timeout_seconds=recall_policy().hook_timeout_seconds,
        )
        return (
            *continuity,
            *self.capture_install.hooks,
            recall_binding(r.executable, p.recall_deployment_file, recall_policy()),
            refresh,
            action_binding(r.executable, p.recall_deployment_file, action_hook_policy()),
        )

    def _timer_units(self) -> dict[str, str]:
        p, r = self.profile, self.profile.runtime.runtime
        return render_nightly(
            r.executable, p.nightly_file, self._common(), r.cwd, self.timer.policy
        )

    def plan(self) -> dict[str, object]:
        return {
            "mode": "dry-run",
            "complete": False,
            "profile": "logged-in-user",
            "parent_death_cleanup_configured": self.snapshot_worker.registry is not None,
            "prerequisite_packages": list(self.profile.prerequisite_packages),
            "databases": self.databases.plan(),
            "runtimes": self.runtime.plan(),
            "application_units": sorted(self.application_units.plan(self._units())),
            "hooks": self.hooks.plan(self._bindings()),
            "nightly": self.timer.plan(self._timer_units()),
            "requires": [
                "Debian 13",
                "logged-in user with unlocked Secret Service",
                "operator-provided Claude login",
                "explicit model download consent",
            ],
        }

    def apply(self, *, allow_downloads: bool, through: str = "verify") -> dict[str, Any]:
        operations: dict[str, Callable[[], Metrics]] = {
            "platform": self._platform,
            "prerequisites": self._prerequisites,
            "databases": self._databases,
            "models": partial(self._models, allow_downloads),
            "schema": self._schema,
            "services": self._services,
            "hooks": self._hooks,
            "schedule": self._schedule,
            "verify": self._verify,
        }
        if through not in operations:
            raise SetupError("unknown installation checkpoint")
        names = tuple(operations)
        names = names[: names.index(through) + 1]
        plan = BatchPlan(
            instance=self.profile.instance,
            session="native-installation",
            input_path=self.profile_path,
            input_digest=self._inputs[str(self.profile_path)],
            profile_digest=digest({name: value for name, value in self._inputs.items()}),
            stages=names,
        )
        journal = BatchJournal(self.profile.state_root / "jobs", plan, batch_policy())
        already_complete = journal.inspect().complete
        bound = {name: partial(self._call, operations[name]) for name in names}
        state = BatchRunner(journal, bound).run()
        if through == "verify" and already_complete:
            # A completed journal is history; a repeated apply still verifies live services.
            self._verify()
        return {
            "mode": "apply",
            "complete": state.complete and through == "verify",
            "job": plan.identity,
            "completed": [stage.name for stage in state.stages if stage.complete],
            "stopped_after": through,
            "data_removed": False,
            "parent_death_cleanup_configured": self.snapshot_worker.registry is not None,
        }

    def _call(self, action: Callable[[], Metrics]) -> Metrics:
        if self._fingerprints() != self._inputs:
            raise SetupError(
                "installation configuration changed; resume refused before the next effect"
            )
        return action()

    def _platform(self) -> Metrics:
        import pwd

        release = platform.freedesktop_os_release() if platform.system() == "Linux" else {}
        if release.get("ID") != "debian" or release.get("VERSION_ID") != "13" or os.getuid() == 0:
            raise SetupError("native installation requires a non-root Debian 13 login")
        user = pwd.getpwuid(os.getuid()).pw_name
        choices = self.setup.choices
        if choices.run_when_logged_out is True or choices.service_scope == "system":
            raise SetupError(
                "first profile does not support unattended or system-scope application workers"
            )
        if any(
            account not in (None, user)
            for account in (
                choices.accounts.application,
                choices.accounts.models,
                choices.keyring_owner,
            )
        ):
            raise SetupError("application, model and keyring accounts must be the logged-in user")
        return {"supported": True}

    def _databases(self) -> Metrics:
        for name in ("mongodb", "neo4j"):
            self.databases.apply(name)
        return {"verified": 2}

    def _prerequisites(self) -> Metrics:
        prefix = list(self.databases.policy.privileged)
        commands = (
            [*prefix, "apt-get", "update"],
            [
                *prefix,
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "apt-get",
                "install",
                "--assume-yes",
                "--no-install-recommends",
                "--",
                *self.profile.prerequisite_packages,
            ],
        )
        for command in commands:
            result = self.runner(command, None, self.databases.policy.command_seconds, None)
            if result.code:
                raise SetupError(
                    "native prerequisite preparation failed; check sudo authorization and APT state"
                )
        return {"packages_requested": len(self.profile.prerequisite_packages)}

    def _schema(self) -> Metrics:
        graph = connect_graph(self.setup, self.archive, self.memory)
        try:
            graph.ensure_schema()
        finally:
            graph.close()
        return {"ready": True}

    def _models(self, allow_downloads: bool) -> Metrics:
        return {
            key: value for key, value in self.runtime.apply(allow_downloads=allow_downloads).items()
        }

    def _services(self) -> Metrics:
        self.application_units.apply(self._units())
        wait_ready(
            self._health,
            self.profile.runtime.readiness_seconds,
            self.profile.runtime.poll_seconds,
            "memory service",
        )
        return {"active": 2}

    def _hooks(self) -> Metrics:
        worker_config(self.profile.snapshot_worker_file)
        self.application_units.apply(self._units())
        self.hooks.apply(self._bindings())
        return {"registered": len(self._bindings())}

    def _schedule(self) -> Metrics:
        self.timer.apply(self._timer_units())
        return {"timers": 1}

    def _health(self) -> None:
        access = [item for item in self.service.access if item.instance == self.profile.instance]
        if len(access) != 1:
            raise SetupError("memory service needs this installation's explicit bearer selector")
        token = self.runtime.secrets.lookup(access[0].entry, access[0].field)
        if not token or any(not 33 <= ord(character) <= 126 for character in token):
            raise SetupError("memory service credential is not a valid bearer token")
        connection = http.client.HTTPConnection(
            self.service.bind.host,
            self.service.bind.port,
            timeout=self.service.body_timeout_seconds,
        )
        try:
            connection.request(
                "GET", self.service.health_path, headers={"Authorization": "Bearer " + token}
            )
            with connection.getresponse() as response:
                if response.status != 200:
                    if response.status == 503:
                        raise ServiceNotReady("memory service is not ready (HTTP 503)")
                    raise SetupError("installed memory service did not pass authenticated health")
                if (
                    len(response.read(self.service.max_response_bytes + 1))
                    > self.service.max_response_bytes
                ):
                    raise SetupError("installed memory health response exceeded its bound")
        except (ConnectionError, TimeoutError, http.client.RemoteDisconnected) as exc:
            raise ServiceNotReady(
                f"installed memory service transport not ready ({type(exc).__name__})"
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise SetupError("installed memory service is not ready; owned units retained") from exc
        finally:
            connection.close()

    def _verify(self) -> Metrics:
        self._databases()
        self.runtime.verify()
        self.application_units.apply(self._units())
        self._health()
        self.hooks.plan(self._bindings())
        result = self.runner(
            [str(self.timer.systemctl), "--user", "is-active", "--quiet", self.timer.timer],
            None,
            self.timer.policy.command_seconds,
            None,
        )
        if result.code:
            raise SetupError("installed nightly timer is not active")
        return {"verified": True}
