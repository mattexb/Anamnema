"""Shared marker grammar and source-backed manual capture, using real isolated stores."""

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from search_fixtures import SearchHTTP
from test_memory_workflow import _cli_options
from test_memory_workflow import memory_workflow as memory_workflow

from anamnema.archive_models import Record, Snapshot, digest
from anamnema.capture_writer import CaptureWriter
from anamnema.cli import main
from anamnema.graph_corrections import correct
from anamnema.graph_store import MemoryConflict, query
from anamnema.marked_capture import capture_policy, captured_marks
from anamnema.memory_models import CaptureOrigin, Correction, MemoryNote, Outcome, Rewrite
from anamnema.setup_config import SetupError
from anamnema.speech import without_marked
from anamnema.speech_policy import speech_policy


def record(text, *, instance="fixture", timestamp="2026-01-01T10:00:00Z", **fields):
    source = {
        "type": "assistant",
        "sessionId": "session-one",
        "timestamp": timestamp,
        "uuid": "reused-native-uuid",
        "message": {"content": [{"type": "text", "text": text}]},
        **fields,
    }
    return Record(instance, "claude", "session-one", "conversation", timestamp, 1, source)


def snapshot(*records):
    return Snapshot(Path("synthetic.jsonl"), "synthetic", tuple(records), 0, 0, False)


def test_one_detector_captures_and_excludes_only_genuine_markers():
    text = (
        "fact(Project (test)): Keep this fact.\n> mistake: quoted\n```\n"
        "learned_lesson: fenced\n```\n~~~\nfact: another example\n~~~\n"
        "- mistake: Check before claiming.\nlearned_lesson: Verify original sources."
    )
    marks = captured_marks(snapshot(record(text)), capture_policy(), speech_policy())
    assert [mark.marker for mark in marks] == ["fact", "mistake", "learned_lesson"]
    assert [mark.index for mark in marks] == [0, 1, 2]
    assert marks[0].entity == "Project (test)"
    remaining = without_marked(text, speech_policy())
    assert "Keep this fact" not in remaining and "Check before claiming" not in remaining
    assert "quoted" in remaining and "fenced" in remaining and "another example" in remaining


@pytest.mark.parametrize("field,value", [("isMeta", True), ("isSidechain", True), ("type", "user")])
def test_transport_or_owner_words_never_become_manual_agent_notes(field, value):
    assert not captured_marks(
        snapshot(record("fact: Not agent evidence.", **{field: value})),
        capture_policy(),
        speech_policy(),
    )


def test_current_stretch_whole_backstop_repeats_and_native_uuid_reuse():
    old = record("mistake: Old event.")
    boundary = Record(
        "fixture", "claude", "session-one", "summary", old.timestamp, 2, {"isCompactSummary": True}
    )
    new = record("fact: New event.")
    data = snapshot(old, boundary, new, new)
    recent = captured_marks(data, capture_policy(), speech_policy())
    whole = captured_marks(data, capture_policy(), speech_policy(), whole=True)
    assert len(recent) == 1 and len(whole) == 2 and whole[0].id != whole[1].id
    assert captured_marks(data, capture_policy(), speech_policy(), whole=True) == whole


@pytest.mark.parametrize("noise", ["meta", "sidechain", "tool", "mixed_tool"])
def test_last_turn_ignores_non_owner_prompt_boundaries_and_preserves_ids(noise):
    old = record("fact: An older turn.")
    prompt = record("A genuine new prompt.", type="user")
    latest = record("fact: The current turn.")
    fields = {"type": "user"}
    text = "Not a new owner prompt."
    if noise == "meta":
        fields["isMeta"] = True
    elif noise == "sidechain":
        fields["isSidechain"] = True
    elif noise in ("tool", "mixed_tool"):
        blocks = [{"type": "tool_result", "content": "synthetic output"}]
        if noise == "mixed_tool":
            blocks.append({"type": "text", "text": text})
        fields["message"] = {"content": blocks}
    data = snapshot(old, prompt, latest, record(text, **fields))
    policy, speech = capture_policy(), speech_policy()
    last = captured_marks(data, policy, speech, last_turn=True)
    whole = captured_marks(data, policy, speech, whole=True)
    assert last == (whole[-1],) and len(whole) == 2


@pytest.mark.parametrize("prompt", ["injection", "peer", "image", "wrapper"])
def test_native_non_tool_user_boundaries_bound_autonomous_capture(prompt):
    speech = speech_policy()
    text = {
        "injection": speech.claude.injected_prefixes[0] + "synthetic notification",
        "peer": speech.claude.peer_prefix + " synthetic peer turn",
        "wrapper": "<system-reminder></system-reminder>",
        "image": "",
    }[prompt]
    fields = {"type": "user"}
    if prompt == "image":
        fields["message"] = {"content": [{"type": "image", "source": {"type": "base64"}}]}
    data = snapshot(
        record("fact: Old one.\nfact: Old two."),
        record(text, **fields),
        record("fact: Latest turn."),
    )
    policy = capture_policy().model_copy(update={"max_marks": 1})
    marks = captured_marks(data, policy, speech, last_turn=True)
    assert len(marks) == 1 and marks[0].text == "Latest turn."


def test_last_turn_handles_boundaries_empty_turns_and_no_prompt_fallback():
    policy, speech = capture_policy(), speech_policy()
    old, latest = record("fact: Old."), record("fact: Latest.")
    boundary = Record("fixture", "claude", "session-one", "summary", old.timestamp, 2, {})
    data = snapshot(old, boundary, latest)
    assert captured_marks(data, policy, speech, last_turn=True) == captured_marks(
        data, policy, speech
    )
    assert not captured_marks(
        snapshot(old, record("New owner turn.", type="user")), policy, speech, last_turn=True
    )
    with pytest.raises(SetupError, match="mutually exclusive"):
        captured_marks(data, policy, speech, whole=True, last_turn=True)


def test_last_turn_uses_configured_tool_result_type():
    old = record("fact: Before a tool response.")
    tool = record(
        "ignored",
        type="user",
        message={
            "content": [
                {"type": "text", "text": "tool context"},
                {"type": "configured-result", "content": "data"},
            ]
        },
    )
    policy = capture_policy()
    policy = policy.model_copy(
        update={
            "turn_prompt": policy.turn_prompt.model_copy(
                update={"tool_result_type": "configured-result"}
            )
        }
    )
    assert len(captured_marks(snapshot(old, tool), policy, speech_policy(), last_turn=True)) == 1


@pytest.mark.parametrize(
    "problem", ["catalogue", "time", "count", "text", "native_session", "owner"]
)
def test_invalid_capture_is_visible_before_mutation(problem):
    policy = capture_policy()
    row = record("fact: One line.\nfact: Another line.")
    data = snapshot(row)
    if problem == "catalogue":
        policy = policy.model_copy(update={"kinds": {"fact": "fact"}})
    elif problem == "time":
        data = snapshot(record("fact: One line.", timestamp="2026-01-01"))
    elif problem == "count":
        policy = policy.model_copy(update={"max_marks": 1})
    elif problem == "text":
        policy = policy.model_copy(update={"max_text_characters": 1})
    elif problem == "native_session":
        data = snapshot(record("fact: Foreign.", sessionId="other-session"))
    else:
        data = snapshot(row, record("fact: Other.", instance="foreign"))
    with pytest.raises(SetupError):
        captured_marks(data, policy, speech_policy(), whole=True)


def test_optional_capture_metadata_does_not_change_older_insertion_hashes():
    note = MemoryNote(
        id="original",
        instance="fixture",
        text="A tiny note.",
        kind="fact",
        source_ids=("source",),
        session="s",
        substrate="claude",
        window="w",
        run="r",
        query_vector=(1.0,),
        passage_vector=(1.0,),
        embedded_with="fixture",
    )
    assert "capture_origin" not in note.model_dump(mode="json")
    assert note.fingerprint == "8d18cf985540e92a9f0f664dc91a9b43ad3f3d8a1a068ec98ce4cdc5bc1abd81"
    raw = note.model_dump(mode="json")
    raw["window"] = None
    with pytest.raises(ValidationError, match="unwindowed"):
        MemoryNote.model_validate_json(json.dumps(raw))
    raw["window"] = "w"
    raw["capture_origin"] = {
        "source_id": "source",
        "index": 0,
        "marker": "fact",
        "text_digest": digest("Not this source text"),
    }
    with pytest.raises(ValidationError, match="capture origin"):
        MemoryNote.model_validate_json(json.dumps(raw))


def prepared(memory_workflow, text="mistake: I skipped a check.\nlearned_lesson: Verify first."):
    instance, _, engine, server, _, _, _, archive = memory_workflow
    original = record(text, instance=instance)
    archive.write([original])
    archive.verify([original])
    policy, speech = capture_policy(), speech_policy()
    marks = captured_marks(snapshot(original), policy, speech)
    return (
        instance,
        engine,
        server,
        archive,
        marks,
        CaptureWriter(engine.store, engine.sources, engine.encoder, policy, speech),
    )


def test_actual_capture_origin_link_source_view_and_no_vote_replay(memory_workflow):
    instance, engine, server, _, marks, writer = prepared(memory_workflow)
    first = writer.apply(instance, marks)
    assert first["inserted"] == 2 and first["notes_model_calls"] == 0
    failure, principle = (engine.store.note(instance, mark.id) for mark in marks)
    origin = CaptureOrigin.model_validate_json(principle["capture_origin"])
    assert origin.lesson_of == failure["note_id"] and principle["lesson_of"] == marks[0].id
    assert failure["capture_mode"] == "marked" and not failure.get("window")
    assert failure["event_time"] == marks[0].timestamp
    shown = engine.sources.open_note(engine.store, instance, marks[0].id)
    assert shown["context"]["source_only"] is True and shown["context"]["status"] == "partial"
    assert "mistake:" in str(shown["rows"]) and shown["judge_against"] == [marks[0].source_id]
    before = {mark.id: engine.store.note(instance, mark.id) for mark in marks}
    calls = len(server.calls)
    replay = writer.apply(instance, marks)
    assert replay["inserted"] == 0 and replay["already_present"] == 2
    assert len(server.calls) == calls
    assert {mark.id: engine.store.note(instance, mark.id) for mark in marks} == before


def test_interrupted_placement_retries_stored_corrected_vector_without_reembedding(
    memory_workflow, monkeypatch
):
    instance, engine, server, _, marks, writer = prepared(memory_workflow, "fact: The old wording.")
    place = writer.topics.place

    def interrupted(*_):
        raise OSError("fixture stopped before placement")

    monkeypatch.setattr(writer.topics, "place", interrupted)
    with pytest.raises(OSError, match="before placement"):
        writer.apply(instance, marks)
    stored = engine.store.note(instance, marks[0].id)
    assert stored["capture_origin"] and stored["placement_pending"]
    assert correct(
        engine.store,
        instance,
        Correction(
            id="capture-rewrite",
            note=marks[0].id,
            outcome=Outcome.PARTLY,
            reason="Original checked.",
            against=(marks[0].source_id,),
            rewrite="The corrected wording.",
        ),
        Rewrite(
            query_vector=(0.0, 0.0, 1.0, 0.0),
            passage_vector=(0.0, 0.0, 1.0, 0.0),
            embedded_with="fixture",
        ),
    )
    before = engine.store.note(instance, marks[0].id)
    calls = len(server.calls)
    monkeypatch.setattr(writer.topics, "place", place)
    result = writer.apply(instance, marks)
    after = engine.store.note(instance, marks[0].id)
    assert result["placed_again"] == 1 and len(server.calls) == calls
    for field in (
        "text",
        "query_vector",
        "passage_vector",
        "status",
        "trust_positive",
        "trust_negative",
        "capture_origin",
        "source_ids",
    ):
        assert after[field] == before[field]


def test_deleted_capture_is_not_resurrected_and_foreign_source_is_refused(memory_workflow):
    instance, engine, server, _, marks, writer = prepared(
        memory_workflow, "fact: A removable claim."
    )
    writer.apply(instance, marks)
    assert correct(
        engine.store,
        instance,
        Correction(
            id="capture-delete",
            note=marks[0].id,
            outcome=Outcome.WRONG,
            reason="Checked and removed.",
            against=(marks[0].source_id,),
            delete=True,
        ),
        None,
    )
    calls = len(server.calls)
    assert writer.apply(instance, marks)["not_materialized"] == 1 and len(server.calls) == calls
    with pytest.raises(SetupError, match="ownership"):
        writer.apply("foreign", marks)
    forged = dataclasses.replace(marks[0], text="Not the words in the source")
    with pytest.raises(SetupError, match="differs"):
        writer.apply(instance, (forged,))


def test_changed_semantic_policy_refuses_reinterpretation_but_bounds_can_change(memory_workflow):
    instance, engine, _, _, marks, writer = prepared(memory_workflow, "fact: Preserve this once.")
    writer.apply(instance, marks)
    original = engine.store.note(instance, marks[0].id)
    larger = writer.policy.model_copy(update={"max_marks": writer.policy.max_marks + 1})
    assert (
        CaptureWriter(engine.store, engine.sources, engine.encoder, larger, writer.speech).apply(
            instance, marks
        )["already_present"]
        == 1
    )
    selection = writer.policy.model_copy(
        update={
            "turn_prompt": writer.policy.turn_prompt.model_copy(
                update={"tool_result_type": "another-native-result"}
            )
        }
    )
    assert (
        CaptureWriter(engine.store, engine.sources, engine.encoder, selection, writer.speech).apply(
            instance, marks
        )["already_present"]
        == 1
    )
    changed = writer.policy.model_copy(update={"event_time_kinds": ()})
    with pytest.raises(MemoryConflict, match="policy changed"):
        CaptureWriter(engine.store, engine.sources, engine.encoder, changed, writer.speech).apply(
            instance, marks
        )
    assert engine.store.note(instance, marks[0].id) == original


def test_owner_only_policy_edits_do_not_break_capture_replay(memory_workflow):
    instance, engine, server, _, marks, writer = prepared(memory_workflow, "fact: Keep this.")
    writer.apply(instance, marks)
    original = engine.store.note(instance, marks[0].id)
    calls = len(server.calls)
    changed = writer.speech.model_copy(update={"owner_envelopes": (r"^new owner wrapper",)})
    result = CaptureWriter(
        engine.store, engine.sources, engine.encoder, writer.policy, changed
    ).apply(instance, marks)
    assert result["already_present"] == 1 and len(server.calls) == calls
    assert engine.store.note(instance, marks[0].id) == original


def test_origin_replay_compares_values_not_json_layout_or_omitted_defaults(memory_workflow):
    instance, engine, server, _, marks, writer = prepared(memory_workflow, "fact: Same origin.")
    writer.apply(instance, marks)
    origin = json.loads(engine.store.note(instance, marks[0].id)["capture_origin"])
    origin.pop("entity")
    origin.pop("lesson_of")
    encoded = json.dumps(dict(reversed(list(origin.items()))), indent=2)
    engine.store.write(
        lambda tx: query(
            tx,
            f"MATCH (n:{engine.store.settings.note_label} {{instance:$instance,note_id:$note}}) "
            "SET n.capture_origin=$origin",
            instance=instance,
            note=marks[0].id,
            origin=encoded,
        )
    )
    before = engine.store.note(instance, marks[0].id)
    calls = len(server.calls)
    assert writer.apply(instance, marks)["already_present"] == 1
    assert engine.store.note(instance, marks[0].id) == before and len(server.calls) == calls
    for corrupt in (
        None,
        "invalid-json",
        json.dumps({**origin, "index": "0"}),
        json.dumps({**origin, "entity": "a different origin"}),
    ):
        engine.store.write(
            lambda tx, corrupt=corrupt: query(
                tx,
                f"MATCH (n:{engine.store.settings.note_label} "
                "{instance:$instance,note_id:$note}) "
                "SET n.capture_origin=$origin",
                instance=instance,
                note=marks[0].id,
                origin=corrupt,
            )
        )
        damaged = engine.store.note(instance, marks[0].id)
        with pytest.raises(MemoryConflict, match="capture origin"):
            writer.apply(instance, marks)
        assert engine.store.note(instance, marks[0].id) == damaged and len(server.calls) == calls


def test_missing_lookup_result_is_a_visible_capture_error(memory_workflow, monkeypatch):
    instance, engine, server, _, marks, writer = prepared(memory_workflow, "fact: A missing row.")
    calls = len(server.calls)
    monkeypatch.setattr(writer.sources, "records", lambda *_: ())
    with pytest.raises(SetupError, match="source lookup"):
        writer.apply(instance, marks)
    assert not engine.store.known(instance, marks[0].id) and len(server.calls) == calls


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_message_unicode_separators_are_not_physical_marker_boundaries(separator):
    speech = speech_policy()
    text = "fact: First" + separator + "second.\nOrdinary" + separator + "continuation."
    marks = captured_marks(snapshot(record(text)), capture_policy(), speech)
    assert marks[0].text == "First" + separator + "second."
    assert without_marked(text, speech) == "Ordinary" + separator + "continuation."


@pytest.mark.parametrize("apply", [False, True])
def test_empty_capture_or_preview_uses_no_credentials_or_services(
    tmp_path, monkeypatch, capsys, apply
):
    path = tmp_path / "empty.jsonl"
    row = record("ordinary prose" if apply else "fact: A synthetic line.")
    path.write_text(json.dumps(row.source) + "\n", encoding="utf-8")

    def forbidden(*_, **__):
        pytest.fail("capture preview/empty apply must not open infrastructure")

    monkeypatch.setattr("anamnema.memory_cli.connect_graph", forbidden)
    monkeypatch.setattr("anamnema.memory_cli.keyring_uri", forbidden)
    args = ["capture", "--file", str(path), "--session", "session-one", "--instance", "fixture"]
    if apply:
        args.append("--apply")
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sources_verified"] is False and result["graph_written"] is False


def test_last_turn_cli_previews_only_current_turn_without_infrastructure(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "turns.jsonl"
    rows = [
        record("fact: Previous turn."),
        record("Next prompt.", type="user"),
        record("fact: Current turn."),
    ]
    path.write_text("\n".join(json.dumps(row.source) for row in rows) + "\n", encoding="utf-8")

    def forbidden(*_, **__):
        pytest.fail("last-turn preview must not open infrastructure")

    monkeypatch.setattr("anamnema.memory_cli.connect_graph", forbidden)
    monkeypatch.setattr("anamnema.memory_cli.keyring_uri", forbidden)
    args = ["capture", "--file", str(path), "--session", "session-one", "--instance", "fixture"]
    assert main([*args, "--last-turn"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["marks"] == 1 and preview["last_turn"] and not preview["whole"]
    with pytest.raises(SystemExit):
        main([*args, "--last-turn", "--whole"])


@pytest.fixture
def capture_notes_server():
    with SearchHTTP() as server:
        yield server


def test_cli_apply_captures_and_replays_with_real_isolated_services(
    memory_workflow, tmp_path, capture_notes_server
):
    instance, _, engine, server, _, _, _, archive = memory_workflow
    path = tmp_path / "captured.jsonl"
    raw = json.dumps(record("fact(Project): Keep a source-backed fact.", instance=instance).source)
    path.write_text(raw + "\n", encoding="utf-8")
    options = _cli_options(
        instance,
        engine,
        server,
        archive,
        tmp_path,
        notes_port=capture_notes_server.server.server_port,
    )
    args = [
        "capture",
        *options,
        "--file",
        str(path),
        "--session",
        "session-one",
        "--last-turn",
        "--apply",
    ]
    command = [sys.executable, "-m", "anamnema", *args]
    first_run = subprocess.run(  # noqa: S603 - own Python entry point and synthetic configs
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=30,
    )
    assert first_run.returncode == 0, first_run.stderr
    first = json.loads(first_run.stdout)
    assert first["sources_verified"] and first["inserted"] == 1 and first["notes_model_calls"] == 0
    assert first["last_turn"] and not first["whole"]
    calls = len(server.calls)
    second_run = subprocess.run(  # noqa: S603 - own Python entry point and synthetic configs
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=30,
    )
    assert second_run.returncode == 0, second_run.stderr
    replay = json.loads(second_run.stdout)
    assert replay["already_present"] == 1 and replay["graph_written"] is False
    assert len(server.calls) == calls and path.read_text(encoding="utf-8") == raw + "\n"
    assert capture_notes_server.calls == []


def test_competing_capture_commits_once_and_reports_the_existing_note(memory_workflow, monkeypatch):
    instance, engine, _, _, marks, writer = prepared(memory_workflow, "fact: One captured claim.")
    other = CaptureWriter(
        engine.store, engine.sources, engine.encoder, writer.policy, writer.speech
    )
    encode = engine.encoder.embed_note
    competed = False

    def competing(text):
        nonlocal competed
        if not competed:
            competed = True
            assert other.apply(instance, marks)["inserted"] == 1
        return encode(text)

    monkeypatch.setattr(engine.encoder, "embed_note", competing)
    result = writer.apply(instance, marks)
    assert result["inserted"] == 0 and result["already_present"] == 1
    note = engine.store.note(instance, marks[0].id)
    assert note["trust_positive"] == engine.store.settings.trust_prior_positive
    assert note["trust_negative"] == engine.store.settings.trust_prior_negative
