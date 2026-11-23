"""Actual Mongo/extraction/Neo4j/source/correction workflow behind shared HTTP paths."""

import json
import sys
import uuid

import pytest
from extraction_fixtures import completion
from fastapi.testclient import TestClient
from search_fixtures import SearchHTTP

from anamnema.archive_models import Record, Snapshot
from anamnema.archive_mongo import MongoArchive, import_snapshot
from anamnema.binding import bindings
from anamnema.chunks import chunks
from anamnema.cli import main
from anamnema.extraction_config import extraction_config
from anamnema.extraction_store import ExtractionStore
from anamnema.graph_corrections import correct, history
from anamnema.ingest_config import archive_settings
from anamnema.llama_provider import LlamaProvider
from anamnema.memory_engine import MemoryEngine
from anamnema.memory_loader import MemoryLoader
from anamnema.memory_models import Correction, Outcome, Rewrite
from anamnema.memory_service import create_memory_app
from anamnema.memory_sources import SourceLookup
from anamnema.merge_judge import MergeJudge
from anamnema.merge_policy import merge_policy
from anamnema.notes_policy import notes_policy
from anamnema.notes_response import validate_response
from anamnema.processing import prepare
from anamnema.repeat_merge import RepeatMerger
from anamnema.retrieval import Retriever
from anamnema.retrieval_config import load_retrieval
from anamnema.search_services import CPUEncoder, CPUReranker, LocalJSON, load_search_services
from anamnema.service_config import load_service
from anamnema.setup_config import Endpoint, SetupError, load_config
from anamnema.source_display import SourceDisplay
from anamnema.source_policy import source_policy
from anamnema.speech_policy import speech_policy
from anamnema.substrates.claude_speech import ClaudeSpeechReader
from anamnema.window_store import WindowStore


@pytest.fixture
def memory_workflow(graph, disposable_mongo, tmp_path, request):
    _, mongo = disposable_mongo
    instance = f"fixture-{uuid.uuid4().hex}"
    archive = MongoArchive(mongo["memory_" + uuid.uuid4().hex], archive_settings())
    record = Record(
        instance,
        "claude",
        "session-one",
        "conversation",
        "2026-01-01T10:00:00Z",
        1,
        {
            "type": "user",
            "timestamp": "2026-01-01T10:00:00Z",
            "message": "Retention is fourteen days, not seven.",
        },
    )
    closing = Record(
        instance,
        "claude",
        "session-one",
        "closing",
        record.timestamp,
        1,
        {"after": record.identity, "at": record.timestamp},
    )
    snapshot = Snapshot(tmp_path / "synthetic.jsonl", "synthetic", (record, closing), 0, 0, True)
    import_snapshot(snapshot, archive)
    window = prepare(snapshot).windows[0]
    speech, notes = speech_policy(), notes_policy()
    rows = ClaudeSpeechReader(speech).read(window).rows
    parts = chunks(rows, bindings(rows, notes), notes)
    extraction = ExtractionStore(archive.database, extraction_config(), notes, speech)
    unloaded = getattr(request, "param", "") == "unloaded-three"
    response = validate_response(
        json.dumps(
            {
                "concepts": [
                    {
                        "text": f"Retention is seven days. Claim {index}."
                        if unloaded
                        else "Retention is seven days.",
                        "kind": "decision",
                        "rows": [0],
                        "ruled_by": 0,
                    }
                    for index in range(3 if unloaded else 1)
                ]
            }
        ),
        notes,
        speech,
    )
    for part in parts:
        extraction.save(
            extraction.selector(window, part, "fixture-run"), part, response, "fixture-attempt"
        )
    with SearchHTTP() as server:
        policy = load_search_services().model_copy(update={"dimensions": 4})
        connection = LocalJSON(
            Endpoint(host="127.0.0.1", port=server.server.server_port),
            policy,
            "fixture-search-token",
            policy.timeout_seconds,
        )
        encoder, ranker = CPUEncoder(connection), CPUReranker(connection)
        display = SourceDisplay(WindowStore(archive, source_policy()), ClaudeSpeechReader(speech))
        source = SourceLookup(
            archive, graph.settings.max_items, graph.settings.max_source_bytes, display
        )
        loader = MemoryLoader(graph, source, extraction, encoder)
        if not unloaded:
            assert loader.window(window, parts, "fixture-run")["inserted"] == 1
        engine = MemoryEngine(graph, source, encoder, Retriever(graph, load_retrieval(), ranker))
        yield instance, record, engine, server, loader, window, parts, archive


@pytest.mark.parametrize("memory_workflow", ["unloaded-three"], indirect=True)
def test_loader_interruption_corrected_pending_note_and_model_free_replay(
    memory_workflow, monkeypatch
):
    instance, original, engine, _, loader, window, parts, _ = memory_workflow
    place = loader.topics.place
    reached = []

    def interrupted(owner, identity):
        reached.append(identity)
        if len(reached) == 2:
            raise OSError("synthetic interruption on second placement")
        return place(owner, identity)

    monkeypatch.setattr(loader.topics, "place", interrupted)
    with pytest.raises(OSError, match="second placement"):
        loader.window(window, parts, "fixture-run")
    rows = engine.store.read(
        f"MATCH (n:{engine.store.settings.note_label} {{instance:$instance}}) "
        "RETURN n.note_id AS id, coalesce(n.placement_pending,false) AS pending",
        instance=instance,
    )
    assert len(rows) == 3 and sum(row["pending"] for row in rows) == 2
    corrected = reached[1]
    check = Correction(
        id="pending-rewrite",
        note=corrected,
        outcome=Outcome.PARTLY,
        rewrite="Retention is fourteen days.",
        reason="Compared original.",
        against=(original.identity,),
    )
    assert correct(
        engine.store,
        instance,
        check,
        Rewrite(
            query_vector=(0.0, 0.0, 1.0, 0.0),
            passage_vector=(0.0, 0.0, 1.0, 0.0),
            embedded_with="synthetic-encoder",
        ),
    )
    before = {row["id"]: engine.store.note(instance, row["id"]) for row in rows}

    def unexpected_embedding(_):
        pytest.fail("replay must not call the encoder")

    monkeypatch.setattr(loader.encoder, "embed_note", unexpected_embedding)
    replay_loader = MemoryLoader(loader.store, loader.sources, loader.extraction, loader.encoder)
    replay = replay_loader.window(window, parts, "fixture-run")
    assert replay["inserted"] == 0 and replay["placed_again"] == 2
    assert replay["already_present"] == 3
    for identity, previous in before.items():
        assert engine.store.note(instance, identity) == {
            key: value for key, value in previous.items() if key != "placement_pending"
        }
    assert replay_loader.window(window, parts, "fixture-run")["placed_again"] == 0


@pytest.mark.parametrize("memory_workflow", ["unloaded-three"], indirect=True)
def test_merge_occurrences_precede_placement_and_replay_does_not_rejudge(
    memory_workflow, model_service, monkeypatch
):
    instance, original, engine, _, loader, window, parts, _ = memory_workflow
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    policy = merge_policy().model_copy(update={"enabled": True})
    merger = RepeatMerger(engine.store, policy, MergeJudge(provider, policy))
    loader.merger = merger
    model_service.chat_replies.append(
        completion(
            json.dumps(
                {
                    "verdicts": [
                        {"pair": "p1", "verdict": "same_claim"},
                        {"pair": "p2", "verdict": "same_event"},
                        {"pair": "p3", "verdict": "same_event"},
                    ]
                }
            )
        )
    )

    def interrupted(*_):
        raise OSError("synthetic stop before first placement")

    monkeypatch.setattr(loader.topics, "place", interrupted)
    with pytest.raises(OSError, match="first placement"):
        loader.window(window, parts, "fixture-run")
    roots = engine.store.read(
        f"MATCH (n:{engine.store.settings.note_label} {{instance:$instance}}) "
        "RETURN n.note_id AS id",
        instance=instance,
    )
    assert len(roots) == 1
    root = roots[0]["id"]
    assert len(merger.graph.occurrences(instance, root)) == 2
    assert engine.store.note(instance, root)["said_again"] == 1
    calls = len(model_service.calls)
    replay = MemoryLoader(loader.store, loader.sources, loader.extraction, loader.encoder, merger)
    result = replay.window(window, parts, "fixture-run")
    assert result["already_present"] == 3 and result["inserted"] == 0
    assert result["placed_again"] == 1 and result["merge_judged"] == 0
    assert len(model_service.calls) == calls
    opened = engine.sources.open_note(engine.store, instance, root, include_raw=True)
    assert len(opened["occurrences"]) == 2
    assert all(row["judge_against"] == [original.identity] for row in opened["occurrences"])


def test_store_find_source_correct_and_replay(memory_workflow):
    instance, original, engine, server, loader, window, parts, archive = memory_workflow
    found = engine.search(instance, "fourteen days")
    note = found["notes"][0]
    assert note["text"] == "Retention is seven days."
    opened = engine.sources.open_note(engine.store, instance, note["id"], include_raw=True)
    assert opened["sources"][0]["source"] == original.source
    correction = Correction.model_validate_json(
        json.dumps(
            {
                "id": "rewrite-one",
                "note": note["id"],
                "outcome": "partly",
                "reason": "Original says fourteen.",
                "against": [original.identity],
                "rewrite": "Retention is fourteen days.",
            }
        )
    )
    assert engine.check(instance, correction)["applied"] is True
    requests = len(server.calls)
    assert engine.check(instance, correction)["applied"] is False
    assert len(server.calls) == requests
    assert loader.window(window, parts, "fixture-run")["already_present"] == 1
    assert engine.search(instance, "fourteen days")["notes"][0]["text"] == correction.rewrite
    assert history(engine.store, instance, note["id"])[0]["before_source_ids"] == [
        original.identity
    ]
    archive.verify([original])


def test_http_ownership_flags_and_source(memory_workflow):
    instance, original, engine, _, _, _, _, archive = memory_workflow
    settings = load_service()
    client = TestClient(
        create_memory_app(engine, settings, {instance: "fixture-token", "other": "other-token"})
    )
    own = {"Authorization": "Bearer fixture-token"}
    other = {
        "Authorization": "Bearer other-token",
        "X-Assistant": instance,
        "X-Forwarded-User": instance,
    }
    assert client.get(settings.health_path).status_code == 401
    response = client.post(settings.search_path, headers=own, json={"question": "fourteen days"})
    assert response.status_code == 200, response.text
    identity = response.json()["notes"][0]["id"]
    assert (
        client.post(settings.source_path, headers=own, json={"note": identity, "raw": True}).json()[
            "sources"
        ][0]["source"]
        == original.source
    )
    compact = client.post(settings.source_path, headers=own, json={"note": identity}).json()
    assert "sources" not in compact
    assert compact["context"]["status"] == "available"
    assert compact["rows"][0]["record"] == original.identity
    assert (
        client.post(
            settings.source_path, headers=own, json={"note": identity, "raw": "false"}
        ).status_code
        == 422
    )
    assert (
        client.post(settings.note_path, headers=other, json={"note": identity}).status_code == 403
    )
    body = {
        "id": "check",
        "note": identity,
        "outcome": "wrong",
        "reason": "synthetic",
        "against": [original.identity],
    }
    for flag in ("delete", "useful"):
        for value in ("false", "true", "", 0, 1, [], {}):
            result = client.post(settings.correction_path, headers=own, json={**body, flag: value})
            assert result.status_code == 422, result.text
    assert history(engine.store, instance, identity) == []
    assert (
        client.post(
            settings.correction_path, headers=other, json={**body, "delete": True}
        ).status_code
        == 403
    )
    result = client.post(settings.correction_path, headers=own, json={**body, "delete": True})
    assert result.status_code == 200, result.text
    assert (
        client.post(settings.correction_path, headers=own, json={**body, "delete": True}).json()[
            "applied"
        ]
        is False
    )
    assert client.post(settings.history_path, headers=own, json={"note": identity}).json()[
        "checks"
    ][0]["deleted_note"]
    archive.verify([original])
    schema = client.get(settings.openapi_path, headers=own).json()
    assert "Correction" in schema["components"]["schemas"]
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_source_corruption_is_not_a_successful_read(memory_workflow):
    instance, original, engine, _, _, _, _, archive = memory_workflow
    archive.database[archive.settings.collections["conversation"]].update_one(
        {"_id": original.identity}, {"$set": {"source.message": "changed after import"}}
    )
    with pytest.raises(SetupError, match="corrupt"):
        engine.sources.records(instance, (original.identity,))


def _cli_options(instance, engine, server, archive, tmp_path, notes_port=None):
    address = engine.store.driver.get_server_info().address
    mongo_uri = f"mongodb://127.0.0.1:{archive.database.client.address[1]}"
    values = {
        ("archive/mongodb", "uri"): mongo_uri,
        ("graph/local", "username"): "fixture",
        ("graph/local", "password"): "fixture-password",
        ("encoder/local", "token"): "fixture-search-token",
    }
    keyring = tmp_path / "fixture-keyring"
    keyring.write_text(
        f"#!{sys.executable}\nimport sys\nvalues={values!r}\n"
        "entry=sys.argv[sys.argv.index('entry')+1]\n"
        "field=sys.argv[sys.argv.index('field')+1]\nsys.stdout.write(values[(entry,field)])\n",
        encoding="utf-8",
    )
    keyring.chmod(0o700)
    setup = load_config().model_dump(mode="json")
    setup["services"]["archive"]["port"] = archive.database.client.address[1]
    setup["services"]["graph"]["port"] = address[1]
    setup["services"]["encoder"]["port"] = server.server.server_port
    if notes_port is not None:
        setup["services"]["notes"]["port"] = notes_port
    setup["keyring"]["executable"] = str(keyring)
    policies = {
        "config": setup,
        "archive-config": {
            **archive.settings.model_dump(mode="json"),
            "database": archive.database.name,
        },
        "memory-config": engine.store.settings.model_dump(mode="json"),
        "search-config": engine.encoder.policy.model_dump(mode="json"),
        "retrieval-config": {**load_retrieval().model_dump(mode="json"), "rerank_enabled": False},
    }
    options = ["--instance", instance]
    for name, policy in policies.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        options += [f"--{name}", str(path)]
    return options


def test_actual_cli_store_search_source_history(memory_workflow, tmp_path, capsys):
    instance, original, engine, server, _, _, _, archive = memory_workflow
    options = _cli_options(instance, engine, server, archive, tmp_path)
    assert main(["search", "fourteen", *options]) == 0
    found = json.loads(capsys.readouterr().out)
    identity = found["notes"][0]["id"]
    assert main(["source", "--note", identity, "--raw", *options]) == 0
    assert json.loads(capsys.readouterr().out)["sources"][0]["source"] == original.source
    assert main(["source", "--note", identity, *options]) == 0
    compact = json.loads(capsys.readouterr().out)
    assert "sources" not in compact
    assert compact["rows"][0]["record"] == original.identity
    assert main(["history", "--note", identity, *options]) == 0
    assert json.loads(capsys.readouterr().out) == {"checks": []}
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps(original.source) + "\n", encoding="utf-8")
    assert (
        main(
            [
                "store",
                "--file",
                str(path),
                "--session",
                "session-one",
                "--closed",
                "--run",
                "fixture-run",
                "--apply",
                *options,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["windows"][0]["already_present"] == 1


@pytest.mark.parametrize("memory_workflow", ["unloaded-three"], indirect=True)
def test_actual_cli_enabled_merge_source_and_unmerge(
    memory_workflow, model_service, tmp_path, capsys
):
    instance, original, engine, server, _, _, _, archive = memory_workflow
    options = _cli_options(instance, engine, server, archive, tmp_path, model_service.port)
    policy = tmp_path / "merge.json"
    policy.write_text(
        merge_policy().model_copy(update={"enabled": True}).model_dump_json(), encoding="utf-8"
    )
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps(original.source) + "\n", encoding="utf-8")
    model_service.chat_replies.append(
        completion(
            json.dumps(
                {
                    "verdicts": [
                        {"pair": "p1", "verdict": "same_claim"},
                        {"pair": "p2", "verdict": "same_event"},
                        {"pair": "p3", "verdict": "same_event"},
                    ]
                }
            )
        )
    )
    store = [
        "store",
        "--file",
        str(path),
        "--session",
        "session-one",
        "--closed",
        "--run",
        "fixture-run",
        "--apply",
        "--merge-config",
        str(policy),
        "--merge-model-file",
        str(model_service.model_file),
        *options,
    ]
    assert main(store) == 0
    result = json.loads(capsys.readouterr().out)["windows"][0]
    assert (result["inserted"], result["repeats"], result["retellings"]) == (1, 1, 1)
    root = engine.store.read(
        f"MATCH (n:{engine.store.settings.note_label} {{instance:$owner}}) RETURN n.note_id AS id",
        owner=instance,
    )[0]["id"]
    assert main(["source", "--note", root, *options]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["also_said_count"] == 1 and shown["retold_count"] == 1
    alias = shown["occurrences"][0]["alias"]
    assert main(["unmerge", "--note", alias, "--reason", "Wrong merge", *options]) == 0
    assert json.loads(capsys.readouterr().out)["undone"] is True
    assert main(store) == 0
    replay = json.loads(capsys.readouterr().out)["windows"][0]
    assert replay["already_present"] == 3 and replay["merge_judged"] == 0
    assert len(model_service.calls) == 1


def test_source_lookup_rejects_oversized_content(memory_workflow):
    instance, original, _, _, _, _, _, archive = memory_workflow
    bounded = SourceLookup(archive, 1, 10)
    with pytest.raises(SetupError, match="byte limit"):
        bounded.records(instance, (original.identity,))
