# Subject Naming

Naming adds a separate search representation; it never changes a note's text,
original passage vector, trust or correction history. New notes start pending
with the original passage as their fallback subject vector. Existing notes can
be seeded the same way through `NamingGraph.seed`.

The pure `NamingWriter` validates model answers. `NamingGraph.write` persists
typed judgements only after locking and comparing the exact judged text and
source identities against the current owned note. Stale, absent and foreign
rows are skipped and counted as unwritten; they do not become named. A named
subject needs an embedding; an unknown subject stores no literal placeholder
and uses the original passage vector. Correction immediately clears the stale
subject metadata, resets its vector and leaves naming pending.
Changing the note's source identities without rewriting its text also invalidates
the settled subject and restores its original-vector fallback.

Selection, storage and model generation remain separate. `GraphSearch` offers
an explicit subject representation alongside its unchanged text default. Its
subject word index covers text and subject, and meaning search still includes
both original note passages and later passage pieces; the best score wins.
Subject naming can add recall but cannot remove an original-vector match. Search
uses the text representation by default; the subject representation is chosen
explicitly.

```sh
anamnema name-notes --instance ASSISTANT --pending
anamnema name-notes --instance ASSISTANT --pending --model-file /absolute/model.gguf --apply
```

Without `--apply`, the command reports the selection mode without opening any
store, reading credentials or calling a model. Apply names one bounded pending
batch (or explicit repeated `--note ID` values) using verified attributed archive
context. Repeating pending mode skips notes already judged. `NamingBuilder` also
offers a judgement preview that calls the model but never embeds or writes;
this Python API is distinct from the infrastructure-free CLI dry-run. Missing
context leaves that note pending with a per-note problem and does not stop healthy
notes in its batch. Each apply attempt rotates its notes behind fresh pending
notes, including missing answers; source failures cannot starve the store. Pending
selection excludes inactive and wrong notes. Model/encoder errors remain visible
and leave naming pending. `anamnema nightly` runs `name-notes --pending` for up
to `naming_rounds` batches, after entity linking and card rebuilds. Writes lock
the note before comparing its judged text and source identities.
