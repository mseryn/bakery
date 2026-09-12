#!/usr/bin/env python3
"""What the bakery remembers between sittings.

A par-baked Croissant is a measured file with every judgement left out. Filling
those judgements in is the slow part, and the same judgements keep coming back:
the five job-log exports in the test corpus share all 66 of their column names,
and answering "what does exit_code hold?" once per file means answering it five
times.

So the answers live in a database rather than in the output files.

## The shape was taken from the finished files, not the par-baked ones

croissant_files/djc_v4.croissant.json and complete_documentation/ are what a
finished dataset actually looks like here, and they carry four things a
par-baked file has no trace of:

    a machine profile     vendor, architecture, CPU, GPU, interconnect,
                          scheduler, node and rack counts, deployment dates --
                          and machines change, so that profile is dated rather
                          than current
    a unit per field      every field dictionary has a UNIT column
    value decodings       exit_code_enum: what 143 and -29 actually mean
    a file series         the published distribution is "one file per date
                          range", not the one CSV parbake measured

A schema built from par-baked output alone silently drops all four.

## The tables

    machines            the systems, by name
    machine_iterations  what a machine was made of, between two dates
    machine_partitions  the per-partition hardware of one iteration
    datasets            a series of files: POLARIS_DJC, every date range of it
    source_files        one imported par-baked Croissant
    file_fields         the columns seen in one file, with parbake's measurements
    field_docs          what a person said a column means -- one per dataset
    enumerations        a decoding table, and enumeration_values its rows
    dataset_answers     the licence, the citation, the responsible-AI blocks
    derived_files       the .txt and .md siblings, tracked but never read

## What is scoped to what

A field description is recorded for (dataset, field), because that is the
smallest thing a column's meaning is actually true of. START_TIMESTAMP is when a
job began in POLARIS_DJC and when an outage began in POLARIS_MACHINESTATUS;
measured on the corpus, 38 (machine, column) pairs mean different things in
different datasets on the same machine, so one description per machine would
have to be wrong for one of them.

Scoping it this tightly would mean typing MACHINE_NAME's description once per
dataset, so what another dataset already says is offered as a default to accept
or edit -- nearest first: another dataset on the same machine, then another
machine, then the same name spelled differently. Roughly 1,080 rows across the
corpus but only ~740 of them typed; the rest are a keystroke each.

Everything else follows the level it is actually true at. Hardware is true of a
machine between two dates, which is what an iteration is. A licence, a landing
page and a limitation are true of a dataset --
djc_v4 is POLARIS_DJC, covering every ANL-ALCF-DJC-POLARIS_*.csv, not one file.
Measurements are true of one file.

## Portability

Column lengths are given because MySQL will not index a TEXT column without
one. Anything unbounded and unindexed is Text. Measurements and answers are
JSON, which all three backends store natively.

Machine names are matched on a lowercased `key` column rather than on `name`,
because SQLite compares case-sensitively and MySQL usually does not. Storing the
fold explicitly makes "Polaris" and "polaris" the same machine everywhere.
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Bumped when the tables change, so a database written by another version is
# refused with an explanation rather than misread.
SCHEMA_VERSION = 2

# Where a file is in the work. `imported` means measured and recorded but not
# yet claimed by anyone; `complete` means nothing is left to ask.
STATE_IMPORTED = "imported"
STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETE = "complete"
STATES = (STATE_IMPORTED, STATE_IN_PROGRESS, STATE_COMPLETE)

# Used as recorded_by when a description was read out of an existing finished
# file rather than typed at a prompt. Seeded rows are real answers someone gave
# once, but nobody has confirmed them *here*, so they are offered as a default
# rather than counted as settled.
SEEDED_BY_PREFIX = "seeded from "


def now():
    """The current time, in UTC, to the second.

    Times are recorded rather than defaulted in the database so that every
    backend writes the same thing. MySQL, Postgres and SQLite each have their
    own idea of what CURRENT_TIMESTAMP means and which timezone it is in.
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def machine_key(name):
    """The form a machine name is matched on: trimmed and lowercased."""
    return name.strip().lower()


class Base(DeclarativeBase):
    pass


class Meta(Base):
    """One row per housekeeping value. Holds the schema version, and little else."""

    __tablename__ = "bakery_meta"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)


# --- the machine, and what it was made of, and when ------------------------

class Machine(Base):
    """A system data comes from: polaris, aurora, mira.

    Deliberately thin. The name and the organisation outlive any particular
    hardware, and everything that can be rebuilt, upgraded or rescheduled lives
    on an iteration instead. Descriptions hang off datasets rather than off
    either: a node refresh does not change what EXIT_CODE holds, and making
    descriptions expire with the hardware would mean retyping all 66 of them the
    first time a machine gains a partition.

    Decoding tables stay here rather than on a dataset, because exit codes are a
    property of the machine's scheduler and are shared by every dataset that
    records one.

    A machine is created when someone names it at a prompt, not when a file
    merely looks like it came from one -- an import suggests a machine and never
    asserts one.
    """

    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    organization: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)

    iterations: Mapped[list["MachineIteration"]] = relationship(
        back_populates="machine", cascade="all, delete-orphan",
        order_by="MachineIteration.starts_on")
    datasets: Mapped[list["Dataset"]] = relationship(
        back_populates="machine", cascade="all, delete-orphan", passive_deletes=True)
    enumerations: Mapped[list["Enumeration"]] = relationship(
        back_populates="machine", cascade="all, delete-orphan")

    def __init__(self, name, **rest):
        super().__init__(name=name.strip(), key=machine_key(name), **rest)

    @property
    def current_iteration(self):
        """The iteration that has not ended, if there is one.

        An open end date is what "present" means in the documents. A machine
        that has been retired has no current iteration, which is not an error --
        Mira was decommissioned and its data is still worth documenting.
        """
        open_ended = [each for each in self.iterations if each.ends_on is None]
        if open_ended:
            return max(open_ended, key=lambda each: each.starts_on or date.min)
        return None

    def iteration_covering(self, day):
        """Which iteration the machine was on a given day, if it is recorded.

        Used to suggest an iteration for a file from the dates in its name. A
        suggestion only: the dates in a filename are what the export was labelled
        with, and section 8 of the Polaris documentation is a worked example of
        those disagreeing with the data inside.
        """
        if day is None:
            return None
        for each in self.iterations:
            if each.covers(day):
                return each
        return None

    def __repr__(self):
        return f"Machine({self.name!r})"


class MachineIteration(Base):
    """What a machine was made of, over a stretch of time.

    Machines change underneath the data. Theta was Cobalt-scheduled and then
    PBS-scheduled; a machine gains a partition, swaps an interconnect, or
    doubles its node count, and a dataset from 2022 is then being described by
    hardware that no longer exists. Recording the hardware against a date range
    is what keeps an old export honest.

    `ends_on` being NULL is how the documents write "present". A machine should
    have at most one of those at a time; that cannot be expressed as a portable
    constraint -- Postgres would take a partial unique index and MySQL would
    not -- so it is enforced where iterations are created, and `overlaps` is
    here for checking.

    `derived_from_id` records which iteration this one was copied from, so the
    usual way of adding one -- take what is true now, change the two things that
    changed, save it as the next -- leaves a trail rather than looking like
    someone retyped the whole profile.
    """

    __tablename__ = "machine_iterations"
    __table_args__ = (
        UniqueConstraint("machine_id", "label", name="one_iteration_per_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[int] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False)
    # Short and human: "as deployed", "after the 2024 GPU refresh".
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="as deployed")

    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)    # NULL means current
    # Where the source says "August 2022" and not a day, the day is the first of
    # the month and the prose it came from is kept here rather than lost.
    dates_as_written: Mapped[str | None] = mapped_column(String(128))

    vendor: Mapped[str | None] = mapped_column(String(255))
    architecture: Mapped[str | None] = mapped_column(String(255))
    scheduler: Mapped[str | None] = mapped_column(String(128))
    # "Homogeneous (identical) nodes", or whatever the partitions below amount
    # to. Free text, because that is how every document writes it.
    partition_summary: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    derived_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("machine_iterations.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)

    machine: Mapped["Machine"] = relationship(back_populates="iterations")
    partitions: Mapped[list["MachinePartition"]] = relationship(
        back_populates="iteration", cascade="all, delete-orphan",
        order_by="MachinePartition.position")
    files: Mapped[list["SourceFile"]] = relationship(back_populates="iteration")

    @property
    def is_current(self):
        return self.ends_on is None

    @property
    def is_profiled(self):
        """Is there enough here to write the System section of a document?"""
        return all([self.vendor, self.architecture, self.scheduler]) and bool(self.partitions)

    def covers(self, day):
        """Was the machine in this state on that day? Open ends count as open."""
        if self.starts_on and day < self.starts_on:
            return False
        if self.ends_on and day > self.ends_on:
            return False
        return True

    def overlaps(self, other):
        """Do two iterations claim the same day? Two answers to one question."""
        if self.ends_on and other.starts_on and other.starts_on > self.ends_on:
            return False
        if other.ends_on and self.starts_on and self.starts_on > other.ends_on:
            return False
        return True

    def next_iteration(self, starts_on, label, ends_previous=True):
        """A copy of this one to edit, as the machine's next state.

        The normal way a machine changes is that almost nothing about it does.
        So the next iteration starts as this one -- hardware, partitions and all
        -- and someone changes the two lines that actually differ. Copying is
        the default rather than a convenience because retyping a profile is how
        a GPU count quietly becomes wrong.

        Returns the new iteration, unsaved, with the partitions copied. The
        caller adds it to a session; nothing is written here.
        """
        if ends_previous and self.ends_on is None and starts_on is not None:
            # The old state stopped being true the day the new one started.
            self.ends_on = starts_on - timedelta(days=1)

        successor = MachineIteration(
            machine_id=self.machine_id,
            label=label,
            starts_on=starts_on,
            ends_on=None,
            vendor=self.vendor,
            architecture=self.architecture,
            scheduler=self.scheduler,
            partition_summary=self.partition_summary,
            derived_from=self,
        )
        successor.partitions = [
            MachinePartition(
                name=each.name, position=each.position, cpu=each.cpu, gpu=each.gpu,
                interconnect=each.interconnect, memory_type=each.memory_type,
                storage=each.storage, node_count=each.node_count,
                rack_count=each.rack_count, notes=each.notes)
            for each in self.partitions
        ]
        return successor

    def __repr__(self):
        span = f"{self.starts_on or '?'} to {self.ends_on or 'present'}"
        return f"MachineIteration({self.label!r}, {span})"


MachineIteration.derived_from = relationship(
    "MachineIteration", remote_side=[MachineIteration.id])


class MachinePartition(Base):
    """One kind of node, in one iteration of a machine.

    Polaris has one: every node is identical, and the document says so in a
    line. Aurora writes the same information as a nested "Partition One" block,
    and a machine with GPU and non-GPU nodes needs two. Rather than flat columns
    that are right for one machine and wrong for the next, the hardware that can
    differ between nodes lives here and a homogeneous machine has a single
    partition.

    Counts are integers; a count that is an estimate or a range belongs in
    `notes` rather than being forced into one.
    """

    __tablename__ = "machine_partitions"
    __table_args__ = (
        UniqueConstraint("iteration_id", "name", name="one_row_per_partition"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    iteration_id: Mapped[int] = mapped_column(
        ForeignKey("machine_iterations.id", ondelete="CASCADE"), nullable=False)
    # "default" for a homogeneous machine, so there is always something to name.
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="default")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cpu: Mapped[str | None] = mapped_column(Text)
    gpu: Mapped[str | None] = mapped_column(Text)
    interconnect: Mapped[str | None] = mapped_column(Text)
    memory_type: Mapped[str | None] = mapped_column(String(255))
    storage: Mapped[str | None] = mapped_column(String(255))
    node_count: Mapped[int | None] = mapped_column(Integer)
    rack_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)

    iteration: Mapped["MachineIteration"] = relationship(back_populates="partitions")

    def __repr__(self):
        return f"MachinePartition({self.name!r}, iteration={self.iteration_id})"


# --- the dataset, and the files in it --------------------------------------

class Dataset(Base):
    """A series of files published as one thing: POLARIS_DJC, AURORA_POWER-TELEMETRY.

    This is the level the finished Croissant is written at. djc_v4's whole
    distribution is one FileObject called `data_files`, described as "one file
    per date range, labeled in the filename" -- so the licence, the landing
    page, the citation and every limitation are true of the series, and would
    otherwise be retyped for each date range parbake measured separately.

    `hand_verified` is section 9 of the documentation template. It is a fact
    about the review rather than about the data, so it is a column here rather
    than an answer someone types into a text field that nothing can act on.
    """

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("machine_id", "name", name="one_dataset_per_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[int] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)     # POLARIS_DJC
    kind: Mapped[str | None] = mapped_column(String(64))               # DJC

    description: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)                      # landing page
    version: Mapped[str | None] = mapped_column(String(32))
    date_published: Mapped[str | None] = mapped_column(String(32))
    # "ANL-ALCF-DJC-POLARIS_YYYYMMDD_YYYYMMDD.csv", and how it is distributed:
    # section 6 of the template, which records things like CRLF line endings and
    # a gzipped alternative that no Croissant property has a slot for.
    filename_pattern: Mapped[str | None] = mapped_column(Text)
    format_notes: Mapped[str | None] = mapped_column(Text)

    hand_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)

    machine: Mapped["Machine"] = relationship(back_populates="datasets")
    files: Mapped[list["SourceFile"]] = relationship(
        back_populates="dataset", order_by="SourceFile.name", passive_deletes=True)
    answers: Mapped[list["DatasetAnswer"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan")
    # The descriptions go with the dataset they describe. Losing the grouping
    # loses them, which is why a dataset is joined rather than replaced.
    field_docs: Mapped[list["FieldDoc"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan")

    def __repr__(self):
        return f"Dataset({self.name!r}, {len(self.files)} file(s))"


class SourceFile(Base):
    """One imported par-baked Croissant.

    Identified by the dataset name inside the Croissant rather than by its path,
    so re-importing a directory that has been moved or copied updates the row
    instead of growing a second one. The path is still recorded, as where it was
    last seen.

    `parbaked_sha256` is the checksum of the par-baked JSON, not of the data it
    describes. It is what says whether the file has been re-measured since it
    was imported -- new columns, a longer scan -- and so whether the recorded
    fields are still what the file contains.

    There is no machine column. A file belongs to a dataset and a dataset
    belongs to a machine, and a second route to the same fact is a second chance
    for the two to disagree. Confirming the machine for a file is what attaches
    it to a dataset.
    """

    __tablename__ = "source_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Pulled out of the name, e.g. DJC or MACHINESTATUS. Used for grouping files
    # into a dataset and for ordering the list; nothing depends on it being right.
    kind: Mapped[str | None] = mapped_column(String(64))

    dataset_id: Mapped[int | None] = mapped_column(
        ForeignKey("datasets.id", ondelete="SET NULL"))
    # Which state the machine was in when this data was produced. Suggested from
    # the dates below and confirmed by a person, never assumed: section 8 of the
    # Polaris documentation is a worked example of a filename's dates disagreeing
    # with the data inside it.
    iteration_id: Mapped[int | None] = mapped_column(
        ForeignKey("machine_iterations.id", ondelete="SET NULL"))
    # What the import thought the machine was, from the filename and from the
    # MACHINE_NAME column. Kept apart from the dataset because a guess is not an
    # answer: it is offered at the prompt and confirmed by a person.
    machine_suggestion: Mapped[str | None] = mapped_column(String(128))

    parbaked_path: Mapped[str] = mapped_column(Text, nullable=False)
    parbaked_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    baked_path: Mapped[str | None] = mapped_column(Text)

    # Read out of the filename, e.g. ANL-ALCF-DJC-POLARIS_20220809_20221231.
    # A label, not a measurement -- it is what the export was named, which is
    # why it only ever suggests an iteration.
    covers_from: Mapped[date | None] = mapped_column(Date)
    covers_to: Mapped[date | None] = mapped_column(Date)

    source_csv_path: Mapped[str | None] = mapped_column(Text)
    source_size_bytes: Mapped[int | None] = mapped_column(Integer)
    rows: Mapped[int | None] = mapped_column(Integer)
    column_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATE_IMPORTED)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)

    dataset: Mapped["Dataset | None"] = relationship(back_populates="files")
    iteration: Mapped["MachineIteration | None"] = relationship(back_populates="files")
    fields: Mapped[list["FileField"]] = relationship(
        back_populates="file", cascade="all, delete-orphan",
        order_by="FileField.position")
    derived: Mapped[list["DerivedFile"]] = relationship(
        back_populates="file", cascade="all, delete-orphan")

    @property
    def machine(self):
        """The machine this file is from, once someone has said which."""
        return self.dataset.machine if self.dataset else None

    @property
    def suggested_iteration(self):
        """Which iteration the dates in the name fall in, if the machine is known."""
        machine = self.machine
        if machine is None:
            return None
        return machine.iteration_covering(self.covers_from or self.covers_to)

    def __repr__(self):
        return f"SourceFile({self.name!r}, {self.column_count} fields)"


class FileField(Base):
    """One column of one imported file, with what parbake measured about it.

    The measurements are kept here rather than re-read from the Croissant every
    time, because they are what makes the question answerable: "what does
    exit_code hold?" with 42 distinct values and a range shown beside it is a
    question; without them it is a memory test.
    """

    __tablename__ = "file_fields"
    __table_args__ = (UniqueConstraint("file_id", "name", name="one_row_per_column"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    measurements: Mapped[dict | None] = mapped_column(JSON)

    file: Mapped["SourceFile"] = relationship(back_populates="fields")

    def __repr__(self):
        return f"FileField({self.name!r} of {self.file_id})"


# --- what a person says ----------------------------------------------------

class FieldDoc(Base):
    """What a person said one column means, in one dataset.

    The thing the whole tool exists to accumulate. One row per (dataset, field):
    answering again for the same dataset replaces it rather than adding a second
    opinion, because two answers that disagree cannot both be written into a
    Croissant and nothing here can pick between them.

    Per dataset rather than per machine because a column name is not a meaning.
    Polaris records START_TIMESTAMP in its job log and in its machine-status log,
    and they are the start of different things. Confirming one would have
    overwritten the other.

    `unit` is its own column because every field dictionary has a UNIT column
    and Croissant has no property for one. Keeping it separate means it can be
    appended to the emitted description without a person having to remember to
    write "Watts" into the prose every time.

    `note` works the same way, for the thing somebody should look at later: a
    count that looks wrong, a column whose meaning is a guess, a join that needs
    checking against the accounting table. It is appended to the emitted
    description as "NOTE: ..." rather than being typed into the prose, which
    means every note in a corpus can be found again -- see notes.py -- instead
    of being buried in a paragraph somebody has to read.

    `enumeration_id` points at a decoding table where the values are codes.
    djc_v4 defines exit_code_enum and then never references it from EXIT_CODE or
    EXIT_STATUS, so the decoding is present in the file and invisible to
    anything reading it. A link recorded here is what stops that happening again.

    `is_confirmed` separates an answer typed at a prompt from one read out of an
    existing finished file. Both are real, but a seeded one is offered as a
    default to accept rather than counted as settled.
    """

    __tablename__ = "field_docs"
    __table_args__ = (
        UniqueConstraint("dataset_id", "field_name", name="one_description_per_dataset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    field_name: Mapped[str] = mapped_column(String(255), nullable=False)

    description: Mapped[str | None] = mapped_column(Text)
    data_type: Mapped[str | None] = mapped_column(String(64))
    unit: Mapped[str | None] = mapped_column(String(128))
    # Something a person should come back to. Kept apart from the description so
    # it can be listed across a whole corpus rather than read for.
    note: Mapped[str | None] = mapped_column(Text)
    enumeration_id: Mapped[int | None] = mapped_column(
        ForeignKey("enumerations.id", ondelete="SET NULL"))

    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    recorded_by: Mapped[str | None] = mapped_column(String(255))
    first_seen_in: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL"))

    dataset: Mapped["Dataset"] = relationship(back_populates="field_docs")
    enumeration: Mapped["Enumeration | None"] = relationship(back_populates="field_docs")

    @property
    def machine(self):
        """The machine this description is ultimately about."""
        return self.dataset.machine if self.dataset else None

    @property
    def is_complete(self):
        """Has this column been both described and typed?

        A Croissant field needs both. A row with one of the two is half an
        answer and the field still has a question outstanding.
        """
        return bool(self.description) and bool(self.data_type)

    @property
    def needs_confirming(self):
        """Answered elsewhere, not yet confirmed here."""
        return self.is_complete and not self.is_confirmed

    def __repr__(self):
        return f"FieldDoc({self.field_name!r}, machine={self.machine_id})"


class Enumeration(Base):
    """A decoding table: what the codes in a column actually mean.

    Recorded per machine for the same reason descriptions are -- exit codes on a
    Cobalt-scheduled Mira and a PBS-scheduled Polaris are not the same
    vocabulary -- and shared between the fields that use it, so EXIT_STATUS and
    EXIT_CODE point at one table rather than two copies that drift apart.

    Written out as a Croissant recordSet with cr:isEnumeration and the values
    inline, which is the form djc_v4 uses.
    """

    __tablename__ = "enumerations"
    __table_args__ = (
        UniqueConstraint("machine_id", "name", name="one_enumeration_per_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[int] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)   # exit_code_enum
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)

    machine: Mapped["Machine"] = relationship(back_populates="enumerations")
    values: Mapped[list["EnumerationValue"]] = relationship(
        back_populates="enumeration", cascade="all, delete-orphan",
        order_by="EnumerationValue.position")
    field_docs: Mapped[list["FieldDoc"]] = relationship(back_populates="enumeration")

    def __repr__(self):
        return f"Enumeration({self.name!r}, {len(self.values)} value(s))"


class EnumerationValue(Base):
    """One code and what it means.

    `is_provisional` because that is how the existing meanings are actually
    written: "PROVISIONAL: in the Polaris sample this did NOT indicate running
    to walltime ... Open question." A guess recorded as a guess is useful; a
    guess that reads as settled is worse than nothing, and the flag is what lets
    the two be told apart without reading for the word.
    """

    __tablename__ = "enumeration_values"
    __table_args__ = (
        UniqueConstraint("enumeration_id", "code", name="one_row_per_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enumeration_id: Mapped[int] = mapped_column(
        ForeignKey("enumerations.id", ondelete="CASCADE"), nullable=False)
    # Text, not an integer: the codes are what appears in the column, and a
    # column of codes is not always numeric.
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    meaning: Mapped[str] = mapped_column(Text, nullable=False)
    is_provisional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    enumeration: Mapped["Enumeration"] = relationship(back_populates="values")

    def __repr__(self):
        return f"EnumerationValue({self.code!r})"


class DatasetAnswer(Base):
    """One dataset-level answer: the licence, the citation, the RAI blocks.

    `key` is the question key the bakery already uses -- "citeAs",
    "rai:dataLimitations" -- so the catalogue in questions.py stays the one
    place those are named.

    The answer is JSON because these are strings, lists of strings, booleans and
    objects, and JSON round-trips all four without a second column saying which.
    The licence is the reason objects matter: djc_v4 writes it as an sc:
    CreativeWork with a name, a text and a url, where the bakery currently
    builds only a text.
    """

    __tablename__ = "dataset_answers"
    __table_args__ = (
        UniqueConstraint("dataset_id", "key", name="one_answer_per_dataset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    answer: Mapped[object] = mapped_column(JSON, nullable=False)

    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=now, nullable=False)
    recorded_by: Mapped[str | None] = mapped_column(String(255))
    first_seen_in: Mapped[int | None] = mapped_column(
        ForeignKey("source_files.id", ondelete="SET NULL"))

    dataset: Mapped["Dataset"] = relationship(back_populates="answers")

    def __repr__(self):
        return f"DatasetAnswer({self.key!r}, dataset={self.dataset_id})"


class DerivedFile(Base):
    """A .txt or .md sibling of an imported Croissant.

    parbake writes these from the Croissant for people to read. They hold
    nothing the Croissant does not, so the bakery never reads one -- but it
    records that they exist, because they are stale the moment the Croissant is
    finished and something has to know which files those are.
    """

    __tablename__ = "derived_files"
    __table_args__ = (UniqueConstraint("file_id", "path", name="one_row_per_artifact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("source_files.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)   # txt | md

    file: Mapped["SourceFile"] = relationship(back_populates="derived")

    def __repr__(self):
        return f"DerivedFile({self.format}, {self.path!r})"
