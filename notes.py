#!/usr/bin/env python3
"""Finding every note somebody left for later, across a directory of Croissants.

A note is the thing a person should come back to: a count that looks wrong, a
meaning that is a guess, a join to check against the accounting table, a column
nobody has been able to explain yet. They are recorded per column and written
into the description as "NOTE: ...", so they travel with the published file and
a reader sees them.

The cost of that is they end up scattered through hundreds of descriptions in
dozens of files. So this reads them back out:

    python3 bakery.py --notes ./baked
    python3 bakery.py --notes ../croissant_files ./baked

It is the answer to "what is left to look at in this corpus?", which is a
different question from "what is unanswered" and cannot be derived from it: a
column with a confident, complete, confirmed description can still have a note
on it saying the number disagrees with the accounting table.

## It reads files, not the database

Deliberately. Notes written by hand into a description -- which is how they were
recorded before there was a field for them -- are found too, because the marker
is the same either way. So is a note in a Croissant nobody here has imported,
or one in a file written by a previous version of this tool. The database is not
the corpus; the files are.

## Everywhere a description can be

    the dataset's own description
    each file or file set in the distribution
    each record set
    each field

All four are scanned, because a note is put wherever the thing it is about is.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

# The marker, fixed, and deliberately shouted. Everything after it is the note,
# up to the next marker if somebody has left two.
#
# Upper case only, and not case-insensitive, because prose contains the word.
# parbake's own banner says "Record the responsible-AI notes: limitations,
# biases, personal information" -- matching that turned every par-baked file in
# the corpus into a file with a note in it. A marker that catches nineteen
# things nobody wrote is worse than no marker.
NOTE_MARKER = re.compile(r"\bNOTES?:\s*")

# Not every .json in a directory is a Croissant, and reading a file to find out
# is cheaper than being told which are.
CROISSANT_HINTS = ("recordSet", "distribution", "conformsTo")


@dataclass
class Note:
    """One note, and enough about where it came from to go and act on it."""

    text: str
    where: str              # "dataset", "file X", "records / EXIT_CODE"
    dataset: str            # the Croissant's name
    path: Path
    unfinished: bool = False    # the file it came from is still par-baked

    def __str__(self):
        return f"{self.dataset} / {self.where}: {self.text}"


def notes_in(description):
    """Every note in one description, in the order they appear.

    A description is "what the column holds. Unit: Watts. NOTE: the top of the
    range looks like a sensor fault." -- so the note is what follows the marker,
    and the prose before it is not a note.
    """
    if not isinstance(description, str) or not description:
        return []
    parts = NOTE_MARKER.split(description)
    return [each.strip() for each in parts[1:] if each and each.strip()]


def _is_parbaked(document):
    conforms = document.get("conformsTo")
    conforms = conforms if isinstance(conforms, list) else [conforms]
    return any(isinstance(each, str) and "PARBAKED" in each for each in conforms)


def notes_in_document(document, path):
    """Every note anywhere in one Croissant."""
    name = document.get("name") or Path(path).name
    unfinished = _is_parbaked(document) or "_unfinished" in document
    found = []

    def collect(description, where):
        for text in notes_in(description):
            found.append(Note(text=text, where=where, dataset=name,
                              path=Path(path), unfinished=unfinished))

    collect(document.get("description"), "dataset")

    for entry in document.get("distribution") or []:
        if isinstance(entry, dict):
            collect(entry.get("description"),
                    f"file {entry.get('name') or entry.get('@id') or '?'}")

    for record_set in document.get("recordSet") or []:
        if not isinstance(record_set, dict):
            continue
        set_name = record_set.get("name") or record_set.get("@id") or "records"
        collect(record_set.get("description"), f"record set {set_name}")
        for field in record_set.get("field") or []:
            if isinstance(field, dict):
                collect(field.get("description"),
                        f"{set_name} / {field.get('name') or field.get('@id') or '?'}")

    return found


def find_notes(*targets):
    """Every note in every Croissant under these paths.

    Returns (notes, scanned, skipped): the notes found, how many Croissants were
    read, and the files that could not be read with the reason. A file that is
    not a Croissant is not a problem and is passed over silently -- a directory
    of output has an index and a lock file in it too.
    """
    found, scanned, skipped = [], 0, []

    for target in targets:
        target = Path(target)
        candidates = ([target] if target.is_file()
                      else sorted(target.rglob("*.json")) if target.is_dir() else [])
        for path in candidates:
            try:
                document = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as problem:
                skipped.append((path, str(problem)))
                continue
            if not isinstance(document, dict) or not any(
                    hint in document for hint in CROISSANT_HINTS):
                continue
            scanned += 1
            found += notes_in_document(document, path)

    return found, scanned, skipped


def by_dataset(notes):
    """The notes grouped by the dataset they are about, for showing."""
    grouped = {}
    for note in notes:
        grouped.setdefault(note.dataset, []).append(note)
    return dict(sorted(grouped.items()))


def report(notes, scanned, skipped):
    """The notes as lines worth reading, grouped, with the unfinished ones marked."""
    if not scanned:
        return ["No Croissant files found. Point at a directory of them, or at one file."]
    if not notes:
        return [f"{scanned} Croissant(s) read. No notes in any of them."]

    lines = [f"{len(notes)} note(s) across {len(by_dataset(notes))} dataset(s), "
             f"from {scanned} Croissant(s) read."]
    for dataset, found in by_dataset(notes).items():
        marker = "  (still par-baked)" if any(note.unfinished for note in found) else ""
        lines.append("")
        lines.append(f"{dataset}{marker}")
        for note in found:
            lines.append(f"    {note.where}")
            lines.append(f"        {note.text}")
    if skipped:
        lines.append("")
        for path, why in skipped:
            lines.append(f"could not read {Path(path).name}: {why}")
    return lines


def notes_recorded_in(session):
    """The notes in the database, which is a different question from the files.

    A note recorded here has not necessarily been written out yet, and a note in
    a file may have come from somewhere this database has never seen. Both are
    worth being able to ask about; this is the one that answers "what have I
    flagged while working?"
    """
    from sqlalchemy import select

    from models import Dataset, FieldDoc

    found = []
    for doc in session.scalars(select(FieldDoc).where(FieldDoc.note.isnot(None))):
        dataset = session.get(Dataset, doc.dataset_id)
        found.append(Note(text=doc.note, where=f"records / {doc.field_name}",
                          dataset=dataset.name if dataset else "?",
                          path=Path(dataset.name if dataset else "?"),
                          unfinished=not doc.is_complete))
    return sorted(found, key=lambda note: (note.dataset, note.where))
