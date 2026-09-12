#!/usr/bin/env python3
"""Turning what has been recorded back into a Croissant.

The database holds the answers; this writes them out. Two shapes, because the
corpus needs two:

    <name>.baked.json     one file, one date range, a concrete FileObject with a
                          computed sha256. The direct successor to
                          <name>.parbaked.json, and the one that can validate.

    <DATASET>.baked.json  the whole series -- every date range of POLARIS_DJC as
                          one cr:FileSet. The shape djc_v4 is, and the one that
                          gets published.

Both carry the same field descriptions, the same decodings and the same
responsible-AI blocks, because those are answers about the data rather than
about a particular copy of it. They differ in the distribution block and in
whether a checksum is a thing that exists.

## The par-baked marker only goes when nothing is left

This is inherited from the older path and is the most important rule here. The
unreal `conformsTo` is the one thing stopping an unfinished file from looking
finished, so it is replaced only when every column is confirmed, every
dataset-level question is answered, and -- for the per-file shape -- a checksum
could actually be computed. Until then the file is written, and written with the
marker still on it, and the reasons are reported.

## A series is a FileSet, not a FileObject with a fake checksum

djc_v4 writes its series as a cr:FileObject carrying
`"sha256": "unknown-not-yet-published"`. That is a placeholder in a field whose
whole purpose is to be verifiable, and a reader has to notice the string to know
it means nothing.

Croissant has cr:FileSet for this exact case: many files matching a pattern,
where a single checksum is not a thing that can exist. So a series is written as
a FileSet with an `includes` glob and no checksum, which is both honest and
standard, and the fields point at it with `fileSet` rather than `fileObject`.

## Notes go in the description too, marked so they can be found again

A note is the thing somebody should come back to: a count that looks wrong, a
meaning that is a guess, a join to check against the accounting table. It is
appended as "NOTE: ..." at the end of the description, which is what a person
reading the published file sees -- and, because the marker is fixed, what
notes.py can find again across a whole directory of Croissants without anybody
having to read them.

## Units go in the description, where djc_v4 puts them

Croissant has no property for a unit. The convention already in the finished
file is to append it to the description -- "... used at that scale. Unit: Core
Hours." -- so that is what is emitted. The unit keeps its own column in the
database, so it is still queryable and nobody has to remember to type it into
prose; the appending happens here, once, on the way out.

## The Markdown is parbake's projection, not a second one

parbake already defines how a Croissant renders to Markdown, decodings and RAI
blocks included. That renderer is borrowed rather than reimplemented, so the
readable copy of a finished file cannot drift from the readable copy of a
par-baked one.
"""

import json
import re
from dataclasses import dataclass, field as dataclass_field
from datetime import date
from pathlib import Path

from sqlalchemy import select

from bake_croissants import FINISHED_CONFORMS_TO, sha256_of
from models import DatasetAnswer, Enumeration, FieldDoc, SourceFile
from outstanding import field_work_for, outstanding_dataset_questions
from parbake_link import render_markdown
from questions import DATASET_QUESTIONS, wrap_answer

BAKED_SUFFIX = ".baked.json"
BAKED_MARKDOWN_SUFFIX = ".baked.md"

# The answers that are objects rather than strings once written out. Recorded as
# whatever they were given as, so a seeded licence that arrived as djc_v4's full
# CreativeWork keeps its name and url, and one typed as a bare string is wrapped
# on the way out.
NEEDS_WRAPPING = ("creator", "publisher", "license")


@dataclass
class BakeReport:
    """What was written, and what is still stopping it from being finished."""

    # The Croissants, which are the output. The Markdown beside each is a
    # projection of it and is listed separately rather than being reported as a
    # second document with its own state -- it has exactly the state of the
    # JSON it was rendered from.
    written: list = dataclass_field(default_factory=list)
    readable: list = dataclass_field(default_factory=list)
    unfinished: dict = dataclass_field(default_factory=dict)   # path -> reasons
    superseded: list = dataclass_field(default_factory=list)
    skipped: list = dataclass_field(default_factory=list)      # (what, why)

    @property
    def finished(self):
        """The ones nothing is outstanding on. The only ones fit to publish."""
        return [path for path in self.written if str(path) not in self.unfinished]

    def lines(self):
        out = [f"{len(self.written)} Croissant(s) written, "
               f"{len(self.finished)} of them finished, "
               f"{len(self.readable)} Markdown rendering(s) beside them"]
        for path in self.written:
            reasons = self.unfinished.get(str(path))
            if reasons:
                out.append(f"  {Path(path).name}: STILL PAR-BAKED -- "
                           f"{len(reasons)} thing(s) outstanding")
                for reason in reasons[:6]:
                    out.append(f"      {reason}")
                if len(reasons) > 6:
                    out.append(f"      ... and {len(reasons) - 6} more")
            else:
                out.append(f"  {Path(path).name}: finished, conformsTo names a real "
                           "Croissant version")
        for path in self.superseded:
            out.append(f"  superseded (par-baked, now stale): {Path(path).name}")
        for what, why in self.skipped:
            out.append(f"  skipped {what}: {why}")
        return out


# --- the pieces ------------------------------------------------------------

def with_unit(description, unit):
    """The description with its unit appended, the way djc_v4 writes it.

    Left alone when the unit is already in the text, which happens for anything
    seeded out of djc_v4 -- it wrote them in by hand, and appending a second
    copy would be the tool arguing with its own source.
    """
    if not unit:
        return description or ""
    text = (description or "").strip()
    if f"unit: {unit.lower()}" in text.lower():
        return text
    if not text:
        return f"Unit: {unit}."
    separator = " " if text.endswith((".", "!", "?")) else ". "
    return f"{text}{separator}Unit: {unit}."


def with_note(description, note):
    """The description with its note appended, marked so it can be found again.

    Last, after the unit, because it is an aside rather than part of what the
    column means. The marker is the fixed string "NOTE:", which is what makes a
    corpus of notes searchable rather than something somebody has to read for.
    """
    if not note:
        return description or ""
    text = (description or "").strip()
    marked = f"NOTE: {note.strip()}"
    if marked.lower() in text.lower():
        return text
    if not text:
        return marked
    separator = " " if text.endswith((".", "!", "?")) else ". "
    return f"{text}{separator}{marked}"


def described(doc):
    """A column's description as it is written out: the prose, its unit, its note."""
    if doc is None:
        return ""
    return with_note(with_unit(doc.description, doc.unit), doc.note)


def field_entry(column_name, doc, record_set_id, source_reference):
    """One cr:Field: what the column is called, means, holds, and comes from.

    `references` is emitted when the column has a decoding table, which is the
    link djc_v4 leaves out -- it defines exit_code_enum and then points nothing
    at it, so the decodings sit in the published file invisible to anything
    reading it.
    """
    entry = {
        "@type": "cr:Field",
        "@id": f"{record_set_id}/{column_name}",
        "name": column_name,
        "description": described(doc),
        "source": source_reference(column_name),
    }
    if doc and doc.data_type:
        entry["dataType"] = doc.data_type
    if doc and doc.enumeration is not None:
        entry["references"] = {
            "field": {"@id": f"{doc.enumeration.name}/code"}}
    return entry


def enumeration_record_set(enumeration):
    """A decoding table as a cr:RecordSet with its values inline.

    The form djc_v4 uses. A meaning recorded as provisional says so in the file
    rather than only in the database: a guess that reads as settled is worse
    than no guess at all.
    """
    name = enumeration.name
    return {
        "@type": "cr:RecordSet",
        "@id": name,
        "name": name,
        "description": enumeration.description or
                       "Value decodings for the codes in this dataset.",
        "cr:isEnumeration": True,
        "key": {"@id": f"{name}/code"},
        "field": [
            {"@type": "cr:Field", "@id": f"{name}/code", "name": "code",
             "description": "The code as it appears in the data.",
             "dataType": "sc:Text"},
            {"@type": "cr:Field", "@id": f"{name}/meaning", "name": "meaning",
             "description": "What the code means. Anything provisional says so.",
             "dataType": "sc:Text"},
        ],
        "data": [
            {f"{name}/code": value.code,
             f"{name}/meaning": (f"PROVISIONAL: {value.meaning}"
                                 if value.is_provisional
                                 and "provisional" not in value.meaning.lower()
                                 else value.meaning)}
            for value in enumeration.values
        ],
    }


def filename_glob_for(dataset):
    """A glob that actually matches the files, rather than the dataset's name.

    The dataset is POLARIS_DJC and its files are ANL-ALCF-DJC-POLARIS_<dates>.csv:
    <MACHINE>_<KIND> and <org>-<facility>-<KIND>-<MACHINE>_<dates> are two
    different conventions, and using the dataset's name as a glob would emit a
    pattern that matches nothing.

    So it is built from the names of the files that are actually in it, with
    their date suffixes taken off. A pattern somebody has recorded by hand wins,
    because they know about the date ranges nobody has imported yet.
    """
    if dataset.filename_pattern:
        return dataset.filename_pattern

    from importing import split_off_dates
    stems = {split_off_dates(each.name)[0] for each in dataset.files}
    if len(stems) == 1:
        return f"{stems.pop()}_*.csv"
    if not stems:
        return f"{dataset.name}_*.csv"
    # Several shapes in one dataset: the shared prefix is as much as can honestly
    # be claimed to match all of them.
    shortest = min(stems, key=len)
    shared = ""
    for position, character in enumerate(shortest):
        if all(stem[position] == character for stem in stems):
            shared += character
        else:
            break
    return f"{shared}*.csv" if shared else "*.csv"


def machine_block(machine, iteration=None):
    """The machine profile, as a block of its own.

    Croissant has nowhere to put a node count, a rack count or an interconnect,
    and section 3 of the documentation template is most of what anybody wants to
    know about where data came from. So it is written out beside the
    measurements parbake left, as a non-standard block that a validator ignores
    and a reader does not have to go and find another document for.
    """
    if machine is None:
        return None
    iteration = iteration or machine.current_iteration
    block = {"name": machine.name}
    if machine.organization:
        block["organization"] = machine.organization
    if iteration is None:
        block["note"] = "No hardware recorded for this machine."
        return block

    block["as_of"] = {
        "label": iteration.label,
        "from": str(iteration.starts_on) if iteration.starts_on else None,
        "to": str(iteration.ends_on) if iteration.ends_on else "present",
        "as_written": iteration.dates_as_written,
    }
    for name in ("vendor", "architecture", "scheduler", "partition_summary"):
        value = getattr(iteration, name)
        if value:
            block[name] = value
    block["partitions"] = [
        {key: value for key, value in {
            "name": partition.name, "cpu": partition.cpu, "gpu": partition.gpu,
            "interconnect": partition.interconnect,
            "memory_type": partition.memory_type, "storage": partition.storage,
            "node_count": partition.node_count, "rack_count": partition.rack_count,
            "notes": partition.notes}.items() if value is not None
        }
        for partition in iteration.partitions
    ]
    return block


def keywords_with_hardware(keywords, machine, iteration=None):
    """The recorded keywords, plus the hardware djc_v4 lists by hand.

    "HPE Apollo Gen10+", "NVIDIA A100" and "PBS Professional" are keywords in
    the finished file because that is what somebody searching the corpus matches
    on. They are already recorded as hardware, so they are added here rather
    than being typed twice and then disagreeing.
    """
    found = list(keywords) if isinstance(keywords, list) else (
        [keywords] if keywords else [])
    if machine is None:
        return found

    iteration = iteration or machine.current_iteration
    extra = [machine.name]
    if iteration is not None:
        extra += [iteration.vendor, iteration.architecture, iteration.scheduler]

    for value in extra:
        if not value:
            continue
        # "NVIDIA A100" is already there and "4x NVIDIA A100 per node" is the
        # same fact spelled longer. One keyword per fact, or a search matches
        # the same dataset three times.
        folded = value.lower()
        if any(folded in each.lower() or each.lower() in folded for each in found):
            continue
        found.append(value)
    return found


def answers_for(session, dataset):
    """Every dataset-level answer, in the catalogue's order, wrapped as Croissant wants."""
    recorded = {row.key: row for row in session.scalars(select(DatasetAnswer).where(
        DatasetAnswer.dataset_id == dataset.id))}
    written = {}
    for spec in DATASET_QUESTIONS:
        found = recorded.get(spec["key"])
        if found is None:
            continue
        value = found.answer
        # An answer that already arrived as an object keeps it: a licence seeded
        # from djc_v4 has a name and a url that wrapping a bare string cannot
        # reconstruct.
        if spec["key"] in NEEDS_WRAPPING and not isinstance(value, dict):
            value = wrap_answer(spec["key"], value)
        written[spec["key"]] = value
    return written


# --- is it finished? -------------------------------------------------------

# Two answers have a shape a validator insists on, and recording them as given
# is how a file with every question answered still fails to validate. A date
# that is not a date is a hard error; a version that is not MAJOR.MINOR.PATCH
# draws an objection. Neither is worth discovering after the par-baked marker
# has already come off, so they are checked as part of being finished.
SEMANTIC_VERSION = re.compile(r"^\d+(\.\d+){1,2}$")


def malformed_answers(answers):
    """The answers whose shape would stop the file validating, and why.

    Only the two the validator actually objects to. This is not a general
    validation pass -- guessing what a licence or a limitation should look like
    is not this module's business -- it is the narrow case where an answer is
    the right answer written in a way nothing downstream can read.
    """
    problems = []

    version = answers.get("version")
    if isinstance(version, str) and version.strip() and not SEMANTIC_VERSION.match(
            version.strip()):
        problems.append(
            f"version is {version!r}, which is not MAJOR.MINOR.PATCH -- "
            "a validator objects to it")

    published = answers.get("datePublished")
    if isinstance(published, str) and published.strip():
        try:
            date.fromisoformat(published.strip())
        except ValueError:
            problems.append(
                f"datePublished is {published!r}, which is not a YYYY-MM-DD date "
                "-- a validator rejects it outright")
    return problems



def reasons_unfinished(session, dataset, source_file=None, checksum_state=None):
    """Everything still stopping this from claiming a real conformsTo.

    Reported rather than counted, because "17 outstanding" tells somebody
    nothing about what to go and do.
    """
    reasons = []

    for spec, found in outstanding_dataset_questions(session, dataset):
        reasons.append(
            f"{spec['key']} is " + ("unanswered" if found is None
                                    else "seeded but not confirmed"))

    reasons += malformed_answers(answers_for(session, dataset))

    files = [source_file] if source_file is not None else list(dataset.files)
    for each in files:
        for work in field_work_for(session, each):
            missing = []
            if not work.description:
                missing.append("a description")
            if not work.data_type:
                missing.append("a type")
            if not missing:
                missing.append("confirming")
            reasons.append(f"{work.name} needs {' and '.join(missing)}")

    if source_file is not None and checksum_state in ("source missing",
                                                      "no source recorded"):
        reasons.append(
            "no sha256 -- a FileObject cannot validate without one "
            f"({checksum_state})")
    return reasons


# --- the two shapes --------------------------------------------------------

def context_from(source_file):
    """The @context parbake already wrote, rather than a second copy of it.

    It is 40 lines of vocabulary mapping and it has to agree with what parbake
    emits, so it is read back off the par-baked file instead of being restated
    here where the two could drift.
    """
    try:
        document = json.loads(Path(source_file.parbaked_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return document.get("@context")


def measurements_from(source_file):
    """What parbake measured, kept as it was.

    A finished file keeps its measurements. What was measured is still worth
    having, and it is the evidence for every description in the file.
    """
    return {column.name: column.measurements for column in source_file.fields
            if column.measurements}


def bake_file(session, source_file, output_directory, report=None):
    """Write one file's finished Croissant: a concrete FileObject with a checksum."""
    report = report or BakeReport()
    dataset = source_file.dataset
    if dataset is None:
        report.skipped.append((source_file.name, "no machine confirmed yet"))
        return report

    machine = dataset.machine
    iteration = source_file.iteration or machine.current_iteration
    context = context_from(source_file)
    if context is None:
        report.skipped.append(
            (source_file.name,
             f"its par-baked file is no longer at {source_file.parbaked_path}, "
             "so the @context cannot be read back"))
        return report

    file_id = f"{source_file.name}.csv"
    document = {"@context": context, "@type": "sc:Dataset", "name": source_file.name}

    answers = answers_for(session, dataset)
    document["description"] = answers.pop("description", "")
    document["conformsTo"] = None           # decided below; kept in position
    for key, value in answers.items():
        document[key] = value
    document["keywords"] = keywords_with_hardware(
        answers.get("keywords"), machine, iteration)

    # contentUrl is the bare filename, as parbake writes it, or the landing page
    # if one has been recorded. Never source_csv_path: that is where the data sat
    # on whichever machine ran the import, which is nobody else's business and
    # will not resolve for them.
    source = {"@type": "cr:FileObject", "@id": file_id, "name": file_id,
              "contentUrl": (dataset.url or file_id),
              "encodingFormat": "text/csv",
              "description": (f"One date range of {dataset.name}"
                              + (f", covering {source_file.covers_from} to "
                                 f"{source_file.covers_to}."
                                 if source_file.covers_from else "."))}
    if source_file.source_size_bytes:
        source["contentSize"] = f"{source_file.source_size_bytes} B"

    checksum_state = "no source recorded"
    if source_file.source_csv_path:
        path = Path(source_file.source_csv_path)
        if path.is_file():
            source["sha256"] = sha256_of(path)
            checksum_state = "added"
        else:
            checksum_state = "source missing"
    document["distribution"] = [source]

    record_sets = [enumeration_record_set(each)
                   for each in used_enumerations(session, dataset)]
    record_sets.append(record_set_for(
        session, dataset, source_file,
        lambda name: {"fileObject": {"@id": file_id},
                      "extract": {"column": name}}))
    document["recordSet"] = record_sets

    profile = machine_block(machine, iteration)
    if profile:
        document["_machine"] = profile
    document["_parbake_measurements"] = measurements_from(source_file)

    reasons = reasons_unfinished(session, dataset, source_file, checksum_state)
    finish(document, reasons)

    path = Path(output_directory) / f"{source_file.name}{BAKED_SUFFIX}"
    write(document, path, report, reasons)
    source_file.baked_path = str(path)
    note_superseded(source_file, report)
    session.commit()
    return report


def bake_dataset(session, dataset, output_directory, report=None):
    """Write the series' Croissant: one cr:FileSet standing for every date range."""
    report = report or BakeReport()
    files = list(dataset.files)
    if not files:
        report.skipped.append((dataset.name, "no files belong to it yet"))
        return report

    machine = dataset.machine
    context = next((found for found in (context_from(each) for each in files)
                    if found), None)
    if context is None:
        report.skipped.append(
            (dataset.name, "none of its par-baked files are still on disk, "
                           "so the @context cannot be read back"))
        return report

    document = {"@context": context, "@type": "sc:Dataset", "name": dataset.name}
    answers = answers_for(session, dataset)
    document["description"] = answers.pop("description", dataset.description or "")
    document["conformsTo"] = None
    for key, value in answers.items():
        document[key] = value
    document["keywords"] = keywords_with_hardware(
        answers.get("keywords"), machine)

    # A FileSet rather than a FileObject: this stands for many files, and a
    # single checksum is not a thing that exists for many files. djc_v4 writes a
    # FileObject here with "sha256": "unknown-not-yet-published", which is a
    # placeholder in the one field whose purpose is to be verifiable.
    pattern = filename_glob_for(dataset)
    described = [f"One file per date range. Naming: {pattern}."]
    if dataset.format_notes:
        described.append(dataset.format_notes.rstrip(".") + ".")
    described.append(f"{len(files)} file(s) documented so far: "
                     + ", ".join(sorted(each.name for each in files)) + ".")
    document["distribution"] = [{
        "@type": "cr:FileSet", "@id": "data_files", "name": "data_files",
        "description": " ".join(described),
        "encodingFormat": "text/csv",
        "includes": pattern,
    }]

    record_sets = [enumeration_record_set(each)
                   for each in used_enumerations(session, dataset)]
    record_sets.append(record_set_for(
        session, dataset, None,
        lambda name: {"fileSet": {"@id": "data_files"},
                      "extract": {"column": name}}))
    document["recordSet"] = record_sets

    profile = machine_block(machine)
    if profile:
        document["_machine"] = profile
    # No _parbake_measurements here on purpose. A measurement is true of one
    # file -- 39,432 rows, 42 distinct exit codes -- and a series is many files,
    # so there is no honest single value to write. The per-file Croissants keep
    # theirs.

    reasons = reasons_unfinished(session, dataset)
    finish(document, reasons)

    path = Path(output_directory) / f"{dataset.name}{BAKED_SUFFIX}"
    write(document, path, report, reasons)
    session.commit()
    return report


# --- the shared parts ------------------------------------------------------

def used_enumerations(session, dataset):
    """The decoding tables any of this dataset's columns point at.

    Only the ones actually referenced. A machine's enumerations are shared by
    every dataset on it, and writing all of them into each file would put the
    Mira task-history decodings into the machine-status document.
    """
    linked = {doc.enumeration_id for doc in session.scalars(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id)) if doc.enumeration_id}
    if not linked:
        return []
    return list(session.scalars(select(Enumeration).where(
        Enumeration.id.in_(linked)).order_by(Enumeration.name)))


def record_set_for(session, dataset, source_file, source_reference):
    """The records RecordSet: one field per column, in the order they appear.

    For the series shape the columns are the union across its files, because a
    later export having gained a column does not unsay what the earlier ones
    hold.
    """
    recorded = {row.field_name: row for row in session.scalars(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id))}

    ordered, seen = [], set()
    files = [source_file] if source_file is not None else list(dataset.files)
    for each in files:
        for column in each.fields:
            if column.name not in seen:
                seen.add(column.name)
                ordered.append(column.name)

    answer = session.scalar(select(DatasetAnswer).where(
        DatasetAnswer.dataset_id == dataset.id, DatasetAnswer.key == "recordSet"))
    return {
        "@type": "cr:RecordSet", "@id": "records", "name": "records",
        "description": (answer.answer if answer else
                        f"One record per row. {len(ordered)} fields."),
        "field": [field_entry(name, recorded.get(name), "records", source_reference)
                  for name in ordered],
    }


def finish(document, reasons):
    """Set conformsTo: the real versions only when nothing is outstanding.

    The single most important line in this module. The unreal conformsTo is what
    stops an unfinished file passing validation, and removing it early would
    throw away the only thing preventing unreviewed work from looking reviewed.
    """
    from parbake_link import PARBAKED_CONFORMS_TO
    document["conformsTo"] = (list(FINISHED_CONFORMS_TO) if not reasons
                              else PARBAKED_CONFORMS_TO)
    if reasons:
        document["_unfinished"] = {
            "warning": "NOT FINISHED. Questions remain unanswered; this file "
                       "deliberately fails validation and must not be cited, "
                       "submitted or published.",
            "outstanding": reasons,
        }
    return not reasons


def write(document, path, report, reasons):
    """Write the JSON, and the Markdown beside it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    report.written.append(path)
    if reasons:
        report.unfinished[str(path)] = reasons

    readable = path.with_name(path.name[: -len(BAKED_SUFFIX)] + BAKED_MARKDOWN_SUFFIX)
    readable.write_text(render_markdown(document), encoding="utf-8")
    report.readable.append(readable)
    return path


def note_superseded(source_file, report):
    """Say which par-baked siblings this has just made stale.

    They were rendered from the par-baked Croissant and nothing has rewritten
    them, so anybody reading one is reading the unreviewed version.
    """
    for derived in source_file.derived:
        report.superseded.append(derived.path)


def bake_everything(session, output_directory):
    """Write every file that has a machine, and every dataset that has files."""
    report = BakeReport()
    for source_file in session.scalars(select(SourceFile).order_by(SourceFile.name)):
        if source_file.dataset_id is None:
            continue
        bake_file(session, source_file, output_directory, report)
    seen = set()
    for source_file in session.scalars(select(SourceFile)):
        dataset = source_file.dataset
        if dataset is not None and dataset.id not in seen:
            seen.add(dataset.id)
            bake_dataset(session, dataset, output_directory, report)
    return report
