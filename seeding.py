#!/usr/bin/env python3
"""Reading work that has already been done into the database.

Three POLARIS_DJC columns have been described three times: once in
djc_v4.croissant.json, once in POLARIS_DJC_field_dictionary.csv, and once in
POLARIS_DJC_documentation.md. Starting the database empty would mean typing all
of it a fourth time, and the fourth version would be the one that disagrees.

So finished work is read in. Three shapes, each holding something the others do
not:

    *.croissant.json        every field described and typed, the dataset-level
                            answers, and the value decodings
    *_field_dictionary.csv  the UNIT column, which no Croissant property has a
                            slot for -- and which is empty in the Croissant
    *_documentation*.md     section 3, the machine profile: vendor,
                            architecture, CPU, GPU, scheduler, node counts,
                            deployment dates. Nothing else records this at all.

Measured on the corpus: the Croissant describes all 66 POLARIS_DJC fields and
the dictionary only 24, but the dictionary carries every unit. Neither on its
own is the work; the union is.

## Nothing seeded is settled

Everything read here lands with is_confirmed = False and a recorded_by naming
the file it came from. It is real work by a real person -- but nobody has
confirmed it in *this* database against *this* export, and the difference
matters when a description was written for a different date range of the same
dataset.

So a seeded answer is offered at the prompt as a default to accept, edit or
reject. One keystroke, and then it is confirmed. That is also what makes it safe
for seeding to create machines and datasets without asking: nothing it creates
is treated as decided.

## A confirmed answer is never overwritten

Seeding twice, or seeding from two files that disagree, fills gaps and leaves
anything a person has confirmed alone. Where two unconfirmed sources both have
a value, the first one read keeps it and the second is reported rather than
applied -- silently preferring one would make the tool quietly wrong exactly
where two documents disagree, which is exactly where someone needs to look.
"""

import csv
import json
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from pathlib import Path

from sqlalchemy import select

from models import (
    Dataset,
    DatasetAnswer,
    Enumeration,
    EnumerationValue,
    FieldDoc,
    Machine,
    MachineIteration,
    MachinePartition,
    SEEDED_BY_PREFIX,
    machine_key,
)

# The dataset-level keys worth carrying across. Deliberately the same keys
# questions.py asks about, so a seeded answer and a typed one are the same thing.
DATASET_KEYS = [
    "description", "version", "url", "datePublished", "creator", "publisher",
    "license", "citeAs", "keywords", "rai:dataCollectionType",
    "rai:hasSyntheticData", "rai:dataLimitations", "rai:dataBiases",
    "rai:personalSensitiveInformation", "rai:dataUseCases", "rai:dataSocialImpact",
]

# How the field dictionaries write a type, and what Croissant calls it. Order
# matters: "string (ISO 8601 datetime)" is a datetime before it is a string.
def croissant_type_for(written):
    """The Croissant type a dictionary's DTYPE column means, or None.

    None rather than a guess. A type nobody recognises is a question for a
    person, and sc:Text is what a wrong guess looks like.
    """
    if not written:
        return None
    lowered = written.strip().lower()
    if "datetime" in lowered or "timestamp" in lowered:
        return "sc:DateTime"
    if lowered.startswith("date") or " date" in lowered:
        return "sc:Date"
    if lowered.startswith(("int", "long")):
        return "sc:Integer"
    if lowered.startswith(("float", "double", "decimal", "num")):
        return "sc:Float"
    if lowered.startswith("bool"):
        return "sc:Boolean"
    if lowered.startswith(("str", "text", "object")):
        return "sc:Text"
    return None


# A finished file can still be unfinished in places. djc_v4 carries
# "(no meaning supplied for NODES_USED)" as the description of 38 of its 66
# fields, which is an admission that nobody wrote one -- and seeding it as a
# description would mark all 38 as described and stop them ever being asked
# about. parbake's own placeholders are borrowed for the same reason.
PLACEHOLDER_SHAPE = re.compile(r"^\(\s*(no|none|not|tbd|todo)\b.*\)$",
                               re.IGNORECASE | re.DOTALL)


def looks_like_a_placeholder(text):
    """Is this an admission that nobody wrote a description, dressed as one?"""
    from parbake_link import BANNER, FIELD_PLACEHOLDER
    stripped = (text or "").strip()
    if not stripped:
        return True
    if BANNER in stripped or FIELD_PLACEHOLDER in stripped:
        return True
    return bool(PLACEHOLDER_SHAPE.match(stripped))


# How a meaning says it is not sure. Taken from how the existing decodings are
# actually written, not invented: "PROVISIONAL: ... Open question."
PROVISIONAL_MARKERS = ("provisional", "open question", "ambiguous",
                       "treat as unknown", "confirm before")

# Section 3 of the documentation template, and where each bullet goes.
ITERATION_KEYS = {
    "vendor": "vendor",
    "architecture": "architecture",
    "scheduler": "scheduler",
    "system partition": "partition_summary",
}
PARTITION_KEYS = {
    "cpu": "cpu",
    "gpu": "gpu",
    "interconnect": "interconnect",
    "memory type": "memory_type",
    "storage": "storage",
    "node count": "node_count",
    "rack count": "rack_count",
}
MONTHS = {name.lower(): number for number, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


@dataclass
class SeedReport:
    """What seeding did, counted so a TUI can show it as well as a terminal."""

    machines: list = dataclass_field(default_factory=list)
    iterations: list = dataclass_field(default_factory=list)
    datasets: list = dataclass_field(default_factory=list)
    fields_added: int = 0
    fields_filled: int = 0          # a gap in an existing seeded row filled in
    fields_kept: int = 0            # already confirmed, left alone
    answers_added: int = 0
    enumerations: list = dataclass_field(default_factory=list)
    unlinked: list = dataclass_field(default_factory=list)      # (enum, why)
    conflicts: list = dataclass_field(default_factory=list)     # (what, why)
    notes: list = dataclass_field(default_factory=list)
    skipped: list = dataclass_field(default_factory=list)       # (file, why)
    sources: list = dataclass_field(default_factory=list)

    def lines(self):
        out = []
        if self.sources:
            out.append(f"read {len(self.sources)} file(s): "
                       f"{', '.join(Path(each).name for each in self.sources)}")
        out.append(f"{self.fields_added} field description(s) seeded, "
                   f"{self.fields_filled} gap(s) filled, "
                   f"{self.fields_kept} left alone as already confirmed")
        if self.answers_added:
            out.append(f"{self.answers_added} dataset-level answer(s) seeded")
        for name in self.machines:
            out.append(f"machine {name} created")
        for name in self.iterations:
            out.append(f"machine profile recorded: {name}")
        for name in self.datasets:
            out.append(f"dataset {name} created")
        for name in self.enumerations:
            out.append(f"decoding table {name} seeded")
        for note in self.notes:
            out.append(note)
        for name, why in self.unlinked:
            out.append(f"{name}: {why}")
        for what, why in self.conflicts:
            out.append(f"DISAGREEMENT -- {what}: {why}")
        for name, why in self.skipped:
            out.append(f"skipped {Path(name).name}: {why}")
        out.append("Nothing seeded is confirmed. Each of these is offered at the "
                   "prompt as a default to accept, edit or reject.")
        return out


# --- working out who and what a file is about ------------------------------

def machine_and_kind_from(label, known_machines=()):
    """Split POLARIS_DJC into a machine and a kind.

    Every finished file here is named <MACHINE>_<KIND>. A machine already in the
    database is recognised wherever it sits; otherwise the first segment is
    taken, which is the convention in all of them.
    """
    segments = [segment for segment in label.split("_") if segment]
    if not segments:
        return None, None
    folded = {name.lower() for name in known_machines}
    for position, segment in enumerate(segments):
        if segment.lower() in folded:
            rest = segments[:position] + segments[position + 1:]
            return segment, "_".join(rest) or None
    return segments[0], "_".join(segments[1:]) or None


def get_or_create_machine(session, name, report):
    """Find a machine by its folded name, or make one.

    Seeding creates machines without asking, which is only safe because nothing
    it records is confirmed. A machine with no confirmed answers against it is a
    name waiting to be checked, not a decision.
    """
    found = session.scalar(select(Machine).where(Machine.key == machine_key(name)))
    if found:
        return found
    made = Machine(name)
    session.add(made)
    session.flush()
    report.machines.append(made.name)
    return made


def get_or_create_dataset(session, machine, name, kind, report):
    found = session.scalar(select(Dataset).where(
        Dataset.machine_id == machine.id, Dataset.name == name))
    if found:
        return found
    made = Dataset(machine_id=machine.id, name=name, kind=kind)
    session.add(made)
    session.flush()
    report.datasets.append(made.name)
    return made


# --- recording one field, without ever overwriting a person ----------------

def _same_answer(one, other):
    """Is the difference between these two worth a person's attention?

    djc_v4 writes "When the job entered the queue, timestamp Unit: Timestamp."
    where the dictionary writes "When the job entered the queue, timestamp" --
    the same sentence with the unit appended, and the unit now has a column of
    its own. Reporting seven of those as disagreements buries the one that is
    real.
    """
    first = re.sub(r"[\s.]+", " ", (one or "").strip().lower())
    second = re.sub(r"[\s.]+", " ", (other or "").strip().lower())
    if not first or not second:
        return False
    return first in second or second in first


def record_field(session, dataset, name, source, report,
                 description=None, data_type=None, unit=None):
    """Seed one column's description, filling gaps and never overwriting a person.

    Three outcomes, all of them reported:

        nothing recorded yet        a new unconfirmed row
        recorded but unconfirmed    empty columns filled; a disagreement on a
                                    column that already has a value is reported
                                    and NOT applied
        confirmed                   left entirely alone

    The last one is the important one. A person who has sat with the
    measurements in front of them and decided outranks any file.
    """
    if looks_like_a_placeholder(description):
        description = None
    values = {"description": description, "data_type": data_type, "unit": unit}
    values = {key: value for key, value in values.items() if value}
    if not values:
        return None

    existing = session.scalar(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id, FieldDoc.field_name == name))

    if existing is None:
        session.add(FieldDoc(dataset_id=dataset.id, field_name=name,
                             is_confirmed=False,
                             recorded_by=f"{SEEDED_BY_PREFIX}{Path(source).name}",
                             **values))
        report.fields_added += 1
        return None

    if existing.is_confirmed:
        report.fields_kept += 1
        return existing

    filled = False
    for key, value in values.items():
        current = getattr(existing, key)
        if not current:
            setattr(existing, key, value)
            filled = True
        elif current != value and not _same_answer(current, value):
            report.conflicts.append((
                f"{dataset.name} / {name} / {key}",
                f"{Path(source).name} says {value!r}; kept what was already "
                f"recorded ({existing.recorded_by or 'an earlier source'}) "
                f"saying {current!r}. Confirm which is right."))
    if filled:
        report.fields_filled += 1
    return existing


# --- a finished Croissant --------------------------------------------------

def looks_provisional(meaning):
    """Does this decoding say it is not sure? Taken from how they are written."""
    lowered = (meaning or "").lower()
    return any(marker in lowered for marker in PROVISIONAL_MARKERS)


def seed_enumerations(session, document, machine, source, report):
    """Read the cr:isEnumeration recordSets: what the codes actually mean.

    The links to the fields that use them are deliberately not guessed. djc_v4
    defines exit_code_enum and references it from neither EXIT_CODE nor
    EXIT_STATUS, so the decoding sits in the published file invisible to
    anything reading it. Guessing the link would replace one silent wrong answer
    with another; the candidates are reported instead, and a person picks.
    """
    for record_set in document.get("recordSet") or []:
        if not record_set.get("cr:isEnumeration"):
            continue
        name = record_set.get("name") or record_set.get("@id")
        if not name:
            continue
        if session.scalar(select(Enumeration).where(
                Enumeration.machine_id == machine.id, Enumeration.name == name)):
            continue

        enumeration = Enumeration(machine_id=machine.id, name=name,
                                  description=record_set.get("description"))
        # The inline rows are keyed by the enumeration's own field ids, e.g.
        # "exit_code_enum/code". Which of the two is the code and which is the
        # meaning is read off the field order, not assumed from the names.
        field_ids = [field.get("@id") for field in record_set.get("field") or []]
        if len(field_ids) >= 2:
            code_id, meaning_id = field_ids[0], field_ids[1]
            for position, row in enumerate(record_set.get("data") or []):
                code, meaning = row.get(code_id), row.get(meaning_id)
                if code is None or meaning is None:
                    continue
                enumeration.values.append(EnumerationValue(
                    code=str(code), meaning=str(meaning), position=position,
                    is_provisional=looks_provisional(meaning)))

        session.add(enumeration)
        session.flush()
        report.enumerations.append(name)

        stem = re.sub(r"_enum$", "", name).lower()
        mentioned = {field.get("name")
                     for other in document.get("recordSet") or []
                     if not other.get("cr:isEnumeration")
                     for field in other.get("field") or []}
        candidates = sorted(
            column for column in mentioned
            if column and (stem in column.lower() or column.lower() in stem
                           or column in (enumeration.description or ""))) or None
        report.unlinked.append((
            name,
            "seeded but not linked to a column -- "
            + (f"candidates: {', '.join(candidates)}" if candidates
               else "no obvious column")
            + ". djc_v4 has the same gap; link it when confirming."))


def seed_croissant(session, path, report, machine_name=None):
    """Read a finished Croissant: the fields, the answers and the decodings."""
    path = Path(path)
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as problem:
        report.skipped.append((str(path), f"could not be read: {problem}"))
        return

    from questions import is_parbaked
    if is_parbaked(document):
        report.skipped.append(
            (str(path), "still par-baked. A par-baked file is imported, not seeded."))
        return

    label = document.get("name") or path.stem
    known = list(session.scalars(select(Machine.name)))
    machine_from_label, kind = machine_and_kind_from(label, known)
    machine = get_or_create_machine(session, machine_name or machine_from_label, report)
    dataset = get_or_create_dataset(session, machine, label, kind, report)
    report.sources.append(str(path))

    for key in DATASET_KEYS:
        if key not in document:
            continue
        if session.scalar(select(DatasetAnswer).where(
                DatasetAnswer.dataset_id == dataset.id, DatasetAnswer.key == key)):
            continue
        session.add(DatasetAnswer(
            dataset_id=dataset.id, key=key, answer=document[key],
            is_confirmed=False,
            recorded_by=f"{SEEDED_BY_PREFIX}{path.name}"))
        report.answers_added += 1

    # The columns live in the recordSet that is not an enumeration.
    for record_set in document.get("recordSet") or []:
        if record_set.get("cr:isEnumeration"):
            continue
        for field in record_set.get("field") or []:
            name = field.get("name")
            if name:
                record_field(session, dataset, name, path, report,
                             description=field.get("description"),
                             data_type=field.get("dataType"))

    seed_enumerations(session, document, machine, path, report)


# --- a field dictionary ----------------------------------------------------

# The dictionaries were written by hand over time and do not agree on a header.
# Both spellings are accepted rather than one being declared correct.
FIELD_COLUMN = "FIELD"
TYPE_COLUMNS = ("DTYPE", "DATATYPE")
UNIT_COLUMN = "UNIT"
MEANING_COLUMN = "MEANING"

DICTIONARY_SUFFIX = re.compile(r"_field_dictionary.*$", re.IGNORECASE)
DOCUMENTATION_SUFFIX = re.compile(r"_documentation.*$", re.IGNORECASE)


def seed_field_dictionary(session, path, report, machine_name=None):
    """Read a FIELD/DTYPE/UNIT/MEANING dictionary.

    This is where units come from. Croissant has no property for one, so the
    dictionaries are the only record that RUNTIME_SECONDS is in seconds and
    USED_CORE_HOURS is not -- and the Croissant, which has all 66 descriptions,
    has none of the units. Reading both is what makes either complete.
    """
    path = Path(path)
    try:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error, UnicodeDecodeError) as problem:
        report.skipped.append((str(path), f"could not be read: {problem}"))
        return

    if not rows or FIELD_COLUMN not in rows[0]:
        report.skipped.append(
            (str(path), f"no {FIELD_COLUMN} column -- not a field dictionary"))
        return

    label = DICTIONARY_SUFFIX.sub("", path.stem)
    known = list(session.scalars(select(Machine.name)))
    machine_from_label, kind = machine_and_kind_from(label, known)
    machine = get_or_create_machine(session, machine_name or machine_from_label, report)
    dataset = get_or_create_dataset(session, machine, label, kind, report)
    report.sources.append(str(path))

    type_column = next((each for each in TYPE_COLUMNS if each in rows[0]), None)

    for row in rows:
        name = (row.get(FIELD_COLUMN) or "").strip()
        if not name:
            continue
        record_field(
            session, dataset, name, path, report,
            description=(row.get(MEANING_COLUMN) or "").strip() or None,
            data_type=croissant_type_for(row.get(type_column) if type_column else None),
            unit=(row.get(UNIT_COLUMN) or "").strip() or None)


# --- a documentation markdown ----------------------------------------------

def _as_count(written):
    """10,624 -> 10624. Anything that is not a plain count stays None."""
    cleaned = (written or "").replace(",", "").strip()
    return int(cleaned) if cleaned.isdigit() else None


def parse_deployment_dates(written):
    """"August 2022 - present" -> a start date, and whether it is still running.

    Returns (starts_on, ends_on). A month with no day becomes the first of it,
    and the prose it came from is kept alongside rather than thrown away, so
    nothing here claims more precision than the document had.
    """
    text = (written or "").strip()
    if not text:
        return None, None

    def one_date(part):
        part = part.strip().rstrip(".")
        found = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", part)      # 27 January 2025
        if found and found[2].lower() in MONTHS:
            return date(int(found[3]), MONTHS[found[2].lower()], int(found[1]))
        found = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", part)    # January 27, 2025
        if found and found[1].lower() in MONTHS:
            return date(int(found[3]), MONTHS[found[1].lower()], int(found[2]))
        found = re.search(r"([A-Za-z]+)\s+(\d{4})", part)                  # August 2022
        if found and found[1].lower() in MONTHS:
            return date(int(found[2]), MONTHS[found[1].lower()], 1)
        found = re.search(r"(\d{4})-(\d{2})-(\d{2})", part)
        if found:
            return date(int(found[1]), int(found[2]), int(found[3]))
        return None

    halves = re.split(r"\s+(?:-|--|–|to)\s+", text, maxsplit=1)
    starts = one_date(halves[0])
    if len(halves) == 1:
        return starts, None
    tail = halves[1].strip().lower()
    if tail.startswith(("present", "ongoing", "now", "current")):
        return starts, None                  # an open end is what "present" means
    return starts, one_date(halves[1])


def read_section(text, heading_words):
    """The bullet lines of one numbered section, e.g. "3. System".

    The documents number their headings and the numbers move -- System is
    section 3 in two of them and the power telemetry document renumbers
    everything after it. So the heading is matched on its words and its number
    is ignored.
    """
    pattern = re.compile(
        r"^#{1,4}\s*(?:\d+\.?\s*)?" + re.escape(heading_words) + r"\s*$",
        re.IGNORECASE | re.MULTILINE)
    found = pattern.search(text)
    if not found:
        return []
    rest = text[found.end():]
    next_heading = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
    body = rest[: next_heading.start()] if next_heading else rest
    return [line.rstrip() for line in body.splitlines() if line.strip().startswith("-")]


def parse_system_section(lines):
    """Section 3 as a profile and its partitions.

    Polaris writes its hardware as flat bullets because every node is identical.
    Aurora writes "Partition One:" and indents the same bullets underneath. Both
    end up here as a profile plus one or more partitions, so a homogeneous
    machine is simply the case of having one.
    """
    profile = {"system_name": None, "deployment": None}
    partitions = []
    current = {}

    def start_partition(name):
        nonlocal current
        current = {"name": name}
        partitions.append(current)

    for line in lines:
        indented = len(line) - len(line.lstrip())
        stripped = line.lstrip()[1:].strip()          # drop the bullet
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key, value = key.strip().lower(), value.strip()

        if key.startswith("partition ") and not value:
            start_partition(stripped.rstrip(":").strip())
            continue
        if key == "system name":
            profile["system_name"] = value
        elif key.startswith("system deployment"):
            profile["deployment"] = value
        elif key in ITERATION_KEYS:
            profile[ITERATION_KEYS[key]] = value
        elif key in PARTITION_KEYS:
            if not partitions or (indented == 0 and PARTITION_KEYS[key] in current):
                start_partition("default" if not partitions else f"partition {len(partitions) + 1}")
            column = PARTITION_KEYS[key]
            current[column] = (_as_count(value) if column.endswith("_count") else value or None)

    return profile, partitions


def seed_documentation(session, path, report, machine_name=None):
    """Read section 3 of a documentation file: what the machine is made of.

    The only source for any of it. A par-baked file cannot know a node count and
    a Croissant has nowhere to put one, so without this the machine profile is
    typed by hand or not recorded at all.

    Read as one iteration, dated from the deployment line and left open-ended
    where the document says "present". A machine that already has a profile is
    left alone: changing hardware is what next_iteration is for, and it is not
    something a re-read of a document should do on its own.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as problem:
        report.skipped.append((str(path), f"could not be read: {problem}"))
        return

    lines = read_section(text, "System")
    if not lines:
        report.skipped.append((str(path), "no System section -- nothing here to seed"))
        return

    profile, partitions = parse_system_section(lines)
    label = DOCUMENTATION_SUFFIX.sub("", path.stem)
    known = list(session.scalars(select(Machine.name)))
    from_label, _ = machine_and_kind_from(label, known)
    # The document's own "System name" is better evidence than its filename.
    name = machine_name or profile["system_name"] or from_label
    if not name:
        report.skipped.append((str(path), "no machine named in the System section"))
        return

    machine = get_or_create_machine(session, name, report)
    report.sources.append(str(path))

    organisation = read_section(text, "Record metadata")
    for line in organisation:
        stripped = line.lstrip()[1:].strip()
        if stripped.lower().startswith("originating organization") and ":" in stripped:
            machine.organization = machine.organization or stripped.split(":", 1)[1].strip()

    if machine.iterations:
        # Several datasets from one machine each repeat its System section, so
        # this is the normal case rather than a problem. A change of hardware is
        # a new iteration, made deliberately, not something a re-read does.
        report.notes.append(
            f"{machine.name} already has a profile; {path.name} added nothing to it")
        return

    starts_on, ends_on = parse_deployment_dates(profile.get("deployment"))
    iteration = MachineIteration(
        machine_id=machine.id, label="as deployed",
        starts_on=starts_on, ends_on=ends_on,
        dates_as_written=profile.get("deployment"),
        vendor=profile.get("vendor"), architecture=profile.get("architecture"),
        scheduler=profile.get("scheduler"),
        partition_summary=profile.get("partition_summary"))
    iteration.partitions = [
        MachinePartition(
            name=entry.get("name", "default"), position=position,
            cpu=entry.get("cpu"), gpu=entry.get("gpu"),
            interconnect=entry.get("interconnect"),
            memory_type=entry.get("memory_type"), storage=entry.get("storage"),
            node_count=entry.get("node_count"), rack_count=entry.get("rack_count"))
        for position, entry in enumerate(partitions)
    ]
    session.add(iteration)
    session.flush()
    report.iterations.append(
        f"{machine.name} -- {iteration.label}, "
        f"{starts_on or 'no start date'} to {ends_on or 'present'}, "
        f"{len(iteration.partitions)} partition(s)")


# --- everything at once ----------------------------------------------------

def seed_from(session, *targets, machine_name=None):
    """Seed from files and directories, in the order that loses the least.

    Documentation first, because it names the machines and everything else
    attaches to one. Then the Croissants, which have every field described and
    typed. Then the dictionaries, which fill in the units the Croissants have
    nowhere to put.

    That order matters because a confirmed answer is never overwritten and the
    first unconfirmed one wins: reading the richer source first means the
    thinner one fills gaps instead of being reported as a disagreement.
    """
    report = SeedReport()
    documentation, croissants, dictionaries = [], [], []

    for target in targets:
        target = Path(target)
        found = sorted(target.rglob("*")) if target.is_dir() else [target]
        for path in found:
            if not path.is_file():
                continue
            name = path.name.lower()
            if name.endswith(".parbaked.json"):
                continue        # a par-baked file is imported, not seeded
            if name.endswith(".json"):
                croissants.append(path)
            elif name.endswith(".csv") and "field_dictionary" in name:
                dictionaries.append(path)
            elif name.endswith(".md") and "documentation" in name:
                documentation.append(path)

    for path in documentation:
        seed_documentation(session, path, report, machine_name)
    for path in croissants:
        seed_croissant(session, path, report, machine_name)
    for path in dictionaries:
        seed_field_dictionary(session, path, report, machine_name)

    session.commit()
    return report
