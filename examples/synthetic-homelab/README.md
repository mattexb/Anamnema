# Synthetic home-lab session

A short Claude Code session written for testing, in the native transcript format Claude Code
saves (one JSON record per line). Everything in it is invented: the person, the machines and
the events. Nothing here comes from a real conversation.

## What happens in it

Someone sets up a nightly backup with their assistant, in 15 messages each way:

- **ortolan**, a Raspberry Pi 5 with a 1 TB SSD in the garden shed, keeps the copy.
- **quercia**, the house storage box (a NAS), holds about 600 GB of photos.
- **Livia**, the user's sister, keeps her scans on quercia in a folder called `livia-scans`.
- They decide on a nightly copy at 02:00 that never deletes anything from the backup.
- The assistant's first copy goes to the wrong disk; it fixes that and writes down the mistake
  and the rule it learned.

So every part of the memory has something to work on: facts about a person and two machines,
a decision, a mistake with its correction, a lesson, and three lines the assistant marked for
the memory itself (`mistake:`, `learned_lesson:`, `fact:`).

## Try it

Check that it reads, without writing anything (the default is a dry run):

```sh
anamnema import --file examples/synthetic-homelab/session.jsonl \
  --instance example --session 5e1f0a7c-3d2b-4c8e-9a61-7b0d2e4f8c15 --closed
```

`--closed` says the session is finished; the importer adds the closing record itself. Add
`--apply` to copy it into your archive once your setup is configured.

## Regenerate it

`generate.py` writes the same file every time: its record ids come from a fixed namespace, not
from chance, so a regenerated file is byte-for-byte identical.

```sh
python3 examples/synthetic-homelab/generate.py examples/synthetic-homelab/session.jsonl
```

This folder belongs to the repository only. It is not part of the installed package.
