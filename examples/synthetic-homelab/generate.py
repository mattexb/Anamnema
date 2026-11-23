"""Write a synthetic Claude Code session in native JSONL: 15 user and 15 assistant turns.

Invented people, machines and events only. It gives every memory stage something to do: facts
about a person and a machine (entity links), a decision, a mistake with its correction and a
principle (lessons), two marked lines for capture, and an ordinary closing exchange.
"""

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

NAMESPACE = uuid.UUID("6b0e8f2a-1c3d-4e5f-8a9b-0c1d2e3f4a5b")
SERIAL = count(1)
SESSION = "5e1f0a7c-3d2b-4c8e-9a61-7b0d2e4f8c15"
CWD = "/home/tester/Projects/homelab"
START = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
TURNS = [
    (
        "Hi. Today we set up the backup for the garden shed server, the little machine called "
        "ortolan. It is a Raspberry Pi 5 with a 1 TB SSD.",
        "Understood: ortolan is the Raspberry Pi 5 in the garden shed with a 1 TB SSD. What should "
        "it back up, and where to?",
    ),
    (
        "It backs up the photo archive from the house NAS, the one we call quercia. Quercia keeps "
        "about 600 GB of photos.",
        "So quercia, the house NAS, holds about 600 GB of photos, and ortolan keeps a second copy "
        "out in the shed. That fits on the 1 TB SSD with room for growth.",
    ),
    (
        "My sister Livia also stores her scans on quercia, under a folder called livia-scans.",
        "Noted: Livia's scans live on quercia in livia-scans. Should they be part of the backup too?",
    ),
    (
        "Yes, include them. Livia asked for it last week.",
        "I will include livia-scans in the same job. One copy of everything on quercia's photo and "
        "scan folders goes to ortolan.",
    ),
    (
        "What tool do you suggest for the copy?",
        "rsync over ssh is enough for a one-way copy. I suggest a nightly run at 02:00, when nobody "
        "uses the NAS, with --delete off so a mistake on quercia never removes the backup copy.",
    ),
    (
        "Good, we decide that: rsync nightly at 02:00, no --delete.",
        "Decision recorded: nightly rsync from quercia to ortolan at 02:00, without --delete.",
    ),
    (
        "Run a first test copy now.",
        "I ran the first copy, but I pointed it at /mnt/backup, which is the SD card, not the SSD. "
        "It filled the card after 30 GB and stopped.",
    ),
    (
        "So the SSD was not mounted?",
        "Right. The SSD mounts at /mnt/ssd, and /mnt/backup is a folder on the SD card. I should "
        "have checked the mount with findmnt before copying. I removed the partial copy from the "
        "card and pointed the job at /mnt/ssd/quercia.",
    ),
    (
        "Please remember that for next time.",
        "mistake: I copied the backup to /mnt/backup on ortolan's SD card instead of the SSD at "
        "/mnt/ssd, because I did not check the mount point before the first copy.",
    ),
    (
        "And the general rule?",
        "learned_lesson: before writing a large copy to a disk, confirm with findmnt that the "
        "target path is on that disk and not on the system card.",
    ),
    (
        "Run the copy again on the SSD.",
        "The second copy finished: 612 GB in 3 hours 40 minutes, all on /mnt/ssd/quercia, and the "
        "SD card is back to 8 GB used.",
    ),
    (
        "How will we know if the nightly job fails?",
        "The job writes its exit code to a log, and a failed run sends a message to your phone "
        "through the existing notification bot. A silent night means success only when the log "
        "has that night's line.",
    ),
    (
        "Note the size for later.",
        "fact: the first full backup of quercia's photos and livia-scans on ortolan took 612 GB on "
        "2026-11-20.",
    ),
    (
        "Anything else to set up today?",
        "Only the 02:00 timer, which is now enabled. Tomorrow morning I will read the log for the "
        "first nightly run.",
    ),
    (
        "Thanks, that is all for tonight.",
        "Good night. The backup of quercia to ortolan runs at 02:00, and I check its log tomorrow.",
    ),
]


def record(kind: str, at: datetime, parent: str | None, content: object) -> dict[str, object]:
    base: dict[str, object] = {
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": CWD,
        "sessionId": SESSION,
        "version": "2.1.330",
        "gitBranch": "main",
        "type": kind,
        "uuid": str(uuid.uuid5(NAMESPACE, f"record-{next(SERIAL)}")),
        "timestamp": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    if kind == "user":
        base["message"] = {"role": "user", "content": content}
    else:
        base["message"] = {
            "id": "msg_" + uuid.uuid5(NAMESPACE, f"message-{base['uuid']}").hex[:24],
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": content}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 80},
        }
        base["requestId"] = "req_" + uuid.uuid5(NAMESPACE, f"request-{base['uuid']}").hex[:24]
    return base


out = Path(sys.argv[1])
lines, parent, at = [], None, START
for user, assistant in TURNS:
    asked = record("user", at, parent, user)
    at += timedelta(seconds=40)
    answered = record("assistant", at, asked["uuid"], assistant)
    at += timedelta(seconds=50)
    parent = answered["uuid"]
    lines += [asked, answered]
out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines), encoding="utf-8")
print(
    json.dumps(
        {
            "file": str(out),
            "session": SESSION,
            "records": len(lines),
            "first": lines[0]["timestamp"],
            "last": lines[-1]["timestamp"],
        }
    )
)
