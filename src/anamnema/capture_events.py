"""Validate native event coordinates and bind requests to explicit capture configuration."""

import argparse
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from anamnema.archive_models import digest
from anamnema.capture_cli import CaptureInput, prepare_capture
from anamnema.capture_queue import CaptureJob, new_capture_job
from anamnema.capture_queue_config import CaptureDeployment, CaptureQueuePolicy
from anamnema.capture_selection import pin_capture
from anamnema.ingest_config import archive_settings
from anamnema.marked_capture import capture_policy
from anamnema.memory_config import load_memory
from anamnema.search_services import load_search_services
from anamnema.setup_config import SetupError, load_config
from anamnema.speech import marked_matches, scrub
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude import ClaudeAdapter

NATIVE_OBJECT = TypeAdapter(dict[str, JsonValue])


def _field(data: dict[str, JsonValue], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise SetupError("capture hook requires explicit session, transcript and cwd")
    return value


def configuration_digest(deployment: CaptureDeployment, policy: CaptureQueuePolicy) -> str:
    """Queued work cannot silently switch service selectors or parsing policies mid-run."""
    configurations = (
        deployment,
        policy,
        load_config(deployment.config),
        archive_settings(deployment.archive_config),
        load_memory(deployment.memory_config),
        load_search_services(deployment.search_config),
        capture_policy(deployment.capture_config),
        speech_policy(deployment.speech_config),
    )
    return digest([item.model_dump(mode="json") for item in configurations])


def marker_hash(marker: str, text: str, entity: str) -> str:
    """This is an expectation hash, never a source or note identity."""
    return digest([marker, text, entity])


def request_from_event(
    payload: object,
    event: Literal["stop", "precompact", "end"],
    deployment: CaptureDeployment,
    policy: CaptureQueuePolicy,
) -> CaptureJob | None:
    """Only explicit deployment ownership and allowed transcript roots enter the queue."""
    try:
        data = NATIVE_OBJECT.validate_python(payload)
    except ValidationError as exc:
        raise SetupError("capture hook input must be a JSON object") from exc
    fields = policy.native_fields
    if data.get(fields["event"]) != policy.native_events[event]:
        raise SetupError("capture hook event does not match invoked handler")
    session, transcript, cwd = (
        _field(data, fields[name]) for name in ("session", "transcript", "cwd")
    )
    if Path(cwd) != deployment.cwd:
        raise SetupError("capture hook cwd differs from deployment")
    path = Path(transcript)
    deployment.check_transcript(path)
    expected: list[str] = []
    capture, speech = (
        capture_policy(deployment.capture_config),
        speech_policy(deployment.speech_config),
    )
    capture.check(speech)
    if event == "stop":
        message = data.get(fields["message"])
        if not isinstance(message, str) or len(message) > policy.max_message_characters:
            raise SetupError("capture Stop requires bounded assistant message text")
        for _, match in marked_matches(message, speech):
            text = scrub(match["text"], speech)[0].strip()
            if not text or len(text) > capture.max_text_characters:
                raise SetupError("capture expectation text exceeds configured bound")
            expected.append(
                marker_hash(match["marker"], text, " ".join((match["entity"] or "").split()))
            )
            if len(expected) > capture.max_marks:
                raise SetupError("capture expectation count exceeds configured bound")
        if not expected:
            return None
    snapshot = ClaudeAdapter(archive_settings(deployment.archive_config)).read(
        path, deployment.instance, session
    )
    selection = pin_capture(
        snapshot, deployment.instance, session, capture, speech, turn=event == "stop"
    )
    return new_capture_job(
        session, path, event, tuple(expected), configuration_digest(deployment, policy), selection
    )


def prepare_job(job: CaptureJob, deployment: CaptureDeployment) -> CaptureInput:
    """The same CLI preparation derives archive identities only from actual native records."""
    deployment.check_transcript(job.transcript)
    return prepare_capture(
        argparse.Namespace(
            instance=deployment.instance,
            file=job.transcript,
            session=job.session,
            capture_config=deployment.capture_config,
            speech_config=deployment.speech_config,
            memory_config=deployment.memory_config,
            archive_config=deployment.archive_config,
            whole=False,
            last_turn=False,
            selection=job.selection.model_dump_json(),
        )
    )


def expectations_present(job: CaptureJob, prepared: CaptureInput) -> bool:
    """Repeated identical lines retain multiplicity; partial transcript flush is not success."""
    available = Counter(marker_hash(mark.marker, mark.text, mark.entity) for mark in prepared.marks)
    return Counter(job.expected) <= available
