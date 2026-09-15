"""Tests for transfer.py -- changing a file's machine, and copying answers.

Run with:
    cd bakery && pytest tests/test_transfer.py -v
"""

from datetime import date

import pytest
from sqlalchemy import select

from database import Database
from models import (
    Dataset,
    DatasetAnswer,
    Enumeration,
    FieldDoc,
    FileField,
    Machine,
    MachineIteration,
    SourceFile,
    STATE_COMPLETE,
)
from outstanding import confirm_machine, field_work_for, record_dataset_answer, record_field_answer
from transfer import apply_copy, change_machine, copy_candidates, plan_copy


@pytest.fixture
def database(tmp_path):
    with Database(directory=tmp_path) as opened:
        yield opened


def a_file(session, name, kind="MACHINESTATUS", columns=("MACHINE_NAME", "NOTE"), **rest):
    stored = SourceFile(name=name, kind=kind, parbaked_path=f"/out/{name}.parbaked.json",
                        parbaked_sha256="0" * 64, column_count=len(columns), **rest)
    session.add(stored)
    session.flush()
    for position, column in enumerate(columns):
        session.add(FileField(file_id=stored.id, name=column, position=position))
    session.commit()
    return stored


def doc(session, dataset, name):
    return session.scalar(select(FieldDoc).where(
        FieldDoc.dataset_id == dataset.id, FieldDoc.field_name == name))


def datasets(session):
    return sorted(each.name for each in session.scalars(select(Dataset)))


# --- changing the machine --------------------------------------------------

def test_a_file_confirmed_as_the_wrong_machine_moves_to_the_right_one(database):
    """The accident: METIS logs confirmed as ThetaGPU, before any answers were given."""
    with database.session() as session:
        stored = a_file(session, "METIS.logs_20260814_1", kind="logs", columns=("pod",))
        confirm_machine(session, stored, "ThetaGPU")
        a_file(session, "ANL-ALCF-MACHINESTATUS-THETAGPU_20200922_20201231")
        other = session.scalar(select(SourceFile).where(SourceFile.name.like("%THETAGPU%")))
        confirm_machine(session, other, "ThetaGPU")

        change = change_machine(session, stored, "Metis")

        assert stored.machine.name == "Metis"
        assert stored.dataset.name == "METIS_logs"
        assert change.removed_dataset == "THETAGPU_logs"
        assert datasets(session) == ["METIS_logs", "THETAGPU_MACHINESTATUS"]
        # The other ThetaGPU file is untouched.
        assert other.dataset.name == "THETAGPU_MACHINESTATUS"


def test_the_only_files_answers_move_with_it(database):
    with database.session() as session:
        stored = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231")
        _, wrong = confirm_machine(session, stored, "aurora")
        record_field_answer(session, wrong, "MACHINE_NAME", description="The machine.",
                            data_type="sc:Text", note="Always one value.", source_file=stored)
        session.add(FieldDoc(dataset_id=wrong.id, field_name="NOTE",
                             description="Seeded wording.", is_confirmed=False))
        record_dataset_answer(session, wrong, "citeAs", "Cite the ALCF.")
        session.commit()

        change = change_machine(session, stored, "polaris")
        right = stored.dataset

        assert change.moved is True
        assert (change.columns, change.answers) == (2, 1)
        moved = doc(session, right, "MACHINE_NAME")
        assert (moved.description, moved.note, moved.is_confirmed) == (
            "The machine.", "Always one value.", True)
        # An unconfirmed answer stays unconfirmed; moving does not confirm it.
        assert doc(session, right, "NOTE").is_confirmed is False
        assert session.scalars(select(DatasetAnswer)).one().dataset_id == right.id
        assert "AURORA_MACHINESTATUS" not in datasets(session)
        # Moved, not duplicated: the two rows exist once, in the new dataset.
        assert len(session.scalars(select(FieldDoc)).all()) == 2


def test_answers_are_copied_when_other_files_stay_behind(database):
    """Two date ranges shared a dataset; only one of them was the wrong machine."""
    with database.session() as session:
        wrong_file = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231")
        right_file = a_file(session, "ANL-ALCF-MACHINESTATUS-AURORA_20250127_20251231")
        _, shared = confirm_machine(session, wrong_file, "aurora")
        confirm_machine(session, right_file, "aurora")
        record_field_answer(session, shared, "MACHINE_NAME", description="The machine.",
                            data_type="sc:Text")

        change = change_machine(session, wrong_file, "polaris")

        assert change.moved is False and change.removed_dataset is None
        assert doc(session, shared, "MACHINE_NAME").description == "The machine."
        assert doc(session, wrong_file.dataset, "MACHINE_NAME").description == "The machine."
        assert right_file.dataset.name == "AURORA_MACHINESTATUS"
        assert "keeps its answers" in "\n".join(change.lines())


def test_a_confirmed_answer_already_in_the_new_dataset_is_kept_and_listed(database):
    with database.session() as session:
        existing_file = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220101_20220808")
        _, right = confirm_machine(session, existing_file, "polaris")
        record_field_answer(session, right, "MACHINE_NAME", description="Polaris, always.",
                            data_type="sc:Text")
        session.add(FieldDoc(dataset_id=right.id, field_name="NOTE",
                             description="unconfirmed here", is_confirmed=False))
        session.commit()

        moving = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231")
        _, wrong = confirm_machine(session, moving, "aurora")
        record_field_answer(session, wrong, "MACHINE_NAME", description="A different reading.",
                            data_type="sc:Text")
        record_field_answer(session, wrong, "NOTE", description="Free text from operators.",
                            data_type="sc:Text")

        change = change_machine(session, moving, "polaris")

        assert doc(session, right, "MACHINE_NAME").description == "Polaris, always."
        assert change.kept_differences == ["MACHINE_NAME"]
        # The unconfirmed one there was replaced by the file's confirmed answer.
        assert doc(session, right, "NOTE").description == "Free text from operators."
        assert doc(session, right, "NOTE").is_confirmed is True


def test_a_decoding_table_link_follows_by_name_or_is_dropped(database):
    with database.session() as session:
        stored = a_file(session, "ANL-ALCF-DJC-POLARIS_20220809_20221231", kind="DJC",
                        columns=("EXIT_CODE", "EXIT_STATUS"))
        _, wrong = confirm_machine(session, stored, "aurora")
        aurora = wrong.machine
        polaris = Machine("polaris")
        session.add(polaris)
        session.flush()
        exit_codes = Enumeration(machine_id=aurora.id, name="exit_code_enum")
        status_codes = Enumeration(machine_id=aurora.id, name="status_enum")
        session.add_all([exit_codes, status_codes,
                         Enumeration(machine_id=polaris.id, name="exit_code_enum")])
        session.flush()
        session.add_all([
            FieldDoc(dataset_id=wrong.id, field_name="EXIT_CODE", description="c",
                     data_type="sc:Integer", enumeration_id=exit_codes.id),
            FieldDoc(dataset_id=wrong.id, field_name="EXIT_STATUS", description="s",
                     data_type="sc:Integer", enumeration_id=status_codes.id)])
        session.commit()

        change = change_machine(session, stored, "polaris")
        right = stored.dataset

        linked = doc(session, right, "EXIT_CODE").enumeration
        assert (linked.name, linked.machine_id) == ("exit_code_enum", polaris.id)
        assert doc(session, right, "EXIT_STATUS").enumeration_id is None
        assert change.links_dropped == ["EXIT_STATUS"]


def test_the_iteration_is_redated_against_the_new_machine(database):
    with database.session() as session:
        stored = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231",
                        covers_from=date(2022, 8, 9), covers_to=date(2022, 12, 31))
        confirm_machine(session, stored, "aurora")
        polaris = Machine("polaris")
        session.add(polaris)
        session.flush()
        session.add(MachineIteration(machine_id=polaris.id, label="as deployed",
                                     starts_on=date(2022, 8, 1)))
        session.commit()

        change_machine(session, stored, "Polaris")
        assert stored.iteration.label == "as deployed"


def test_changing_to_the_machine_it_already_has_changes_nothing(database):
    with database.session() as session:
        stored = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231")
        _, dataset = confirm_machine(session, stored, "polaris")
        change = change_machine(session, stored, "POLARIS")
        assert stored.dataset_id == dataset.id
        assert "Nothing changed" in change.lines()[0]


# --- copying ---------------------------------------------------------------

def two_machinestatus_files(session, columns=("MACHINE_NAME", "START_TIMESTAMP", "NOTE")):
    source_file = a_file(session, "ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231",
                         columns=columns)
    _, source = confirm_machine(session, source_file, "polaris")
    target_file = a_file(session, "ANL-ALCF-MACHINESTATUS-AURORA_20250127_20251231",
                         columns=columns)
    _, target = confirm_machine(session, target_file, "aurora")
    return source, target, target_file


def test_a_plan_counts_what_would_happen_before_anything_is_written(database):
    with database.session() as session:
        source, target, _ = two_machinestatus_files(
            session, columns=("A", "B", "C", "D", "E"))
        for name in "ABCD":
            record_field_answer(session, source, name, description=f"{name} means this.",
                                data_type="sc:Text")
        record_field_answer(session, target, "B", description="B means this.",
                            data_type="sc:Text")
        record_field_answer(session, target, "C", description="C means something else.",
                            data_type="sc:Text")
        session.add(FieldDoc(dataset_id=source.id, field_name="E",
                             description="seeded, not confirmed", is_confirmed=False))
        session.commit()

        plan = plan_copy(session, source, target)

        assert plan.fills == ["A", "D"]
        assert plan.same == ["B"]
        assert [each.name for each in plan.column_differences] == ["C"]
        assert plan.not_confirmed == ["E"]
        assert doc(session, target, "A") is None          # nothing written yet


def test_filling_confirms_and_says_where_it_came_from(database):
    with database.session() as session:
        source, target, target_file = two_machinestatus_files(session)
        for name in ("MACHINE_NAME", "START_TIMESTAMP", "NOTE"):
            record_field_answer(session, source, name, description=f"{name}.",
                                data_type="sc:Text", unit="Timestamp" if "TIME" in name else None,
                                note="Quality note." if name == "NOTE" else None)

        apply_copy(session, plan_copy(session, source, target))

        copied = doc(session, target, "START_TIMESTAMP")
        assert (copied.description, copied.unit, copied.is_confirmed) == (
            "START_TIMESTAMP.", "Timestamp", True)
        assert copied.recorded_by == "copied from POLARIS_MACHINESTATUS"
        assert doc(session, target, "NOTE").note == "Quality note."
        # Every column now answered, so nothing is left to confirm.
        assert field_work_for(session, target_file) == []
        assert target_file.state == STATE_COMPLETE


def test_filling_keeps_what_the_target_had_where_the_source_is_empty(database):
    with database.session() as session:
        source, target, _ = two_machinestatus_files(session)
        record_field_answer(session, source, "START_TIMESTAMP", description="Start.",
                            data_type="sc:Text")
        session.add(FieldDoc(dataset_id=target.id, field_name="START_TIMESTAMP",
                             unit="Timestamp", is_confirmed=False))
        session.commit()

        apply_copy(session, plan_copy(session, source, target))
        filled = doc(session, target, "START_TIMESTAMP")
        assert (filled.description, filled.unit) == ("Start.", "Timestamp")


def test_a_difference_is_only_replaced_when_chosen(database):
    with database.session() as session:
        source, target, _ = two_machinestatus_files(session)
        record_field_answer(session, source, "MACHINE_NAME", description="From the source.",
                            data_type="sc:Text")
        record_field_answer(session, source, "NOTE", description="Source note column.",
                            data_type="sc:Text")
        record_field_answer(session, target, "MACHINE_NAME", description="Already here.",
                            data_type="sc:Text", unit="none")
        record_field_answer(session, target, "NOTE", description="Target note column.",
                            data_type="sc:Text")

        plan = plan_copy(session, source, target)
        choices = {each.name: each for each in plan.column_differences}
        choices["MACHINE_NAME"].take_incoming = True
        choices["NOTE"].take_incoming = False
        apply_copy(session, plan)

        replaced = doc(session, target, "MACHINE_NAME")
        # Replacing takes the source exactly, including its empty unit.
        assert (replaced.description, replaced.unit) == ("From the source.", None)
        assert doc(session, target, "NOTE").description == "Target note column."


def test_dataset_level_answers_are_filled_and_differences_chosen(database):
    with database.session() as session:
        source, target, _ = two_machinestatus_files(session)
        record_dataset_answer(session, source, "citeAs", "Cite the ALCF.")
        record_dataset_answer(session, source, "license", {"@type": "sc:CreativeWork",
                                                           "text": "ALCF terms."})
        record_dataset_answer(session, source, "version", "1.0.0")
        record_dataset_answer(session, target, "version", "2.0.0")

        plan = plan_copy(session, source, target)
        assert sorted(plan.answer_fills) == ["citeAs", "license"]
        assert [each.name for each in plan.answer_differences] == ["version"]

        plan.answer_differences[0].take_incoming = False
        apply_copy(session, plan)
        answers = {row.key: row.answer for row in target.answers}
        assert answers["license"] == {"@type": "sc:CreativeWork", "text": "ALCF terms."}
        assert answers["version"] == "2.0.0"


def test_a_decoding_table_link_is_copied_to_the_target_machines_table_of_that_name(database):
    with database.session() as session:
        source, target, _ = two_machinestatus_files(session, columns=("EXIT_CODE", "STATUS"))
        polaris_codes = Enumeration(machine_id=source.machine_id, name="exit_code_enum")
        polaris_status = Enumeration(machine_id=source.machine_id, name="status_enum")
        aurora_codes = Enumeration(machine_id=target.machine_id, name="exit_code_enum")
        session.add_all([polaris_codes, polaris_status, aurora_codes])
        session.flush()
        session.add_all([
            FieldDoc(dataset_id=source.id, field_name="EXIT_CODE", description="c",
                     data_type="sc:Integer", enumeration_id=polaris_codes.id),
            FieldDoc(dataset_id=source.id, field_name="STATUS", description="s",
                     data_type="sc:Integer", enumeration_id=polaris_status.id)])
        session.commit()

        plan = plan_copy(session, source, target)
        assert plan.links_left_out == ["STATUS"]
        apply_copy(session, plan)

        assert doc(session, target, "EXIT_CODE").enumeration_id == aurora_codes.id
        assert doc(session, target, "STATUS").enumeration_id is None


def test_copy_candidates_put_the_most_useful_dataset_first(database):
    with database.session() as session:
        target_file = a_file(session, "TARGET_20220101_20221231", kind="T",
                             columns=tuple(f"C{index}" for index in range(200)))
        _, target = confirm_machine(session, target_file, "polaris")
        few = a_file(session, "FEW_20220101_20221231", kind="FEW", columns=("C1",))
        _, few_dataset = confirm_machine(session, few, "polaris")
        many = a_file(session, "MANY_20220101_20221231", kind="MANY",
                      columns=tuple(f"C{index}" for index in range(200)))
        _, many_dataset = confirm_machine(session, many, "aurora")
        record_field_answer(session, few_dataset, "C1", description="one", data_type="sc:Text")
        for index in range(200):
            record_field_answer(session, many_dataset, f"C{index}", description=str(index),
                                data_type="sc:Text")

        ranked = copy_candidates(session, target)
        assert [(dataset.name, matching) for dataset, matching, _ in ranked] == [
            ("AURORA_MANY", 200), ("POLARIS_FEW", 1)]

        plan = plan_copy(session, many_dataset, target)
        assert len(plan.fills) == 200
        apply_copy(session, plan)
        assert field_work_for(session, target_file) == []
