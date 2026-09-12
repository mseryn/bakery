"""Tests for importing.py.

The importer's job is to record what is measurable and refuse to decide what is
not. So what is worth testing is the line between those:

    the Croissant is read; the .txt and .md are recorded and never opened
    a machine is suggested, with its evidence, and never assigned
    the column disagreeing with the filename is reported, not smoothed over
    importing twice updates one row rather than making two
    a re-measured file gains and loses columns without losing any descriptions

Run with:
    cd bakery && pytest tests/test_importing.py -v
"""

import json
from datetime import date

import pytest
from sqlalchemy import select

from database import Database
from importing import (
    import_target,
    machine_from_column,
    parse_name,
    shared_prefix_of,
    split_off_dates,
    suggest_machine,
)
from models import DerivedFile, FieldDoc, FileField, Machine, SourceFile

REAL_CORPUS = "../bakery_test_out"


@pytest.fixture
def database(tmp_path):
    with Database(directory=tmp_path) as opened:
        yield opened


def a_parbaked_document(name="ANL-ALCF-DJC-POLARIS_20220809_20221231",
                        columns=("JOB_NAME", "MACHINE_NAME", "EXIT_CODE"),
                        machine_column_value="polaris", rows=39432):
    """The shape parbake writes: a par-baked conformsTo, fields, measurements."""
    measurements = {column: {"position": index, "rows_seen": rows}
                    for index, column in enumerate(columns)}
    if machine_column_value and "MACHINE_NAME" in columns:
        measurements["MACHINE_NAME"] = {
            "position": columns.index("MACHINE_NAME"), "rows_seen": rows,
            "distinct_count": 1,
            "most_common_values": [{"value": machine_column_value, "count": rows}]}
    return {
        "@type": "sc:Dataset",
        "conformsTo": "http://mlcommons.org/croissant/PARBAKED-DO-NOT-SUBMIT",
        "name": name,
        "distribution": [{"@type": "cr:FileObject", "name": f"{name}.csv"}],
        "recordSet": [{"@type": "cr:RecordSet", "name": "records",
                       "field": [{"name": column} for column in columns]}],
        "_parbake": {"source_file": f"/data/{name}.csv",
                     "source_size_in_bytes": 27825794, "rows_read": rows},
        "_parbake_measurements": measurements,
    }


def write_output_tree(root, documents, with_siblings=True):
    """A parbake output directory: croissants in one folder, siblings in others."""
    croissants = root / "parbaked_croissants"
    croissants.mkdir(parents=True, exist_ok=True)
    for document in documents:
        name = document["name"]
        (croissants / f"{name}.parbaked.json").write_text(json.dumps(document))
        if with_siblings:
            for folder, suffix in (("parbaked_txt", ".txt"),
                                   ("parbaked_markdown", ".parbaked.md")):
                (root / folder).mkdir(parents=True, exist_ok=True)
                (root / folder / f"{name}{suffix}").write_text(
                    "generated from the Croissant; nothing here is a source of truth")
    return root


# --- what a filename says --------------------------------------------------

def test_the_two_naming_conventions_in_the_corpus_both_parse():
    hyphenated = parse_name("ANL-ALCF-DJC-POLARIS_20220809_20221231")
    assert hyphenated.machine == "POLARIS"
    assert hyphenated.kind == "DJC"
    assert hyphenated.covers_from == date(2022, 8, 9)
    assert hyphenated.covers_to == date(2022, 12, 31)

    underscored = parse_name("aurora_crayex_telemetry_power_2024-05-02",
                             known_machines=["aurora"])
    assert underscored.machine == "aurora"
    assert underscored.kind == "crayex_telemetry_power"
    assert underscored.covers_from == underscored.covers_to == date(2024, 5, 2)


def test_a_kind_of_more_than_one_segment_survives():
    """GPU-NODE is the kind. POLARIS is the machine. Splitting on the last hyphen is enough."""
    parts = parse_name("ANL-ALCF-GPU-NODE-POLARIS_20230920_20231231")
    assert parts.machine == "POLARIS"
    assert parts.kind == "GPU-NODE"


def test_a_machine_already_in_the_database_is_recognised_wherever_it_sits():
    """Which is what makes the underscore convention work, and improves as the database fills."""
    without = parse_name("anonymized_aurora_dim_job_comp_2026-01")
    assert without.machine is None

    with_it = parse_name("anonymized_aurora_dim_job_comp_2026-01",
                         known_machines=["Aurora"])
    assert with_it.machine == "aurora"
    assert with_it.covers_from == date(2026, 1, 1)
    assert with_it.covers_to == date(2026, 1, 31)      # to the end of the month


def test_the_shared_prefix_of_a_batch_is_learned_not_hard_coded():
    """ANL-ALCF is an organisation and a facility. Nothing here should know that in advance."""
    stems = ["ANL-ALCF-DJC-POLARIS_20220809_20221231",
             "ANL-ALCF-MACHINESTATUS-THETA_20170701_20171231",
             "ANL-ALCF-GPU-NODE-POLARIS_20230920_20231231"]
    assert shared_prefix_of(stems) == ["ANL", "ALCF"]

    # One name has nothing to have in common with.
    assert shared_prefix_of(stems[:1]) == []
    # A batch from two sites shares nothing, and nothing is dropped.
    assert shared_prefix_of(["ANL-ALCF-DJC-POLARIS", "OLCF-SUMMIT-DJC-SUMMIT"]) == []


def test_a_name_with_no_dates_keeps_its_dates_unknown():
    body, starts, ends = split_off_dates("ANL-ALCF-DJC-POLARIS")
    assert (body, starts, ends) == ("ANL-ALCF-DJC-POLARIS", None, None)

    # Eight digits that are not a date are not a date.
    body, starts, ends = split_off_dates("SOMETHING_20221345_20229999")
    assert starts is None and ends is None


# --- what the data says ----------------------------------------------------

def test_a_machine_name_column_of_one_value_is_the_best_evidence_there_is():
    document = a_parbaked_document(machine_column_value="polaris")
    assert machine_from_column(document) == "polaris"


def test_a_machine_name_column_with_several_values_names_nothing():
    """A file covering two machines should not have one of them picked for it."""
    document = a_parbaked_document()
    document["_parbake_measurements"]["MACHINE_NAME"] = {
        "distinct_count": 2,
        "most_common_values": [{"value": "theta", "count": 900},
                               {"value": "thetagpu", "count": 100}]}
    assert machine_from_column(document) is None


def test_the_suggestion_carries_the_evidence_for_itself():
    document = a_parbaked_document()
    parts = parse_name("ANL-ALCF-DJC-POLARIS_20220809_20221231")
    suggestion = suggest_machine(document, parts)

    assert suggestion.machine == "polaris"
    assert suggestion.disagrees is False
    assert any("MACHINE_NAME" in line for line in suggestion.evidence)
    assert any("filename" in line for line in suggestion.evidence)


def test_a_filename_disagreeing_with_the_data_is_reported_not_smoothed_over():
    """A file called POLARIS whose every row says thetagpu is the thing to look at."""
    document = a_parbaked_document(machine_column_value="thetagpu")
    parts = parse_name("ANL-ALCF-DJC-POLARIS_20220809_20221231")
    suggestion = suggest_machine(document, parts)

    assert suggestion.machine == "thetagpu"        # the data wins over the label
    assert suggestion.disagrees is True
    assert "DISAGREE" in suggestion.describe()


def test_a_file_with_no_machine_name_column_falls_back_to_the_filename():
    document = a_parbaked_document(columns=("COBALT_JOBID", "ERROR_CODE"))
    parts = parse_name("ANL-ALCF-HARDWAREERROR-THETA_20170620_20170630")
    suggestion = suggest_machine(document, parts)

    assert suggestion.machine == "THETA"
    assert suggestion.disagrees is False


# --- importing -------------------------------------------------------------

def test_an_import_records_the_measurements_and_suggests_the_machine(database, tmp_path):
    write_output_tree(tmp_path / "out", [a_parbaked_document()])

    with database.session() as session:
        report = import_target(session, tmp_path / "out")

        stored = session.scalars(select(SourceFile)).one()
        assert report.added == [stored.name]
        assert stored.kind == "DJC"
        assert stored.column_count == 3
        assert stored.rows == 39432
        assert stored.source_csv_path.endswith(".csv")
        assert stored.covers_from == date(2022, 8, 9)

        # Suggested. Not assigned.
        assert stored.machine_suggestion == "polaris"
        assert stored.dataset_id is None
        assert stored.machine is None
        assert session.scalars(select(Machine)).all() == []


def test_the_columns_keep_the_order_they_had_in_the_file(database, tmp_path):
    write_output_tree(tmp_path / "out", [a_parbaked_document(
        columns=("JOB_NAME", "MACHINE_NAME", "EXIT_CODE"))])

    with database.session() as session:
        import_target(session, tmp_path / "out")
        stored = session.scalars(select(SourceFile)).one()
        assert [column.name for column in stored.fields] == [
            "JOB_NAME", "MACHINE_NAME", "EXIT_CODE"]
        assert stored.fields[0].measurements["rows_seen"] == 39432


def test_the_text_and_markdown_are_recorded_and_never_opened(database, tmp_path):
    """They are generated FROM the Croissant. Reading them would be reading it twice."""
    root = write_output_tree(tmp_path / "out", [a_parbaked_document()])
    sibling = root / "parbaked_txt" / "ANL-ALCF-DJC-POLARIS_20220809_20221231.txt"
    sibling.chmod(0o000)      # unreadable: an import that opens it will fail

    try:
        with database.session() as session:
            report = import_target(session, root)
            assert report.derived_recorded == 2
            recorded = session.scalars(select(DerivedFile)).all()
            assert sorted(each.format for each in recorded) == ["md", "txt"]
    finally:
        sibling.chmod(0o644)


def test_importing_twice_updates_one_row_rather_than_making_two(database, tmp_path):
    root = write_output_tree(tmp_path / "out", [a_parbaked_document()])

    with database.session() as session:
        first = import_target(session, root)
        second = import_target(session, root)

        assert len(first.added) == 1
        assert second.added == [] and len(second.unchanged) == 1
        assert len(session.scalars(select(SourceFile)).all()) == 1
        assert second.derived_recorded == 0        # already recorded, not doubled


def test_a_moved_output_directory_is_the_same_file_not_a_new_one(database, tmp_path):
    """Matched on the dataset name inside the Croissant, not on where it sits."""
    document = a_parbaked_document()
    write_output_tree(tmp_path / "first", [document])
    write_output_tree(tmp_path / "second", [document])

    with database.session() as session:
        import_target(session, tmp_path / "first")
        report = import_target(session, tmp_path / "second")

        stored = session.scalars(select(SourceFile)).one()
        assert report.unchanged == [stored.name]
        assert "second" in stored.parbaked_path       # where it was last seen


def test_a_remeasured_file_gains_and_loses_columns_without_losing_descriptions(
        database, tmp_path):
    """The point of scoping descriptions to the dataset rather than to the file."""
    root = tmp_path / "out"
    write_output_tree(root, [a_parbaked_document(
        columns=("JOB_NAME", "MACHINE_NAME", "EXIT_CODE"))])

    with database.session() as session:
        import_target(session, root)
        stored = session.scalars(select(SourceFile)).one()

        # Somebody does the work: a machine, a dataset, and a description.
        from models import Dataset
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        dataset = Dataset(machine_id=machine.id, name="POLARIS_DJC", kind="DJC")
        session.add(dataset)
        session.commit()
        session.add(FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE",
                             description="The scheduler's exit status.",
                             data_type="sc:Integer", first_seen_in=stored.id))
        session.commit()

        # The export is re-measured: a column arrives, EXIT_CODE goes away.
        write_output_tree(root, [a_parbaked_document(
            columns=("JOB_NAME", "MACHINE_NAME", "QUEUE_NAME"))])
        report = import_target(session, root)

        name = stored.name
        assert report.remeasured == [name]
        assert report.columns_added[name] == ["QUEUE_NAME"]
        assert report.columns_removed[name] == ["EXIT_CODE"]

        # The measurements followed the file. The judgement did not.
        assert sorted(column.name for column in stored.fields) == [
            "JOB_NAME", "MACHINE_NAME", "QUEUE_NAME"]
        assert session.scalars(select(FieldDoc)).one().description.startswith(
            "The scheduler's")


def test_a_new_file_does_not_report_every_column_as_news(database, tmp_path):
    """266 column names is a count dressed up as news, and it buries what matters."""
    write_output_tree(tmp_path / "out", [a_parbaked_document(
        columns=tuple(f"COLUMN_{index}" for index in range(40)))])

    with database.session() as session:
        report = import_target(session, tmp_path / "out")
        assert report.columns_added == {}
        assert session.scalars(select(SourceFile)).one().column_count == 40


def test_a_finished_croissant_is_skipped_with_a_reason(database, tmp_path):
    """Reading someone's finished work is a different job, and not done quietly."""
    document = a_parbaked_document()
    document["conformsTo"] = ["http://mlcommons.org/croissant/1.0"]
    write_output_tree(tmp_path / "out", [document])

    with database.session() as session:
        report = import_target(session, tmp_path / "out")
        assert report.added == []
        assert len(report.skipped) == 1
        assert "seeded, not imported" in report.skipped[0][1]


def test_an_unreadable_file_does_not_stop_the_rest_of_the_run(database, tmp_path):
    root = write_output_tree(tmp_path / "out", [
        a_parbaked_document(name="GOOD-ONE_20220101_20221231"),
        a_parbaked_document(name="BROKEN-ONE_20220101_20221231")])
    (root / "parbaked_croissants" / "BROKEN-ONE_20220101_20221231.parbaked.json"
     ).write_text("{not json at all")

    with database.session() as session:
        report = import_target(session, root)
        assert report.added == ["GOOD-ONE_20220101_20221231"]
        assert "could not be read" in report.skipped[0][1]


def test_pointing_at_the_croissants_folder_works_as_well_as_the_output_root(
        database, tmp_path):
    """Whichever of the two someone has in their hand should work."""
    root = write_output_tree(tmp_path / "out", [a_parbaked_document()])

    with database.session() as session:
        report = import_target(session, root / "parbaked_croissants")
        assert len(report.added) == 1
        # Siblings are found from the croissants folder's parent either way.
        assert report.derived_recorded == 2


def test_an_empty_directory_is_not_an_error(database, tmp_path):
    (tmp_path / "nothing").mkdir()
    with database.session() as session:
        report = import_target(session, tmp_path / "nothing")
        assert report.total == 0
        assert report.lines()[0].startswith("0 file(s)")


# --- against the real corpus ------------------------------------------------

def test_the_whole_test_corpus_imports(database):
    """The files in bakery_test_out, as parbake actually wrote them."""
    import os
    if not os.path.isdir(REAL_CORPUS):
        pytest.skip("the test corpus is not where it was expected")

    with database.session() as session:
        report = import_target(session, REAL_CORPUS)

        assert report.total >= 17
        assert report.skipped == []

        files = session.scalars(select(SourceFile)).all()
        # Nothing was assigned a machine. That is the whole posture.
        assert all(each.machine is None for each in files)
        # Every file got its dates and its kind off the name.
        assert all(each.covers_from and each.kind for each in files)
        assert session.scalars(select(FileField)).all()

        # Most of the corpus is named <org>-<facility>-<KIND>-<MACHINE>_<dates>
        # and gets a suggestion. The exception is the one file in the other
        # convention -- aurora_crayex_telemetry_power_2024-05-02 -- which has no
        # MACHINE_NAME column either, so nothing in it names a machine. It
        # suggests none rather than guessing, and the person is asked.
        suggested = [each for each in files if each.machine_suggestion]
        unsuggested = [each for each in files if not each.machine_suggestion]
        assert len(suggested) >= 17
        for each in unsuggested:
            assert "-" not in each.name.split("_")[0]      # not the hyphenated convention
