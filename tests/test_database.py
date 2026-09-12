"""Tests for the database layer: models.py and database.py.

What is worth testing here is not that SQLAlchemy works. It is the handful of
decisions that would go wrong quietly:

    a machine named twice in different case is one machine, on every backend
    deleting a file takes its columns with it, including on SQLite
    a second description for the same (machine, field) replaces, never doubles
    an older database is brought forward, with the work in it intact
    a database from a LATER version is refused, not misread
    a new machine iteration copies the old one rather than starting blank

Run with:
    cd bakery && pytest tests/test_database.py -v
"""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database import (
    URL_VARIABLE,
    Database,
    DatabaseVersionMismatch,
    describe,
    resolve_url,
)
from models import (
    SCHEMA_VERSION,
    STATE_IMPORTED,
    Dataset,
    DatasetAnswer,
    DerivedFile,
    Enumeration,
    EnumerationValue,
    FieldDoc,
    FileField,
    Machine,
    MachineIteration,
    MachinePartition,
    Meta,
    SourceFile,
    machine_key,
)


@pytest.fixture
def database(tmp_path):
    with Database(directory=tmp_path) as opened:
        yield opened


def a_machine_with_hardware(session, name="polaris"):
    """A machine with one recorded iteration, the way an import leaves it."""
    machine = Machine(name)
    session.add(machine)
    session.commit()
    iteration = MachineIteration(
        machine_id=machine.id, label="as deployed", starts_on=date(2022, 8, 1),
        vendor="Hewlett Packard Enterprise", architecture="HPE Apollo Gen10+",
        scheduler="PBS Professional", partition_summary="Homogeneous (identical) nodes")
    iteration.partitions = [MachinePartition(
        name="default", cpu="1x AMD EPYC Milan 7543P (32 cores) per node",
        gpu="4x NVIDIA A100 per node", interconnect="HPE Slingshot (Dragonfly)",
        node_count=595)]
    session.add(iteration)
    session.commit()
    return machine, iteration


def a_dataset(session, machine, name="POLARIS_DJC"):
    dataset = Dataset(machine_id=machine.id, name=name, kind="DJC")
    session.add(dataset)
    session.commit()
    return dataset


def a_file(name="ANL-ALCF-DJC-POLARIS_20220809_20221231", **rest):
    """A source file row with the required columns filled in."""
    return SourceFile(
        name=name,
        kind=rest.pop("kind", "DJC"),
        parbaked_path=rest.pop("parbaked_path", f"/out/{name}.parbaked.json"),
        parbaked_sha256=rest.pop("parbaked_sha256", "0" * 64),
        column_count=rest.pop("column_count", 66),
        state=rest.pop("state", STATE_IMPORTED),
        **rest,
    )


# --- opening ---------------------------------------------------------------

def test_a_new_database_has_the_tables_and_the_version(database):
    with database.session() as session:
        assert session.get(Meta, "schema_version").value == str(SCHEMA_VERSION)
    assert database.created is True
    assert database.is_empty() is True


def test_opening_an_existing_database_does_not_recreate_it(tmp_path):
    first = Database(directory=tmp_path)
    with first.session() as session:
        session.add(Machine("polaris"))
        session.commit()
    first.close()

    second = Database(directory=tmp_path)
    assert second.created is False
    with second.session() as session:
        assert session.scalars(select(Machine)).one().name == "polaris"
    second.close()


def test_the_url_comes_from_the_argument_then_the_environment_then_a_local_file(
        tmp_path, monkeypatch):
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    assert resolve_url(directory=tmp_path).endswith("bakery.sqlite")

    monkeypatch.setenv(URL_VARIABLE, "postgresql+psycopg://host/bakery")
    assert resolve_url(directory=tmp_path) == "postgresql+psycopg://host/bakery"

    # An explicit url beats the environment, so one command can be pointed
    # somewhere else without unsetting anything.
    assert resolve_url("sqlite:///other.sqlite", tmp_path) == "sqlite:///other.sqlite"


def test_describing_a_database_does_not_show_the_password():
    shown = describe("postgresql+psycopg://bob:hunter2@host/bakery")
    assert "hunter2" not in shown
    assert "bob" in shown and "host" in shown


# --- machines --------------------------------------------------------------

def test_a_machine_name_is_matched_folded_but_stored_as_given(database):
    with database.session() as session:
        session.add(Machine("  Polaris  "))
        session.commit()
        found = session.scalars(select(Machine)).one()

    assert found.name == "Polaris"       # shown back the way it was typed
    assert found.key == "polaris"        # matched on this
    assert machine_key("POLARIS") == found.key


def test_the_same_machine_in_another_case_is_not_a_second_machine(database):
    with database.session() as session:
        session.add(Machine("polaris"))
        session.commit()
    with database.session() as session:
        session.add(Machine("Polaris"))
        with pytest.raises(IntegrityError):
            session.commit()


# --- what a person says ----------------------------------------------------

def test_a_description_is_recorded_per_dataset_and_replaces_itself(database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add_all([machine, a_file()])
        session.commit()
        dataset = a_dataset(session, machine)

        session.add(FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE",
                             description="first try", data_type="sc:Integer"))
        session.commit()

        recorded = session.scalars(
            select(FieldDoc).where(FieldDoc.field_name == "EXIT_CODE")).one()
        recorded.description = "the scheduler's exit status"
        session.commit()

        assert session.scalars(select(FieldDoc)).all() == [recorded]
        assert recorded.description == "the scheduler's exit status"


def test_two_descriptions_for_one_dataset_and_field_are_refused(database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        dataset = a_dataset(session, machine)
        session.add_all([
            FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE", description="a"),
            FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE", description="b"),
        ])
        with pytest.raises(IntegrityError):
            session.commit()


def test_one_column_name_can_mean_two_things_on_the_same_machine(database):
    """The whole reason descriptions are scoped to a dataset.

    Polaris records START_TIMESTAMP in its job log and in its machine-status
    log. One is when a job began; the other is when an outage began. Measured on
    the corpus, 38 (machine, column) pairs are like this.
    """
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        jobs = a_dataset(session, machine, name="POLARIS_DJC")
        status = a_dataset(session, machine, name="POLARIS_MACHINESTATUS")

        session.add_all([
            FieldDoc(dataset_id=jobs.id, field_name="START_TIMESTAMP",
                     description="When the job began execution."),
            FieldDoc(dataset_id=status.id, field_name="START_TIMESTAMP",
                     description="When the outage began."),
        ])
        session.commit()

        recorded = session.scalars(select(FieldDoc)).all()
        assert len(recorded) == 2
        assert {each.machine.name for each in recorded} == {"polaris"}


def test_a_field_is_only_complete_with_both_a_description_and_a_type(database):
    described = FieldDoc(dataset_id=1, field_name="X", description="something")
    typed = FieldDoc(dataset_id=1, field_name="X", data_type="sc:Text")
    both = FieldDoc(dataset_id=1, field_name="X", description="s", data_type="sc:Text")

    assert described.is_complete is False
    assert typed.is_complete is False
    assert both.is_complete is True


def test_a_dataset_answer_keeps_the_shape_it_was_given(database):
    """Strings, lists, booleans and objects share one column and come back as themselves.

    The object matters: djc_v4 writes its licence as an sc:CreativeWork with a
    name, a text and a url, and the bakery currently builds only a text.
    """
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        dataset = a_dataset(session, machine)
        session.add_all([
            DatasetAnswer(dataset_id=dataset.id, key="citeAs", answer="Cite ALCF."),
            DatasetAnswer(dataset_id=dataset.id, key="rai:dataLimitations",
                          answer=["COBALT_JOBID is always 0", "exit codes are signed"]),
            DatasetAnswer(dataset_id=dataset.id, key="rai:hasSyntheticData", answer=False),
            DatasetAnswer(dataset_id=dataset.id, key="license", answer={
                "@type": "sc:CreativeWork",
                "name": "ALCF data attribution requirement",
                "text": "This data was generated from resources of the ALCF...",
                "url": "https://reports.alcf.anl.gov/data/polaris.html"}),
        ])
        session.commit()

    with database.session() as session:
        answers = {row.key: row.answer for row in session.scalars(select(DatasetAnswer))}

    assert answers["citeAs"] == "Cite ALCF."
    assert answers["rai:dataLimitations"][0] == "COBALT_JOBID is always 0"
    assert answers["rai:hasSyntheticData"] is False
    assert answers["license"]["url"].endswith("polaris.html")
    assert answers["license"]["name"] == "ALCF data attribution requirement"


def test_one_answer_per_dataset_and_question(database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        dataset = a_dataset(session, machine)
        session.add_all([
            DatasetAnswer(dataset_id=dataset.id, key="license", answer="a"),
            DatasetAnswer(dataset_id=dataset.id, key="license", answer="b"),
        ])
        with pytest.raises(IntegrityError):
            session.commit()


# --- files and columns -----------------------------------------------------

def test_a_file_is_identified_by_its_dataset_name_not_its_path(database):
    """Re-importing a directory that has moved must not make a second row."""
    with database.session() as session:
        session.add(a_file(parbaked_path="/first/place.parbaked.json"))
        session.commit()
    with database.session() as session:
        session.add(a_file(parbaked_path="/somewhere/else.parbaked.json"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_imported_file_starts_with_no_machine_only_a_suggestion(database):
    """An import suggests a machine. It never asserts one."""
    with database.session() as session:
        session.add(a_file(machine_suggestion="polaris"))
        session.commit()
        stored = session.scalars(select(SourceFile)).one()

        assert stored.dataset_id is None       # nobody has confirmed a machine yet
        assert stored.machine is None
        assert stored.machine_suggestion == "polaris"
        assert stored.state == STATE_IMPORTED


def test_columns_come_back_in_the_order_they_appear_in_the_file(database):
    with database.session() as session:
        stored = a_file()
        session.add(stored)
        session.commit()
        session.add_all([
            FileField(file_id=stored.id, name="COBALT_JOBID", position=1),
            FileField(file_id=stored.id, name="JOB_NAME", position=0),
            FileField(file_id=stored.id, name="EXIT_CODE", position=2),
        ])
        session.commit()

    with database.session() as session:
        stored = session.scalars(select(SourceFile)).one()
        assert [column.name for column in stored.fields] == [
            "JOB_NAME", "COBALT_JOBID", "EXIT_CODE"]


def test_measurements_survive_the_round_trip(database):
    measured = {"rows_seen": 39432, "distinct_count": 2, "holds_one_value": True,
                "most_common_values": [{"value": "0", "count": 39431}]}
    with database.session() as session:
        stored = a_file()
        session.add(stored)
        session.commit()
        session.add(FileField(file_id=stored.id, name="COBALT_JOBID",
                              position=1, measurements=measured))
        session.commit()

    with database.session() as session:
        assert session.scalars(select(FileField)).one().measurements == measured


def test_one_row_per_column_per_file(database):
    with database.session() as session:
        stored = a_file()
        session.add(stored)
        session.commit()
        session.add_all([
            FileField(file_id=stored.id, name="JOB_NAME", position=0),
            FileField(file_id=stored.id, name="JOB_NAME", position=9),
        ])
        with pytest.raises(IntegrityError):
            session.commit()


def test_deleting_a_file_takes_its_columns_and_derived_files_with_it(database):
    """The SQLite foreign-key pragma is what makes this true here as well as on Postgres."""
    with database.session() as session:
        stored = a_file()
        session.add(stored)
        session.commit()
        session.add_all([
            FileField(file_id=stored.id, name="JOB_NAME", position=0),
            DerivedFile(file_id=stored.id, path="/out/x.txt", format="txt"),
            DerivedFile(file_id=stored.id, path="/out/x.md", format="md"),
        ])
        session.commit()

        session.delete(stored)
        session.commit()

        assert session.scalars(select(FileField)).all() == []
        assert session.scalars(select(DerivedFile)).all() == []


def test_deleting_a_machine_takes_what_was_said_about_it(database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        dataset = a_dataset(session, machine)
        session.add_all([
            FieldDoc(dataset_id=dataset.id, field_name="JOB_NAME", description="a"),
            DatasetAnswer(dataset_id=dataset.id, key="license", answer="b"),
        ])
        session.commit()

        session.delete(machine)
        session.commit()

        assert session.scalars(select(FieldDoc)).all() == []
        assert session.scalars(select(DatasetAnswer)).all() == []


def test_a_file_reaches_its_machine_through_its_dataset(database):
    """There is one route to the machine, so there is nothing to disagree with itself."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        stored = a_file()
        session.add(stored)
        session.commit()

        assert stored.machine is None       # imported, nobody has said which yet
        stored.dataset_id = dataset.id
        session.commit()
        assert stored.machine.name == "polaris"


# --- machines change under the data ----------------------------------------

def test_hardware_belongs_to_an_iteration_not_to_the_machine(database):
    """A node refresh changes the hardware. It does not change what the machine is."""
    with database.session() as session:
        machine, iteration = a_machine_with_hardware(session)

        assert machine.current_iteration is iteration
        assert iteration.is_current is True          # no end date means present
        assert iteration.is_profiled is True
        assert iteration.partitions[0].node_count == 595


def test_a_machine_with_no_iterations_is_not_an_error(database):
    """A machine is named before anyone types its hardware in."""
    with database.session() as session:
        session.add(Machine("polaris"))
        session.commit()
        assert session.scalars(select(Machine)).one().current_iteration is None


def test_the_next_iteration_starts_as_a_copy_of_the_current_one(database):
    """Retyping a profile is how a GPU count quietly becomes wrong."""
    with database.session() as session:
        machine, first = a_machine_with_hardware(session)

        second = first.next_iteration(starts_on=date(2024, 6, 1),
                                      label="after the 2024 refresh")
        # Everything carried over, including the partition, without being asked.
        assert second.vendor == first.vendor
        assert second.scheduler == "PBS Professional"
        assert [p.node_count for p in second.partitions] == [595]
        assert second.derived_from is first

        # Now change only what actually changed.
        second.partitions[0].gpu = "4x NVIDIA H100 per node"
        session.add(second)
        session.commit()

    with database.session() as session:
        machine = session.scalars(select(Machine)).one()
        assert [each.label for each in machine.iterations] == [
            "as deployed", "after the 2024 refresh"]
        assert machine.iterations[0].partitions[0].gpu == "4x NVIDIA A100 per node"
        assert machine.current_iteration.partitions[0].gpu == "4x NVIDIA H100 per node"


def test_opening_the_next_iteration_closes_the_one_before_it(database):
    """Two current iterations would be two answers to what the machine is now."""
    with database.session() as session:
        machine, first = a_machine_with_hardware(session)
        second = first.next_iteration(date(2024, 6, 1), "after the 2024 refresh")
        session.add(second)
        session.commit()

        assert first.ends_on == date(2024, 5, 31)   # the day before the new one
        assert first.is_current is False
        assert first.overlaps(second) is False
        assert machine.current_iteration is second


def test_an_iteration_covers_the_days_between_its_dates(database):
    with database.session() as session:
        machine, first = a_machine_with_hardware(session)
        second = first.next_iteration(date(2024, 6, 1), "after the 2024 refresh")
        session.add(second)
        session.commit()

        assert machine.iteration_covering(date(2022, 12, 31)) is first
        assert machine.iteration_covering(date(2024, 6, 2)) is second
        assert machine.iteration_covering(date(2030, 1, 1)) is second   # open end
        assert machine.iteration_covering(date(2019, 1, 1)) is None     # before it existed
        assert machine.iteration_covering(None) is None


def test_a_file_suggests_the_iteration_its_dates_fall_in(database):
    """Suggests. The Polaris documentation records a file whose label and contents disagree."""
    with database.session() as session:
        machine, first = a_machine_with_hardware(session)
        second = first.next_iteration(date(2024, 6, 1), "after the 2024 refresh")
        session.add(second)
        dataset = a_dataset(session, machine)

        stored = a_file(covers_from=date(2022, 8, 9), covers_to=date(2022, 12, 31))
        stored.dataset = dataset
        session.add(stored)
        session.commit()

        assert stored.suggested_iteration is first
        assert stored.iteration_id is None      # a suggestion is not an answer


def test_a_retired_machine_has_no_current_iteration(database):
    """Mira was decommissioned. Its data is still worth documenting."""
    with database.session() as session:
        machine = Machine("mira")
        session.add(machine)
        session.commit()
        session.add(MachineIteration(
            machine_id=machine.id, label="as deployed",
            starts_on=date(2013, 4, 9), ends_on=date(2019, 12, 31)))
        session.commit()

        assert machine.current_iteration is None
        assert machine.iteration_covering(date(2015, 1, 1)) is not None


def test_deleting_a_machine_takes_its_iterations_and_partitions(database):
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        session.delete(machine)
        session.commit()

        assert session.scalars(select(MachineIteration)).all() == []
        assert session.scalars(select(MachinePartition)).all() == []


# --- the dataset is the level a Croissant is published at ------------------

def test_many_files_belong_to_one_dataset(database):
    """djc_v4 is POLARIS_DJC, covering every date range, not one CSV."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        dataset.filename_pattern = "ANL-ALCF-DJC-POLARIS_YYYYMMDD_YYYYMMDD.csv"

        for name in ("ANL-ALCF-DJC-POLARIS_20220809_20221231",
                     "ANL-ALCF-DJC-POLARIS_20230101_20231231"):
            stored = a_file(name=name)
            stored.dataset = dataset
            session.add(stored)
        session.commit()

        assert len(dataset.files) == 2
        # The licence is answered once, for the series.
        session.add(DatasetAnswer(dataset_id=dataset.id, key="citeAs", answer="Cite ALCF."))
        session.commit()
        assert len(session.scalars(select(DatasetAnswer)).all()) == 1


def test_losing_a_dataset_does_not_lose_the_measurements(database):
    """Which files belong together is a judgement. What parbake saw is not."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        stored = a_file()
        stored.dataset = dataset
        session.add(stored)
        session.commit()
        session.add(FileField(file_id=stored.id, name="JOB_NAME", position=0))
        session.commit()

        session.delete(dataset)
        session.commit()

        assert len(session.scalars(select(SourceFile)).all()) == 1
        assert len(session.scalars(select(FileField)).all()) == 1
        assert session.scalars(select(SourceFile)).one().dataset_id is None


# --- units, and codes that do not mean what they look like ------------------

def test_a_unit_is_recorded_beside_the_description_not_inside_it(database):
    """Every field dictionary has a UNIT column. Croissant has no property for one."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        session.add(FieldDoc(dataset_id=dataset.id, field_name="value",
                             description="Power draw of the reporting device.",
                             data_type="sc:Float", unit="Watts"))
        session.commit()

        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.unit == "Watts"
        assert "Watts" not in recorded.description      # not buried in the prose


def test_two_fields_can_share_one_decoding_table(database):
    """EXIT_STATUS and EXIT_CODE decode the same way. Two copies would drift."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        enumeration = Enumeration(
            machine_id=machine.id, name="exit_code_enum",
            description="Exit-code decodings for the values observed in this file.")
        enumeration.values = [
            EnumerationValue(code="0", position=0,
                             meaning="Success. CAVEAT: interactive jobs always record 0."),
            EnumerationValue(code="143", position=1, is_provisional=True,
                             meaning="SIGTERM (128+15). PROVISIONAL: did not indicate "
                                     "running to walltime in the Polaris sample."),
        ]
        session.add(enumeration)
        session.commit()

        dataset = a_dataset(session, machine)
        session.add_all([
            FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE", data_type="sc:Integer",
                     description="Exit code as reported.", enumeration_id=enumeration.id),
            FieldDoc(dataset_id=dataset.id, field_name="EXIT_STATUS", data_type="sc:Integer",
                     description="Exit status as reported.", enumeration_id=enumeration.id),
        ])
        session.commit()

        assert len(enumeration.field_docs) == 2
        assert [each.field_name for each in enumeration.field_docs] == [
            "EXIT_CODE", "EXIT_STATUS"]


def test_a_provisional_meaning_is_flagged_rather_than_read_for(database):
    """A guess recorded as a guess is useful. A guess that reads as settled is not."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        enumeration = Enumeration(machine_id=machine.id, name="exit_code_enum")
        enumeration.values = [
            EnumerationValue(code="0", meaning="Success.", position=0),
            EnumerationValue(code="143", meaning="SIGTERM. Open question.",
                             is_provisional=True, position=1),
        ]
        session.add(enumeration)
        session.commit()

        provisional = [each.code for each in enumeration.values if each.is_provisional]
        assert provisional == ["143"]


def test_one_meaning_per_code(database):
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        enumeration = Enumeration(machine_id=machine.id, name="exit_code_enum")
        session.add(enumeration)
        session.commit()
        session.add_all([
            EnumerationValue(enumeration_id=enumeration.id, code="0", meaning="a"),
            EnumerationValue(enumeration_id=enumeration.id, code="0", meaning="b"),
        ])
        with pytest.raises(IntegrityError):
            session.commit()


def test_removing_a_decoding_table_leaves_the_descriptions_alone(database):
    """The link goes. What the column means was answered separately."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        enumeration = Enumeration(machine_id=machine.id, name="exit_code_enum")
        session.add(enumeration)
        session.commit()
        dataset = a_dataset(session, machine)
        session.add(FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE",
                             description="Exit code as reported.", data_type="sc:Integer",
                             enumeration_id=enumeration.id))
        session.commit()

        session.delete(enumeration)
        session.commit()
        session.expire_all()

        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.enumeration_id is None
        assert recorded.description == "Exit code as reported."


# --- an answer given here, and an answer read from somewhere else -----------

def test_a_seeded_description_is_not_counted_as_settled(database):
    """Read out of a finished file, so real -- but nobody has confirmed it here."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        session.add(FieldDoc(
            dataset_id=dataset.id, field_name="JOB_NAME",
            description="PBS job identifier", data_type="sc:Text",
            is_confirmed=False, recorded_by="seeded from djc_v4.croissant.json"))
        session.commit()

        seeded = session.scalars(select(FieldDoc)).one()
        assert seeded.is_complete is True          # it has both halves
        assert seeded.needs_confirming is True     # and still has to be looked at
        assert seeded.recorded_by.startswith("seeded from ")


def test_an_answer_typed_at_a_prompt_is_confirmed_by_default(database):
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        session.add(FieldDoc(dataset_id=dataset.id, field_name="JOB_NAME",
                             description="PBS job identifier", data_type="sc:Text"))
        session.commit()

        assert session.scalars(select(FieldDoc)).one().needs_confirming is False


# --- growing the tables under somebody's work ------------------------------

def test_an_older_database_is_brought_forward_rather_than_refused(tmp_path):
    """The whole point of migrating: somebody has hours of answers in that file.

    create_all adds missing TABLES but never missing COLUMNS, so opening an
    older database without doing anything would fail on the first query against
    a column that is not there.
    """
    import sqlite3

    from models import Dataset, FieldDoc

    opened = Database(directory=tmp_path)
    with opened.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        dataset = Dataset(machine_id=machine.id, name="POLARIS_DJC", kind="DJC")
        session.add(dataset)
        session.commit()
        session.add(FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE",
                             description="The exit code.", data_type="sc:Integer"))
        session.commit()
    opened.close()

    # Make the file genuinely a version-1 one: take the column away and say so.
    connection = sqlite3.connect(tmp_path / "bakery.sqlite")
    connection.execute("ALTER TABLE field_docs DROP COLUMN note")
    connection.execute("UPDATE bakery_meta SET value='1' WHERE name='schema_version'")
    connection.commit()
    connection.close()

    upgraded = Database(directory=tmp_path)
    assert upgraded.migrated_from == 1

    with upgraded.session() as session:
        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.description == "The exit code."      # the work survived
        assert recorded.note is None                         # and the column is there
        recorded.note = "Check against the accounting table."
        session.commit()
    upgraded.close()

    # And opening it again does nothing, rather than running the step twice.
    again = Database(directory=tmp_path)
    assert again.migrated_from is None
    with again.session() as session:
        assert session.get(Meta, "schema_version").value == str(SCHEMA_VERSION)
    again.close()


def test_a_database_from_a_later_version_is_still_refused(tmp_path):
    """Migrating is forwards only: this cannot know what a future version did."""
    opened = Database(directory=tmp_path)
    with opened.session() as session:
        session.get(Meta, "schema_version").value = str(SCHEMA_VERSION + 1)
        session.commit()
    opened.close()

    with pytest.raises(DatabaseVersionMismatch) as refused:
        Database(directory=tmp_path)
    assert "later version" in str(refused.value)


def test_a_note_is_recorded_beside_the_description_not_inside_it(database):
    """Same reasoning as the unit: so it can be listed across a corpus."""
    with database.session() as session:
        machine, _ = a_machine_with_hardware(session)
        dataset = a_dataset(session, machine)
        session.add(FieldDoc(
            dataset_id=dataset.id, field_name="EXIT_STATUS",
            description="The exit status as PBS recorded it.", data_type="sc:Integer",
            note="Disagrees with EXIT_CODE on 122 of 39,432 rows."))
        session.commit()

        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.note.startswith("Disagrees with")
        assert "Disagrees" not in recorded.description
        assert recorded.is_complete is True      # a note does not make it unfinished
