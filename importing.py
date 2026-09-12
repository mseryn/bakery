#!/usr/bin/env python3
"""Reading a parbake output directory into the database.

parbake writes three things per CSV: a Croissant, a text report and a markdown
report. Only the Croissant is read here. The other two are generated FROM it and
hold nothing it does not, so importing them would be importing the same facts
twice and giving the copies a chance to disagree. They are recorded as files
that exist -- they go stale the moment the Croissant is finished, and something
has to know which files those are -- and never opened.

    <output>/parbaked_croissants/NAME.parbaked.json     read, and the master
    <output>/parbaked_txt/NAME.txt                      recorded, never opened
    <output>/parbaked_markdown/NAME.parbaked.md         recorded, never opened

## What an import decides, and what it refuses to

It records what is measurable: the columns, their order, what parbake saw in
them, how many rows, where the data was. It works out a machine and a kind from
the filename and from the MACHINE_NAME column, and then puts them in
`machine_suggestion` rather than acting on them.

That line matters. A machine is what every description is scoped to, so a wrong
one silently attaches sixty-six descriptions to the wrong system. The evidence
for a guess is shown to a person and confirmed once, which costs one keystroke
and cannot go quietly wrong. It is the same posture parbake takes about data
types.

## Importing twice is normal

Files get re-measured -- a fuller scan, a new column, a corrected export -- so
an import updates rather than duplicates. A file is matched by the dataset name
inside its Croissant, not by path, so moving or copying the output directory
does not produce a second row for the same dataset.

When the Croissant has changed, the columns are brought into line with it: new
ones added, departed ones removed, measurements refreshed. Nothing a person said
is touched. Descriptions belong to the machine, not the file, so a column that
vanishes from one export keeps its meaning for the next one that has it.
"""

import calendar
import hashlib
import json
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from pathlib import Path

from sqlalchemy import select

from bake_croissants import PARBAKED_SUFFIX, find_parbaked_files
from models import FileField, SourceFile, now
from parbake_link import MARKDOWN_SUBDIRECTORY, TEXT_SUBDIRECTORY
from questions import is_parbaked

# ANL-ALCF-DJC-POLARIS_20220809_20221231 -- a start and an end.
DATE_RANGE = re.compile(r"_(\d{8})_(\d{8})$")
# aurora_crayex_telemetry_power_2024-05-02 -- one day.
SINGLE_DAY = re.compile(r"_(\d{4})-(\d{2})-(\d{2})$")
# anonymized_aurora_dim_job_comp_2026-01 -- a month.
SINGLE_MONTH = re.compile(r"_(\d{4})-(\d{2})$")


# --- what a filename says --------------------------------------------------

@dataclass
class NameParts:
    """What can be read off a filename. All of it a guess, none of it recorded as fact."""

    stem: str
    kind: str | None = None
    machine: str | None = None
    covers_from: date | None = None
    covers_to: date | None = None


def _last_day_of(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def split_off_dates(stem):
    """The name without its date suffix, and the range that suffix names.

    Three shapes appear in the corpus, so three are handled. A name in none of
    them keeps its dates unknown, which is not a failure -- the dates only ever
    suggest which iteration of a machine a file belongs to.
    """
    found = DATE_RANGE.search(stem)
    if found:
        try:
            starts = date(int(found[1][:4]), int(found[1][4:6]), int(found[1][6:]))
            ends = date(int(found[2][:4]), int(found[2][4:6]), int(found[2][6:]))
        except ValueError:
            return stem, None, None     # eight digits that are not a date
        return stem[: found.start()], starts, ends

    found = SINGLE_DAY.search(stem)
    if found:
        try:
            day = date(int(found[1]), int(found[2]), int(found[3]))
        except ValueError:
            return stem, None, None
        return stem[: found.start()], day, day

    found = SINGLE_MONTH.search(stem)
    if found:
        try:
            first = date(int(found[1]), int(found[2]), 1)
        except ValueError:
            return stem, None, None
        return stem[: found.start()], first, _last_day_of(first.year, first.month)

    return stem, None, None


def shared_prefix_of(stems):
    """The hyphen-separated segments every name in a batch begins with.

    A directory of ALCF exports all start ANL-ALCF: an organisation and a
    facility, not a kind of data. Rather than hard-coding those two, they are
    found by looking at what the batch has in common, which works for a site
    that writes its names differently.

    Needs at least two names to mean anything -- a single file has all of itself
    in common with itself -- and never takes the last segment, which is the
    machine.
    """
    hyphenated = [stem.split("-") for stem in stems if "-" in stem]
    if len(hyphenated) < 2:
        return []

    shared = []
    for position in range(min(len(parts) for parts in hyphenated) - 2):
        segment = hyphenated[0][position]
        if all(parts[position] == segment for parts in hyphenated):
            shared.append(segment)
        else:
            break
    return shared


def parse_name(stem, known_machines=(), shared_prefix=()):
    """Read a machine, a kind and a date range off a filename.

    Two conventions appear in the corpus and both are handled:

        ANL-ALCF-GPU-NODE-POLARIS_20230920_20231231   hyphens, machine last
        aurora_crayex_telemetry_power_2024-05-02      underscores, machine first

    A machine already in the database wins over position, wherever it appears in
    the name. That is what makes the second convention work at all, and it means
    the guesses get better as the database fills up.
    """
    body, covers_from, covers_to = split_off_dates(stem)
    parts = NameParts(stem=stem, covers_from=covers_from, covers_to=covers_to)

    folded = {name.lower(): name for name in known_machines}
    segments = re.split(r"[-_]", body)
    recognised = [segment for segment in segments if segment.lower() in folded]

    if "-" in body:
        hyphenated = body.split("-")
        # A known machine wherever it sits, otherwise the last segment: the
        # convention in every ALCF name here.
        parts.machine = recognised[-1] if recognised else hyphenated[-1]
        remaining = [segment for segment in hyphenated if segment != parts.machine]
        if shared_prefix and remaining[: len(shared_prefix)] == list(shared_prefix):
            remaining = remaining[len(shared_prefix):]
        elif len(remaining) >= 3:
            # No batch to learn from. Two leading segments is what an
            # organisation and a facility take in every name seen here.
            remaining = remaining[2:]
        elif len(remaining) == 2:
            remaining = remaining[1:]
        parts.kind = "-".join(remaining) or None
    else:
        parts.machine = recognised[-1] if recognised else None
        remaining = [segment for segment in segments if segment != parts.machine]
        parts.kind = "_".join(remaining) or None

    return parts


def machine_from_column(document):
    """The machine the data itself names, if it names one.

    A MACHINE_NAME column holding one value for every row is the best evidence
    there is -- better than the filename, which is a label someone typed. Only
    accepted when it really is one value: a column with several is a file
    covering several machines, and this should not pick one of them.
    """
    measured = (document.get("_parbake_measurements") or {}).get("MACHINE_NAME")
    if not measured:
        return None
    common = measured.get("most_common_values") or []
    if len(common) != 1 or measured.get("distinct_count") != 1:
        return None
    value = str(common[0].get("value", "")).strip()
    return value or None


@dataclass
class Suggestion:
    """A guess at the machine, and why. The why is what makes it confirmable."""

    machine: str | None
    evidence: list = dataclass_field(default_factory=list)
    disagrees: bool = False

    def describe(self):
        if not self.machine:
            return "no machine suggested"
        lead = f"{self.machine} -- {'; '.join(self.evidence)}"
        return lead + ("  (THE TWO DISAGREE)" if self.disagrees else "")


def suggest_machine(document, parts):
    """What machine this file looks like it came from, and the evidence for it.

    Where the column and the filename disagree, the column wins and the
    disagreement is reported rather than smoothed over. A file called POLARIS
    whose every row says thetagpu is exactly the kind of thing someone needs to
    look at, and exactly the kind of thing that gets missed if the tool quietly
    picks one.
    """
    from_column = machine_from_column(document)
    from_name = parts.machine

    if from_column and from_name:
        agree = from_column.lower() == from_name.lower()
        return Suggestion(
            machine=from_column,
            evidence=[f"every row of MACHINE_NAME says {from_column!r}",
                      f"the filename says {from_name!r}"],
            disagrees=not agree)
    if from_column:
        return Suggestion(from_column, [f"every row of MACHINE_NAME says {from_column!r}"])
    if from_name:
        return Suggestion(from_name, [f"read off the filename {parts.stem!r}"])
    return Suggestion(None)


# --- reading one par-baked file -------------------------------------------

def sha256_of(path):
    """The checksum of the par-baked JSON -- what says whether it has been re-measured."""
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def columns_in(document):
    """Every column the Croissant describes, in order, with what was measured.

    Reads the first recordSet. parbake writes exactly one; a finished file can
    carry a second holding an enumeration, and that is not a list of columns.
    """
    record_sets = document.get("recordSet") or []
    if not record_sets:
        return []
    measurements = document.get("_parbake_measurements") or {}
    found = []
    for position, field in enumerate(record_sets[0].get("field") or []):
        name = field.get("name")
        if name:
            found.append((name, position, measurements.get(name)))
    return found


def derived_files_for(croissant_path, stem):
    """The .txt and .md parbake wrote beside this Croissant, if they are there.

    Located through parbake's own directory names, so a rename there does not
    silently stop them being found here.
    """
    output_root = croissant_path.parent.parent
    candidates = [
        (output_root / TEXT_SUBDIRECTORY / f"{stem}.txt", "txt"),
        (output_root / MARKDOWN_SUBDIRECTORY / f"{stem}.parbaked.md", "md"),
    ]
    return [(path, kind) for path, kind in candidates if path.is_file()]


# --- putting it in the database -------------------------------------------

@dataclass
class ImportReport:
    """What one import did. Counted rather than printed, so a TUI can show it too."""

    added: list = dataclass_field(default_factory=list)
    remeasured: list = dataclass_field(default_factory=list)
    unchanged: list = dataclass_field(default_factory=list)
    skipped: list = dataclass_field(default_factory=list)       # (name, why)
    columns_added: dict = dataclass_field(default_factory=dict)
    columns_removed: dict = dataclass_field(default_factory=dict)
    disagreements: list = dataclass_field(default_factory=list)  # (name, description)
    derived_recorded: int = 0

    @property
    def total(self):
        return len(self.added) + len(self.remeasured) + len(self.unchanged)

    def lines(self):
        """The report as something worth reading, shortest first."""
        out = [f"{self.total} file(s) in the database "
               f"-- {len(self.added)} new, {len(self.remeasured)} re-measured, "
               f"{len(self.unchanged)} unchanged"]
        if self.derived_recorded:
            out.append(f"{self.derived_recorded} text and markdown sibling(s) recorded, "
                       "not read")
        for name, columns in self.columns_added.items():
            out.append(f"{name}: {len(columns)} new column(s): {', '.join(sorted(columns))}")
        for name, columns in self.columns_removed.items():
            out.append(f"{name}: {len(columns)} column(s) gone: {', '.join(sorted(columns))}")
        for name, why in self.disagreements:
            out.append(f"{name}: {why}")
        for name, why in self.skipped:
            out.append(f"skipped {name}: {why}")
        return out


def known_machine_names(session):
    """Every machine already named, so the guesses improve as the database fills."""
    from models import Machine
    return list(session.scalars(select(Machine.name)))


def _sync_columns(session, stored, document, report, is_new):
    """Bring the recorded columns into line with what the Croissant now says.

    Only touches measurements. What a person said about a column lives against
    the machine, so a column disappearing from one export does not throw away
    its meaning for the next export that has it.

    A change is only worth reporting for a file that was already there. Listing
    all 266 columns of a file being seen for the first time is a count dressed
    up as news, and it buries the one line that matters -- the export that
    gained three columns since last week.
    """
    measured = {name: (position, values) for name, position, values in columns_in(document)}
    existing = {column.name: column for column in stored.fields}

    appeared = set(measured) - set(existing)
    departed = set(existing) - set(measured)

    for name in departed:
        session.delete(existing[name])
    for name, (position, values) in measured.items():
        if name in existing:
            existing[name].position = position
            existing[name].measurements = values
        else:
            session.add(FileField(file_id=stored.id, name=name,
                                  position=position, measurements=values))

    stored.column_count = len(measured)
    if is_new:
        return
    if appeared:
        report.columns_added[stored.name] = sorted(appeared)
    if departed:
        report.columns_removed[stored.name] = sorted(departed)


def _sync_derived(session, stored, croissant_path, report):
    """Record the .txt and .md siblings. Recorded, not read."""
    from models import DerivedFile
    already = {each.path for each in stored.derived}
    for path, kind in derived_files_for(croissant_path, stored.name):
        if str(path) not in already:
            session.add(DerivedFile(file_id=stored.id, path=str(path), format=kind))
            report.derived_recorded += 1


def import_one(session, croissant_path, report, known_machines=(), shared_prefix=()):
    """Import a single par-baked Croissant. Returns the SourceFile, or None if skipped."""
    croissant_path = Path(croissant_path)
    try:
        document = json.loads(croissant_path.read_text())
    except (OSError, json.JSONDecodeError) as problem:
        report.skipped.append((croissant_path.name, f"could not be read: {problem}"))
        return None

    if not is_parbaked(document):
        # A finished Croissant is somebody's work, not a measurement. Reading it
        # is a different job with a different question attached -- what to do
        # with the answers it already contains -- so it is not done quietly here.
        report.skipped.append(
            (croissant_path.name,
             "not par-baked. A finished Croissant is seeded, not imported."))
        return None

    name = document.get("name") or croissant_path.name[: -len(PARBAKED_SUFFIX)]
    provenance = document.get("_parbake") or {}
    parts = parse_name(name, known_machines, shared_prefix)
    suggestion = suggest_machine(document, parts)
    checksum = sha256_of(croissant_path)

    stored = session.scalar(select(SourceFile).where(SourceFile.name == name))
    is_new = stored is None
    if is_new:
        stored = SourceFile(name=name, parbaked_path=str(croissant_path),
                            parbaked_sha256=checksum)
        session.add(stored)
        session.flush()      # so the columns below have a file_id to point at

    changed = is_new or stored.parbaked_sha256 != checksum

    # Where it was last seen, and what the filename says, are refreshed every
    # time. They cost nothing and a moved directory should not go stale.
    stored.parbaked_path = str(croissant_path)
    stored.parbaked_sha256 = checksum
    stored.kind = parts.kind
    stored.covers_from = parts.covers_from
    stored.covers_to = parts.covers_to
    stored.machine_suggestion = suggestion.machine
    stored.source_csv_path = provenance.get("source_file")
    stored.source_size_bytes = provenance.get("source_size_in_bytes")
    stored.rows = provenance.get("rows_read")

    if changed:
        _sync_columns(session, stored, document, report, is_new)
        stored.updated_at = now()
    _sync_derived(session, stored, croissant_path, report)

    if suggestion.disagrees:
        report.disagreements.append((name, suggestion.describe()))

    if is_new:
        report.added.append(name)
    elif changed:
        report.remeasured.append(name)
    else:
        report.unchanged.append(name)
    return stored


def import_target(session, target):
    """Import a parbake output directory, its croissants folder, or one file.

    Whichever of those someone has in their hand should work. Anyone who has
    just run parbake is holding the output root and should not have to know the
    layout, which is the same reasoning bake_croissants uses -- and the same
    function, so the two cannot come to disagree about where files live.
    """
    report = ImportReport()
    croissant_files = find_parbaked_files(target)
    if not croissant_files:
        return report

    known = known_machine_names(session)
    stems = [path.name[: -len(PARBAKED_SUFFIX)] for path in croissant_files]
    shared_prefix = shared_prefix_of(stems)

    for path in croissant_files:
        import_one(session, path, report, known, shared_prefix)

    session.commit()
    return report
