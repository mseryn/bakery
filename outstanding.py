#!/usr/bin/env python3
"""What is still unanswered about a file, and what the database can offer instead.

The interface asks questions. This works out which questions are left, in what
order, and what is already known that might answer one -- so that the widgets in
bakery.py are only ever drawing what this decided, and the deciding can be tested
without a terminal.

## The order the questions come in

    the machine          everything else is scoped through it, so nothing can
                         be asked before it is settled -- naming it is also what
                         puts the file in a dataset, which is what the column
                         descriptions hang off
    the machine profile  only if nothing is recorded for it yet
    the dataset answers  licence, citation, the responsible-AI blocks: true of
                         the series, asked once for it
    the columns          in the order they appear in the file, which is the
                         order somebody reading the data would meet them

## Three states, not two

A column is not simply known or unknown:

    unanswered    nothing recorded. Ask.
    seeded        read out of a finished file, nobody has confirmed it HERE.
                  Show it filled in and ask for a keystroke.
    confirmed     somebody sat with the measurements and decided. Do not ask.

The middle one is the whole reason the corpus is worth seeding. It turns 66
questions into 66 confirmations, which is a different afternoon.

## Offering another machine's answer

When a column has never been described for this machine but has been for
another, that description is offered as a default. That is what makes the
per-machine scoping affordable: 1,034 rows in the database, 742 of them typed.

An offer is never applied on its own. Two kinds are made, and they are labelled
differently because they are not equally good evidence:

    exact        the same column name on another machine
    respelled    the same name in another case -- START_TIMESTAMP where the
                 other machine says start_timestamp. Worth showing, and not
                 worth assuming: an export that renames its columns may have
                 changed more than the capitals.
"""

from dataclasses import dataclass

from sqlalchemy import func, select

from models import (
    Dataset,
    DatasetAnswer,
    FieldDoc,
    Machine,
    SourceFile,
    STATE_COMPLETE,
    STATE_IMPORTED,
    STATE_IN_PROGRESS,
    machine_key,
    now,
)
from questions import DATASET_QUESTIONS, describe_measurements


@dataclass
class Offer:
    """An answer from elsewhere, and how near it is to the question being asked."""

    doc: FieldDoc
    where: str              # the dataset it came from
    kind: str               # seeded | sibling | other machine | respelled

    def describe(self):
        if self.kind == "seeded":
            return f"already recorded here, unconfirmed ({self.doc.recorded_by})"
        if self.kind == "sibling":
            return f"{self.where} on this machine describes the same column"
        if self.kind == "respelled":
            return (f"{self.where} describes {self.doc.field_name} "
                    "-- the same name in another case")
        return f"{self.where} describes the same column"


@dataclass
class FieldWork:
    """One column of one file, and everything needed to ask about it."""

    name: str
    position: int
    measurements: dict | None
    doc: FieldDoc | None = None
    offer: Offer | None = None

    @property
    def evidence(self):
        """What parbake saw, as something a person can read.

        Without this, "what does exit_code hold?" is a memory test. The same
        renderer the older prompting used, so the two cannot drift apart.
        """
        return describe_measurements(self.measurements)

    @property
    def description(self):
        if self.doc and self.doc.description:
            return self.doc.description
        return self.offer.doc.description if self.offer else None

    @property
    def data_type(self):
        if self.doc and self.doc.data_type:
            return self.doc.data_type
        return self.offer.doc.data_type if self.offer else None

    @property
    def unit(self):
        if self.doc and self.doc.unit:
            return self.doc.unit
        return self.offer.doc.unit if self.offer else None

    @property
    def note(self):
        """Only ever this dataset's own note.

        A note is about the work, not about the column: "check this against the
        accounting table" was written by somebody looking at THIS export. Another
        dataset's note is not evidence about this one, so it is never offered.
        """
        return self.doc.note if self.doc else None

    @property
    def is_settled(self):
        """Answered here, by somebody, on purpose."""
        return bool(self.doc and self.doc.is_complete and self.doc.is_confirmed)

    @property
    def state(self):
        if self.is_settled:
            return "confirmed"
        if self.description or self.data_type:
            return "offered"        # something to accept rather than to type
        return "unanswered"


@dataclass
class Coverage:
    """How much of a file is answered. What the list screen shows per row."""

    columns: int = 0
    confirmed: int = 0
    offered: int = 0

    @property
    def outstanding(self):
        return self.columns - self.confirmed

    @property
    def share(self):
        return self.confirmed / self.columns if self.columns else 0.0

    def describe(self):
        if not self.columns:
            return "no columns"
        if not self.outstanding:
            return f"all {self.columns} confirmed"
        if self.offered:
            return (f"{self.confirmed}/{self.columns} confirmed, "
                    f"{self.offered} to accept")
        return f"{self.confirmed}/{self.columns} confirmed"


# --- offers ----------------------------------------------------------------

def offer_for(session, dataset, field_name, existing=None):
    """The best answer from elsewhere for a column this dataset has not settled.

    Nearest first, because how near an answer came from is how much it is worth:

        recorded here already   seeded from a finished file, not yet confirmed
        a sibling dataset       the same column, same machine, another export.
                                Often right -- COBALT_JOBID means the same thing
                                in every Mira dataset -- and sometimes exactly
                                wrong, which is why it is offered and not taken.
        another machine         the same column name on a different system
        respelled               the same name in another case. Worth showing and
                                not worth assuming: an export that renames its
                                columns may have changed more than the capitals.

    A confirmed answer beats an unconfirmed one within each kind, and the most
    recently recorded breaks a tie.
    """
    if existing is not None and (existing.description or existing.data_type):
        return Offer(existing, dataset.name, "seeded")

    def best(rows):
        usable = [row for row in rows if row.description or row.data_type]
        return max(usable, key=lambda row: (row.is_confirmed, row.recorded_at),
                   default=None)

    def named(doc):
        found = session.get(Dataset, doc.dataset_id)
        return found.name if found else "another dataset"

    elsewhere = session.scalars(select(FieldDoc).where(
        FieldDoc.field_name == field_name,
        FieldDoc.dataset_id != dataset.id)).all()

    siblings = [row for row in elsewhere
                if session.get(Dataset, row.dataset_id)
                and session.get(Dataset, row.dataset_id).machine_id == dataset.machine_id]
    nearest = best(siblings)
    if nearest:
        return Offer(nearest, named(nearest), "sibling")

    further = best(elsewhere)
    if further:
        return Offer(further, named(further), "other machine")

    respelled = best(session.scalars(select(FieldDoc).where(
        func.lower(FieldDoc.field_name) == field_name.lower(),
        FieldDoc.field_name != field_name)).all())
    if respelled:
        return Offer(respelled, named(respelled), "respelled")
    return None


def field_work_for(session, source_file, only_outstanding=True):
    """Every column of this file that still needs something, in file order.

    Needs the machine to be settled first: without one there is nothing to look
    a description up against, which is why the machine is the first question.
    """
    dataset = source_file.dataset
    if dataset is None:
        return []

    recorded = {row.field_name: row for row in session.scalars(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id))}

    work = []
    for column in source_file.fields:
        existing = recorded.get(column.name)
        item = FieldWork(name=column.name, position=column.position,
                         measurements=column.measurements, doc=existing)
        if not item.is_settled:
            item.offer = offer_for(session, dataset, column.name, existing)
        if item.is_settled and only_outstanding:
            continue
        work.append(item)
    return work


def coverage_of(session, source_file):
    """How much of this file is answered, for the list screen."""
    dataset = source_file.dataset
    columns = len(source_file.fields)
    if dataset is None:
        return Coverage(columns=columns)

    recorded = {row.field_name: row for row in session.scalars(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id))}
    found = Coverage(columns=columns)
    for column in source_file.fields:
        existing = recorded.get(column.name)
        if existing and existing.is_complete and existing.is_confirmed:
            found.confirmed += 1
        elif existing and (existing.description or existing.data_type):
            found.offered += 1
    return found


# --- answering -------------------------------------------------------------

def dataset_for(session, source_file, machine_name, dataset_name=None):
    """The machine and dataset a file belongs to under this machine name.

    Creates either if it does not exist yet, and attaches nothing. The dataset is
    named <MACHINE>_<KIND> to match how the finished files are named, and an
    existing one is joined rather than a second being made.

    Returns (machine, dataset).
    """
    found = session.scalar(select(Machine).where(Machine.key == machine_key(machine_name)))
    if found is None:
        found = Machine(machine_name)
        session.add(found)
        session.flush()

    name = dataset_name or (f"{found.name.upper()}_{source_file.kind}"
                            if source_file.kind else found.name.upper())
    dataset = session.scalar(select(Dataset).where(
        Dataset.machine_id == found.id, Dataset.name == name))
    if dataset is None:
        dataset = Dataset(machine_id=found.id, name=name, kind=source_file.kind)
        session.add(dataset)
        session.flush()
    return found, dataset


def date_to_iteration(source_file, machine):
    """Record the iteration the file's dates fall in, or none if none covers them."""
    source_file.iteration = machine.iteration_covering(
        source_file.covers_from or source_file.covers_to)


def confirm_machine(session, source_file, machine_name, dataset_name=None):
    """Settle which machine a file came from, and put it in a dataset.

    Confirming the machine is what attaches the file to a dataset, because a
    dataset belongs to a machine and there is nowhere else for it to hang. To
    change a machine already confirmed, see transfer.change_machine, which also
    carries the file's answers across.

    Returns (machine, dataset).
    """
    found, dataset = dataset_for(session, source_file, machine_name, dataset_name)
    source_file.dataset = dataset
    date_to_iteration(source_file, found)
    source_file.state = STATE_IN_PROGRESS
    source_file.updated_at = now()
    session.commit()
    return found, dataset


def record_field_answer(session, dataset, field_name, description=None,
                        data_type=None, unit=None, note=None, source_file=None,
                        recorded_by=None):
    """Write down what somebody said about a column, confirmed.

    Anything answered through the interface is confirmed by definition: a person
    was looking at the measurements when they said it. Saving happens per
    answer, so stopping halfway loses nothing -- which is the normal way this
    work ends, because nobody answers 66 questions in one sitting.
    """
    existing = session.scalar(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id, FieldDoc.field_name == field_name))
    if existing is None:
        existing = FieldDoc(dataset_id=dataset.id, field_name=field_name)
        session.add(existing)

    if description is not None:
        existing.description = description or None
    if data_type is not None:
        existing.data_type = data_type or None
    if unit is not None:
        existing.unit = unit or None
    if note is not None:
        existing.note = note or None

    existing.is_confirmed = True
    existing.recorded_at = now()
    existing.recorded_by = recorded_by
    if source_file is not None and existing.first_seen_in is None:
        existing.first_seen_in = source_file.id

    if source_file is not None:
        refresh_state(session, source_file)
    session.commit()
    return existing


def refresh_state(session, source_file):
    """Move a file between imported, in progress and complete as answers land."""
    found = coverage_of(session, source_file)
    if source_file.machine is None:
        source_file.state = STATE_IMPORTED
    elif found.columns and not found.outstanding:
        source_file.state = STATE_COMPLETE
    else:
        source_file.state = STATE_IN_PROGRESS
    source_file.updated_at = now()
    return source_file.state


# --- the questions that are not about a column -----------------------------

def outstanding_dataset_questions(session, dataset):
    """The licence, the citation, the RAI blocks this dataset still needs.

    Asked once for a series rather than once per date range, and taken from the
    same catalogue the older prompting used so a seeded answer and a typed one
    are the same thing.
    """
    answered = {row.key: row for row in session.scalars(select(DatasetAnswer).where(
        DatasetAnswer.dataset_id == dataset.id))}
    outstanding = []
    for spec in DATASET_QUESTIONS:
        found = answered.get(spec["key"])
        if found is None or not found.is_confirmed:
            outstanding.append((spec, found))
    return outstanding


def record_dataset_answer(session, dataset, key, answer, recorded_by=None,
                          source_file=None):
    """Write down one dataset-level answer, confirmed."""
    existing = session.scalar(select(DatasetAnswer).where(
        DatasetAnswer.dataset_id == dataset.id, DatasetAnswer.key == key))
    if existing is None:
        existing = DatasetAnswer(dataset_id=dataset.id, key=key, answer=answer)
        session.add(existing)
    else:
        existing.answer = answer
    existing.is_confirmed = True
    existing.recorded_at = now()
    existing.recorded_by = recorded_by
    if source_file is not None and existing.first_seen_in is None:
        existing.first_seen_in = source_file.id
    session.commit()
    return existing


def machine_needs_profile(machine):
    """Is there anything recorded about what this machine is made of?"""
    return not machine.iterations


def files_for_listing(session):
    """Every imported file, with its coverage, worst first.

    Worst first because the list is a queue: what needs doing should not be
    below what is finished.
    """
    rows = []
    for source_file in session.scalars(select(SourceFile).order_by(SourceFile.name)):
        rows.append((source_file, coverage_of(session, source_file)))
    rows.sort(key=lambda row: (row[1].share, row[0].name))
    return rows
