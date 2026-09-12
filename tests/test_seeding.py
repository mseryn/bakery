"""Tests for seeding.py.

Seeding reads work somebody has already done. The risk is not that it fails to
read a file -- it is that it reads one too willingly and the database ends up
holding something nobody actually said:

    a placeholder description is not a description
    a person's confirmed answer is never overwritten by a file
    two texts that differ by an appended unit are not two answers
    an enumeration is not linked to a column by guesswork
    nothing seeded counts as settled

Run with:
    cd bakery && pytest tests/test_seeding.py -v
"""

import json
from datetime import date

import pytest
from sqlalchemy import select

from database import Database
from models import (
    Dataset,
    DatasetAnswer,
    Enumeration,
    FieldDoc,
    Machine,
    MachineIteration,
)
from seeding import (
    croissant_type_for,
    looks_like_a_placeholder,
    looks_provisional,
    machine_and_kind_from,
    parse_deployment_dates,
    parse_system_section,
    read_section,
    seed_croissant,
    seed_documentation,
    seed_from,
    SeedReport,
)

FINISHED_CROISSANT = "../croissant_files/djc_v4.croissant.json"
DOCUMENTATION = "../complete_documentation"


@pytest.fixture
def database(tmp_path):
    with Database(directory=tmp_path) as opened:
        yield opened


def a_finished_croissant(name="POLARIS_DJC", fields=None, **extra):
    fields = fields or [
        {"name": "JOB_NAME", "description": "PBS job identifier", "dataType": "sc:Text"},
        {"name": "NODES_USED", "description": "(no meaning supplied for NODES_USED)",
         "dataType": "sc:Integer"},
    ]
    document = {
        "@type": "sc:Dataset",
        "name": name,
        "conformsTo": ["http://mlcommons.org/croissant/1.0"],
        "recordSet": [{"@type": "cr:RecordSet", "name": "records", "field": fields}],
    }
    document.update(extra)
    return document


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


def write_dictionary(path, rows, header="FIELD,DTYPE,UNIT,MEANING"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n" + "\n".join(rows) + "\n")
    return path


SYSTEM_SECTION = """# Documentation — something

## 1. Record metadata
- Originating organization: Argonne National Laboratory

## 3. System
- System name: Polaris
- Vendor: Hewlett Packard Enterprise
- Architecture: HPE Apollo Gen10+
- System partition: Homogeneous (identical) nodes
- CPU: 1x AMD EPYC Milan 7543P (32 cores) per node
- GPU: 4x NVIDIA A100 per node
- Interconnect: HPE Slingshot (Dragonfly topology)
- Memory Type: DDR3
- Storage: Networked (no on-node storage)
- Scheduler: PBS Professional
- Node count: 595
- Rack count:
- System deployment dates: August 2022 - present

## 4. Data records
- Granularity: one row per completed PBS job
"""


# --- reading a type, a placeholder, a hedge --------------------------------

def test_the_dictionaries_type_vocabulary_maps_to_croissant():
    """Every spelling that actually appears across the three dictionaries."""
    assert croissant_type_for("str") == "sc:Text"
    assert croissant_type_for("string") == "sc:Text"
    assert croissant_type_for("int64") == "sc:Integer"
    assert croissant_type_for("integer") == "sc:Integer"
    assert croissant_type_for("float64") == "sc:Float"
    assert croissant_type_for("float") == "sc:Float"
    assert croissant_type_for("string (ISO 8601 datetime)") == "sc:DateTime"


def test_a_type_nobody_recognises_is_a_question_not_a_guess():
    """sc:Text is what a wrong guess looks like, so it is not offered as one."""
    assert croissant_type_for("blob") is None
    assert croissant_type_for("") is None
    assert croissant_type_for(None) is None


def test_a_placeholder_is_not_a_description():
    """38 of djc_v4's 66 descriptions are this. Seeding them would settle them."""
    assert looks_like_a_placeholder("(no meaning supplied for NODES_USED)") is True
    assert looks_like_a_placeholder("(none supplied)") is True
    assert looks_like_a_placeholder("   ") is True
    assert looks_like_a_placeholder("NOT REVIEWED -- a human must write this "
                                    "description.") is True
    assert looks_like_a_placeholder("PBS job identifier") is False
    assert looks_like_a_placeholder("(see section 8 for the caveat)") is False


def test_a_decoding_that_says_it_is_unsure_is_flagged_as_unsure():
    assert looks_provisional("SIGTERM (128+15). PROVISIONAL: ... Open question.") is True
    assert looks_provisional("Ambiguous; treat as unknown failure.") is True
    assert looks_provisional("General error, catch-all (shell exit 1).") is False


def test_a_label_splits_into_a_machine_and_a_kind():
    assert machine_and_kind_from("POLARIS_DJC") == ("POLARIS", "DJC")
    assert machine_and_kind_from("AURORA_POWER-TELEMETRY") == ("AURORA", "POWER-TELEMETRY")
    # A machine already known is recognised wherever it sits in the name.
    assert machine_and_kind_from("anonymized_aurora_djc",
                                 known_machines=["Aurora"]) == ("aurora", "anonymized_djc")


# --- seeding field descriptions --------------------------------------------

def test_a_croissants_descriptions_and_types_are_seeded_but_not_confirmed(
        database, tmp_path):
    path = write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant())

    with database.session() as session:
        report = SeedReport()
        seed_croissant(session, path, report)
        session.commit()

        seeded = session.scalars(select(FieldDoc)).all()
        by_name = {each.field_name: each for each in seeded}

        assert by_name["JOB_NAME"].description == "PBS job identifier"
        assert by_name["JOB_NAME"].data_type == "sc:Text"
        assert by_name["JOB_NAME"].is_confirmed is False
        assert by_name["JOB_NAME"].needs_confirming is True
        assert by_name["JOB_NAME"].recorded_by.startswith("seeded from ")


def test_a_placeholder_description_is_left_out_so_the_field_still_gets_asked(
        database, tmp_path):
    """The whole point. A field with a fake description is never asked about again."""
    path = write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant())

    with database.session() as session:
        seed_croissant(session, path, SeedReport())
        session.commit()

        nodes_used = session.scalar(select(FieldDoc).where(
            FieldDoc.field_name == "NODES_USED"))
        assert nodes_used.description is None      # not "(no meaning supplied ...)"
        assert nodes_used.data_type == "sc:Integer"     # the type was real
        assert nodes_used.is_complete is False          # so it is still outstanding


def test_the_dictionary_supplies_the_units_the_croissant_has_nowhere_to_put(
        database, tmp_path):
    """Neither source is the work on its own. The union is."""
    write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        fields=[{"name": "RUNTIME_SECONDS", "description": "How long the job ran.",
                 "dataType": "sc:Integer"}]))
    write_dictionary(tmp_path / "POLARIS_DJC_field_dictionary.csv",
                     ["RUNTIME_SECONDS,int64,Seconds,"])

    with database.session() as session:
        report = seed_from(session, tmp_path)

        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.description == "How long the job ran."   # from the Croissant
        assert recorded.unit == "Seconds"                        # from the dictionary
        assert recorded.data_type == "sc:Integer"
        assert report.fields_filled == 1


def test_a_confirmed_answer_is_never_overwritten_by_a_file(database, tmp_path):
    """Somebody sat with the measurements and decided. That outranks any document."""
    path = write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        fields=[{"name": "JOB_NAME", "description": "PBS job identifier",
                 "dataType": "sc:Text"}]))

    with database.session() as session:
        machine = Machine("POLARIS")
        session.add(machine)
        session.commit()
        dataset = Dataset(machine_id=machine.id, name="POLARIS_DJC", kind="DJC")
        session.add(dataset)
        session.commit()
        session.add(FieldDoc(dataset_id=dataset.id, field_name="JOB_NAME",
                             description="The PBS id; strip .polaris to join.",
                             data_type="sc:Text", is_confirmed=True))
        session.commit()

        report = SeedReport()
        seed_croissant(session, path, report)
        session.commit()

        kept = session.scalars(select(FieldDoc)).one()
        assert kept.description == "The PBS id; strip .polaris to join."
        assert report.fields_kept == 1
        assert report.fields_added == 0


def test_a_sentence_and_the_same_sentence_plus_a_unit_are_not_two_answers(
        database, tmp_path):
    """djc_v4 appends "Unit: Timestamp." to what the dictionary says. Seven of those."""
    write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        fields=[{"name": "QUEUED_TIMESTAMP", "dataType": "sc:Text",
                 "description": "When the job entered the queue, timestamp "
                                "Unit: Timestamp."}]))
    write_dictionary(tmp_path / "POLARIS_DJC_field_dictionary.csv",
                     ["QUEUED_TIMESTAMP,str,Timestamp,"
                      "\"When the job entered the queue, timestamp\""])

    with database.session() as session:
        report = seed_from(session, tmp_path)
        assert report.conflicts == []
        assert session.scalars(select(FieldDoc)).one().unit == "Timestamp"


def test_two_sources_that_really_disagree_are_reported_and_not_merged(
        database, tmp_path):
    """Where two documents disagree is exactly where somebody needs to look."""
    write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        fields=[{"name": "EXIT_CODE", "dataType": "sc:Integer",
                 "description": "The exit code as PBS recorded it."}]))
    write_dictionary(tmp_path / "POLARIS_DJC_field_dictionary.csv",
                     ["EXIT_CODE,int64,,\"The shell's exit status, not PBS's.\""])

    with database.session() as session:
        report = seed_from(session, tmp_path)

        assert len(report.conflicts) == 1
        what, why = report.conflicts[0]
        assert "EXIT_CODE" in what and "description" in what
        assert "Confirm which is right" in why
        # The first one read is kept rather than a choice being made.
        assert session.scalars(select(FieldDoc)).one().description.endswith(
            "as PBS recorded it.")


def test_a_dictionary_row_with_only_a_type_still_records_the_type(database, tmp_path):
    """The Aurora DJC dictionary has 16 rows and not one meaning. The types are real."""
    write_dictionary(tmp_path / "AURORA_DJC_field_dictionary_v1.0.csv",
                     ["runtime_seconds,float64,,", "queue_name,str,,"])

    with database.session() as session:
        seed_from(session, tmp_path)
        recorded = {each.field_name: each for each in session.scalars(select(FieldDoc))}

        assert recorded["runtime_seconds"].data_type == "sc:Float"
        assert recorded["runtime_seconds"].description is None
        assert recorded["runtime_seconds"].is_complete is False


# --- seeding the dataset-level answers -------------------------------------

def test_the_dataset_answers_keep_their_shape_including_the_licence_object(
        database, tmp_path):
    licence = {"@type": "sc:CreativeWork", "name": "ALCF data attribution requirement",
               "text": "This data was generated from resources of the ALCF...",
               "url": "https://reports.alcf.anl.gov/data/polaris.html"}
    path = write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        license=licence, citeAs="Cite ALCF.",
        keywords=["Polaris", "ALCF"], **{"rai:hasSyntheticData": False}))

    with database.session() as session:
        report = SeedReport()
        seed_croissant(session, path, report)
        session.commit()

        answers = {each.key: each for each in session.scalars(select(DatasetAnswer))}
        assert answers["license"].answer == licence     # the whole object, not just text
        assert answers["keywords"].answer == ["Polaris", "ALCF"]
        assert answers["rai:hasSyntheticData"].answer is False
        assert all(each.is_confirmed is False for each in answers.values())
        assert report.answers_added == 4

        # The answers hang off a dataset, which hangs off a machine.
        dataset = session.scalars(select(Dataset)).one()
        assert dataset.name == "POLARIS_DJC"
        assert dataset.kind == "DJC"
        assert dataset.machine.name == "POLARIS"


def test_a_parbaked_file_is_refused_rather_than_seeded(database, tmp_path):
    """Par-baked measurements are imported. Finished judgements are seeded."""
    document = a_finished_croissant()
    document["conformsTo"] = "http://mlcommons.org/croissant/PARBAKED-DO-NOT-SUBMIT"
    path = write_json(tmp_path / "thing.croissant.json", document)

    with database.session() as session:
        report = SeedReport()
        seed_croissant(session, path, report)
        assert session.scalars(select(FieldDoc)).all() == []
        assert "imported, not seeded" in report.skipped[0][1]


# --- seeding the decodings -------------------------------------------------

def test_an_enumeration_is_seeded_with_its_hedges_intact(database, tmp_path):
    path = write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        fields=[{"name": "EXIT_CODE", "dataType": "sc:Integer",
                 "description": "The exit code."}]))
    document = json.loads(path.read_text())
    document["recordSet"].append({
        "@type": "cr:RecordSet", "@id": "exit_code_enum", "name": "exit_code_enum",
        "cr:isEnumeration": True,
        "description": "Exit-code decodings as they appear in EXIT_CODE.",
        "field": [{"@id": "exit_code_enum/code", "name": "code"},
                  {"@id": "exit_code_enum/meaning", "name": "meaning"}],
        "data": [
            {"exit_code_enum/code": "0", "exit_code_enum/meaning": "Success."},
            {"exit_code_enum/code": "143",
             "exit_code_enum/meaning": "SIGTERM. PROVISIONAL: open question."}]})
    path.write_text(json.dumps(document))

    with database.session() as session:
        report = SeedReport()
        seed_croissant(session, path, report)
        session.commit()

        enumeration = session.scalars(select(Enumeration)).one()
        assert enumeration.name == "exit_code_enum"
        assert [each.code for each in enumeration.values] == ["0", "143"]
        assert enumeration.values[0].is_provisional is False
        assert enumeration.values[1].is_provisional is True


def test_an_enumeration_is_not_linked_to_a_column_by_guesswork(database, tmp_path):
    """djc_v4 links its enum to nothing. Guessing swaps one silent error for another."""
    path = write_json(tmp_path / "POLARIS_DJC.croissant.json", a_finished_croissant(
        fields=[{"name": "EXIT_CODE", "dataType": "sc:Integer", "description": "Code."},
                {"name": "EXIT_STATUS", "dataType": "sc:Integer", "description": "Status."}]))
    document = json.loads(path.read_text())
    document["recordSet"].append({
        "@type": "cr:RecordSet", "@id": "exit_code_enum", "name": "exit_code_enum",
        "cr:isEnumeration": True, "description": "Codes for EXIT_CODE and EXIT_STATUS.",
        "field": [{"@id": "exit_code_enum/code"}, {"@id": "exit_code_enum/meaning"}],
        "data": [{"exit_code_enum/code": "0", "exit_code_enum/meaning": "Success."}]})
    path.write_text(json.dumps(document))

    with database.session() as session:
        report = SeedReport()
        seed_croissant(session, path, report)
        session.commit()

        assert all(each.enumeration_id is None
                   for each in session.scalars(select(FieldDoc)))
        name, why = report.unlinked[0]
        assert name == "exit_code_enum"
        assert "EXIT_CODE" in why       # the candidates are offered, not applied
        assert "link it when confirming" in why


# --- seeding the machine profile -------------------------------------------

def test_the_system_section_is_found_whatever_number_it_carries():
    """System is section 3 in two documents and renumbered in the third."""
    assert read_section(SYSTEM_SECTION, "System")
    assert read_section(SYSTEM_SECTION.replace("## 3. System", "## 9. System"), "System")
    assert read_section(SYSTEM_SECTION.replace("## 3. System", "## System"), "System")
    assert read_section(SYSTEM_SECTION, "Nothing In Here") == []


def test_a_homogeneous_machine_becomes_one_partition():
    profile, partitions = parse_system_section(read_section(SYSTEM_SECTION, "System"))

    assert profile["system_name"] == "Polaris"
    assert profile["vendor"] == "Hewlett Packard Enterprise"
    assert profile["scheduler"] == "PBS Professional"
    assert len(partitions) == 1
    assert partitions[0]["node_count"] == 595
    assert partitions[0]["rack_count"] is None      # the document leaves it blank
    assert partitions[0]["memory_type"] == "DDR3"


def test_a_nested_partition_block_becomes_a_named_partition():
    """Aurora writes its hardware indented under "Partition One:"."""
    nested = """## 3. System
- System name: Aurora
- Vendor: HPE (Hewlett Packard Enterprise)
- Architecture: Cray EX
- System partition: One homogeneous (identical) nodes
- Partition One:
    - CPU: 2x Intel Xeon CPU Max Series
    - GPU: 6x Intel Data Center GPU Max Series
    - Node count: 10,624
    - Rack count: 166
    - System deployment dates: 27 January 2025 - present
"""
    profile, partitions = parse_system_section(read_section(nested, "System"))

    assert profile["system_name"] == "Aurora"
    assert len(partitions) == 1
    assert partitions[0]["name"] == "Partition One"
    assert partitions[0]["node_count"] == 10624     # the comma does not stop it
    assert partitions[0]["rack_count"] == 166


def test_deployment_dates_are_read_without_claiming_more_precision_than_given():
    assert parse_deployment_dates("August 2022 - present") == (date(2022, 8, 1), None)
    assert parse_deployment_dates("27 January 2025 - present") == (date(2025, 1, 27), None)
    assert parse_deployment_dates("April 2013 - December 2019") == (
        date(2013, 4, 1), date(2019, 12, 1))
    assert parse_deployment_dates("2022-08-09 - present") == (date(2022, 8, 9), None)
    assert parse_deployment_dates("") == (None, None)
    assert parse_deployment_dates("whenever it was") == (None, None)


def test_a_documentation_file_becomes_a_machine_and_its_first_iteration(
        database, tmp_path):
    """The only source for any of this. A par-baked file cannot know a node count."""
    path = tmp_path / "POLARIS_DJC_documentation.md"
    path.write_text(SYSTEM_SECTION)

    with database.session() as session:
        report = SeedReport()
        seed_documentation(session, path, report)
        session.commit()

        machine = session.scalars(select(Machine)).one()
        assert machine.name == "Polaris"
        assert machine.organization == "Argonne National Laboratory"

        iteration = session.scalars(select(MachineIteration)).one()
        assert iteration.starts_on == date(2022, 8, 1)
        assert iteration.is_current is True                 # "present"
        assert iteration.dates_as_written == "August 2022 - present"
        assert iteration.is_profiled is True
        assert report.iterations


def test_a_second_document_about_a_profiled_machine_changes_nothing(
        database, tmp_path):
    """Several datasets from one machine each repeat its System section."""
    (tmp_path / "POLARIS_DJC_documentation.md").write_text(SYSTEM_SECTION)
    (tmp_path / "POLARIS_OTHER_documentation.md").write_text(
        SYSTEM_SECTION.replace("Node count: 595", "Node count: 999"))

    with database.session() as session:
        report = seed_from(session, tmp_path)

        iteration = session.scalars(select(MachineIteration)).one()
        assert iteration.partitions[0].node_count == 595    # the first one read
        assert any("already has a profile" in note for note in report.notes)
        # Reported as a note, not as a disagreement: this is the normal case.
        assert report.conflicts == []


def test_a_document_with_no_system_section_is_skipped_with_a_reason(
        database, tmp_path):
    path = tmp_path / "djc_v4_documentation.md"
    path.write_text("# POLARIS_DJC\n\n## Fields\n- a rendering of the Croissant\n")

    with database.session() as session:
        report = SeedReport()
        seed_documentation(session, path, report)
        assert session.scalars(select(Machine)).all() == []
        assert "no System section" in report.skipped[0][1]


# --- against the real finished files ---------------------------------------

def test_the_real_finished_files_seed(database):
    import os
    if not os.path.isdir(DOCUMENTATION) or not os.path.isfile(FINISHED_CROISSANT):
        pytest.skip("the finished files are not where they were expected")

    with database.session() as session:
        report = seed_from(session, DOCUMENTATION, FINISHED_CROISSANT)

        names = sorted(each.name for each in session.scalars(select(Machine)))
        assert names == ["Aurora", "Polaris"]

        polaris = session.scalar(select(Machine).where(Machine.key == "polaris"))
        assert polaris.current_iteration.partitions[0].node_count == 595
        assert polaris.current_iteration.scheduler == "PBS Professional"

        aurora = session.scalar(select(Machine).where(Machine.key == "aurora"))
        assert aurora.current_iteration.partitions[0].node_count == 10624

        fields = session.scalars(select(FieldDoc)).all()
        described = [each for each in fields if each.description]
        # 38 of djc_v4's 66 descriptions are placeholders and must not be here.
        assert len(fields) == 93
        assert 35 <= len(described) <= 45
        assert not any("no meaning supplied" in each.description for each in described)

        # Every unit in the three dictionaries came across.
        assert len([each for each in fields if each.unit]) == 11
        # And nothing at all is settled.
        assert all(each.is_confirmed is False for each in fields)
        assert report.skipped == []
