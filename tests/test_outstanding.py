"""Tests for outstanding.py -- what is still unanswered, and what can be offered.

This is where the interface's decisions actually get made, so it is tested
without one. What matters:

    nothing can be asked about a file until its machine is settled
    an offer comes from the nearest place it can, and is never applied by itself
    confirming an answer in one dataset does not touch another's
    the queue puts the least finished first

Run with:
    cd bakery && pytest tests/test_outstanding.py -v
"""

from datetime import date

import pytest
from sqlalchemy import select

from database import Database
from models import (
    Dataset,
    FieldDoc,
    Machine,
    MachineIteration,
    SourceFile,
    STATE_COMPLETE,
    STATE_IN_PROGRESS,
)
from outstanding import (
    confirm_machine,
    coverage_of,
    field_work_for,
    files_for_listing,
    machine_needs_profile,
    outstanding_dataset_questions,
    record_dataset_answer,
    record_field_answer,
)


@pytest.fixture
def database(tmp_path):
    with Database(directory=tmp_path) as opened:
        yield opened


def a_file(session, name="ANL-ALCF-DJC-POLARIS_20220809_20221231", kind="DJC",
           columns=("JOB_NAME", "EXIT_CODE"), suggestion="polaris", **rest):
    from models import FileField
    stored = SourceFile(name=name, kind=kind, machine_suggestion=suggestion,
                        parbaked_path=f"/out/{name}.parbaked.json",
                        parbaked_sha256="0" * 64, column_count=len(columns), **rest)
    session.add(stored)
    session.flush()
    for position, column in enumerate(columns):
        session.add(FileField(file_id=stored.id, name=column, position=position,
                              measurements={"rows_seen": 39432, "distinct_count": 42}))
    session.commit()
    return stored


# --- the machine comes first -----------------------------------------------

def test_nothing_can_be_asked_about_a_file_with_no_machine(database):
    """There is nothing to look a description up against until one is named."""
    with database.session() as session:
        stored = a_file(session)
        assert field_work_for(session, stored) == []
        assert coverage_of(session, stored).columns == 2
        assert coverage_of(session, stored).confirmed == 0


def test_naming_the_machine_is_what_puts_the_file_in_a_dataset(database):
    with database.session() as session:
        stored = a_file(session)
        machine, dataset = confirm_machine(session, stored, "polaris")

        assert machine.name == "polaris"
        assert dataset.name == "POLARIS_DJC"        # <MACHINE>_<KIND>, as the finished files are named
        assert dataset.kind == "DJC"
        assert stored.dataset_id == dataset.id
        assert stored.machine is machine
        assert stored.state == STATE_IN_PROGRESS
        assert len(field_work_for(session, stored)) == 2


def test_a_second_file_of_the_same_kind_joins_the_dataset_rather_than_making_one(
        database):
    """POLARIS_DJC is every date range of it, which is what the finished file describes."""
    with database.session() as session:
        first = a_file(session, name="ANL-ALCF-DJC-POLARIS_20220809_20221231")
        second = a_file(session, name="ANL-ALCF-DJC-POLARIS_20230101_20231231")

        _, one = confirm_machine(session, first, "polaris")
        _, two = confirm_machine(session, second, "Polaris")     # and in another case

        assert one.id == two.id
        assert len(session.scalars(select(Dataset)).all()) == 1
        assert len(session.scalars(select(Machine)).all()) == 1


def test_confirming_a_machine_dates_the_file_to_an_iteration_when_one_covers_it(
        database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        session.add(MachineIteration(machine_id=machine.id, label="as deployed",
                                     starts_on=date(2022, 8, 1)))
        session.commit()

        stored = a_file(session, covers_from=date(2022, 8, 9), covers_to=date(2022, 12, 31))
        confirm_machine(session, stored, "polaris")
        assert stored.iteration.label == "as deployed"


def test_a_file_outside_every_iteration_is_left_undated_rather_than_forced(database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        session.add(MachineIteration(machine_id=machine.id, label="as deployed",
                                     starts_on=date(2022, 8, 1), ends_on=date(2023, 1, 1)))
        session.commit()

        stored = a_file(session, covers_from=date(2019, 1, 1), covers_to=date(2019, 6, 1))
        confirm_machine(session, stored, "polaris")
        assert stored.iteration_id is None


def test_a_machine_with_no_hardware_recorded_says_so(database):
    with database.session() as session:
        machine = Machine("polaris")
        session.add(machine)
        session.commit()
        assert machine_needs_profile(machine) is True


# --- offers ----------------------------------------------------------------

def test_a_sibling_dataset_on_the_same_machine_is_the_nearest_offer(database):
    """COBALT_JOBID means the same thing in every Mira dataset. Usually."""
    with database.session() as session:
        jobs = a_file(session, name="ANL-ALCF-DJC-MIRA_20130409_20131231",
                      columns=("COBALT_JOBID",), suggestion="mira")
        machine, job_dataset = confirm_machine(session, jobs, "mira")
        record_field_answer(session, job_dataset, "COBALT_JOBID",
                            description="The Cobalt job id.", data_type="sc:Integer")

        tasks = a_file(session, name="ANL-ALCF-TH-MIRA_20130409_20131231", kind="TH",
                       columns=("COBALT_JOBID",), suggestion="mira")
        confirm_machine(session, tasks, "mira")

        work = field_work_for(session, tasks)
        assert work[0].offer.kind == "sibling"
        assert work[0].offer.where == "MIRA_DJC"
        assert work[0].description == "The Cobalt job id."
        assert work[0].state == "offered"
        assert "on this machine" in work[0].offer.describe()


def test_another_machine_is_offered_when_no_sibling_has_it(database):
    with database.session() as session:
        polaris_file = a_file(session, columns=("JOB_NAME",))
        _, polaris_djc = confirm_machine(session, polaris_file, "polaris")
        record_field_answer(session, polaris_djc, "JOB_NAME",
                            description="The PBS job identifier.", data_type="sc:Text")

        theta_file = a_file(session, name="ANL-ALCF-DJC-THETA_20170701_20171231",
                            columns=("JOB_NAME",), suggestion="theta")
        confirm_machine(session, theta_file, "theta")

        offer = field_work_for(session, theta_file)[0].offer
        assert offer.kind == "other machine"
        assert offer.where == "POLARIS_DJC"


def test_a_name_in_another_case_is_offered_and_labelled_as_such(database):
    """Worth showing. Not worth assuming: a renamed column may have changed more."""
    with database.session() as session:
        lowercase = a_file(session, name="aurora_dim_job_comp_2026-01", kind="dim_job_comp",
                           columns=("start_timestamp",), suggestion="aurora")
        _, dataset = confirm_machine(session, lowercase, "aurora")
        record_field_answer(session, dataset, "start_timestamp",
                            description="When the job began.", data_type="sc:Text")

        uppercase = a_file(session, name="ANL-ALCF-DJC-AURORA_20250127_20251231",
                           columns=("START_TIMESTAMP",), suggestion="aurora")
        confirm_machine(session, uppercase, "aurora")

        offer = field_work_for(session, uppercase)[0].offer
        assert offer.kind == "respelled"
        assert "another case" in offer.describe()
        assert offer.doc.field_name == "start_timestamp"


def test_an_unconfirmed_answer_already_here_beats_one_from_elsewhere(database):
    """A seeded row is about this dataset. A sibling's is about a different one."""
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_CODE",))
        machine, dataset = confirm_machine(session, stored, "polaris")
        session.add(FieldDoc(dataset_id=dataset.id, field_name="EXIT_CODE",
                             description="From the finished file.", is_confirmed=False,
                             recorded_by="seeded from djc_v4.croissant.json"))
        session.commit()

        offer = field_work_for(session, stored)[0].offer
        assert offer.kind == "seeded"
        assert "unconfirmed" in offer.describe()


def test_a_confirmed_answer_is_preferred_over_an_unconfirmed_one(database):
    with database.session() as session:
        guessed = a_file(session, name="ONE_20220101_20221231", kind="ONE",
                         columns=("SHARED",))
        _, first = confirm_machine(session, guessed, "polaris")
        session.add(FieldDoc(dataset_id=first.id, field_name="SHARED",
                             description="seeded wording", is_confirmed=False))
        session.commit()

        settled = a_file(session, name="TWO_20220101_20221231", kind="TWO",
                         columns=("SHARED",))
        _, second = confirm_machine(session, settled, "polaris")
        record_field_answer(session, second, "SHARED", description="confirmed wording",
                            data_type="sc:Text")

        asking = a_file(session, name="THREE_20220101_20221231", kind="THREE",
                        columns=("SHARED",))
        confirm_machine(session, asking, "polaris")
        assert field_work_for(session, asking)[0].description == "confirmed wording"


def test_a_column_nobody_has_described_anywhere_has_nothing_on_offer(database):
    with database.session() as session:
        stored = a_file(session, columns=("NOTE",))
        confirm_machine(session, stored, "polaris")
        work = field_work_for(session, stored)[0]
        assert work.offer is None
        assert work.state == "unanswered"
        assert work.description is None


def test_an_offer_is_never_applied_on_its_own(database):
    """Showing a default is not recording one. Nothing is written until it is confirmed."""
    with database.session() as session:
        source = a_file(session, name="ONE_20220101_20221231", kind="ONE",
                        columns=("SHARED",))
        _, first = confirm_machine(session, source, "polaris")
        record_field_answer(session, first, "SHARED", description="the wording",
                            data_type="sc:Text")

        asking = a_file(session, name="TWO_20220101_20221231", kind="TWO",
                        columns=("SHARED",))
        _, second = confirm_machine(session, asking, "polaris")

        work = field_work_for(session, asking)[0]
        assert work.description == "the wording"       # offered
        assert work.doc is None                        # and not recorded
        assert coverage_of(session, asking).confirmed == 0


# --- recording, and what it does not touch ---------------------------------

def test_confirming_a_column_in_one_dataset_leaves_the_other_alone(database):
    """START_TIMESTAMP is a job starting in one dataset and an outage in another."""
    with database.session() as session:
        jobs = a_file(session, columns=("START_TIMESTAMP",))
        _, job_dataset = confirm_machine(session, jobs, "polaris")
        record_field_answer(session, job_dataset, "START_TIMESTAMP",
                            description="When the job began execution.",
                            data_type="sc:DateTime")

        status = a_file(session, name="ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231",
                        kind="MACHINESTATUS", columns=("START_TIMESTAMP",))
        _, status_dataset = confirm_machine(session, status, "polaris")
        record_field_answer(session, status_dataset, "START_TIMESTAMP",
                            description="When the outage began.", data_type="sc:DateTime")

        both = session.scalars(select(FieldDoc)).all()
        assert len(both) == 2
        assert {each.description for each in both} == {
            "When the job began execution.", "When the outage began."}


def test_an_answer_given_here_is_confirmed_and_remembers_which_file_it_was_for(database):
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_CODE",))
        _, dataset = confirm_machine(session, stored, "polaris")
        recorded = record_field_answer(session, dataset, "EXIT_CODE",
                                       description="The exit code.",
                                       data_type="sc:Integer", unit="", source_file=stored)

        assert recorded.is_confirmed is True
        assert recorded.first_seen_in == stored.id
        assert recorded.unit is None        # an empty unit is no unit, not ""


def test_a_file_becomes_complete_when_its_last_column_is_answered(database):
    with database.session() as session:
        stored = a_file(session, columns=("JOB_NAME", "EXIT_CODE"))
        _, dataset = confirm_machine(session, stored, "polaris")
        assert stored.state == STATE_IN_PROGRESS

        record_field_answer(session, dataset, "JOB_NAME", description="a",
                            data_type="sc:Text", source_file=stored)
        assert stored.state == STATE_IN_PROGRESS

        record_field_answer(session, dataset, "EXIT_CODE", description="b",
                            data_type="sc:Integer", source_file=stored)
        assert stored.state == STATE_COMPLETE
        assert field_work_for(session, stored) == []
        assert coverage_of(session, stored).describe() == "all 2 confirmed"


def test_a_description_without_a_type_does_not_finish_a_column(database):
    """A Croissant field needs both. Half an answer is still a question."""
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_CODE",))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "EXIT_CODE",
                            description="The exit code.", data_type="", source_file=stored)

        assert stored.state == STATE_IN_PROGRESS
        assert len(field_work_for(session, stored)) == 1


# --- the queue -------------------------------------------------------------

def test_the_queue_puts_the_least_finished_first(database):
    """A queue, not an inventory: what needs doing should not be below what is done."""
    with database.session() as session:
        done = a_file(session, name="DONE_20220101_20221231", kind="DONE",
                      columns=("A",))
        _, dataset = confirm_machine(session, done, "polaris")
        record_field_answer(session, dataset, "A", description="a", data_type="sc:Text",
                            source_file=done)
        untouched = a_file(session, name="UNTOUCHED_20220101_20221231", kind="X",
                           columns=("B", "C"))

        order = [each.name for each, _ in files_for_listing(session)]
        assert order.index(untouched.name) < order.index(done.name)


def test_the_evidence_is_what_parbake_measured(database):
    """Without it, "what does exit_code hold?" is a memory test."""
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_CODE",))
        confirm_machine(session, stored, "polaris")
        evidence = field_work_for(session, stored)[0].evidence
        assert "39,432 rows" in evidence
        assert "42 distinct values" in evidence


# --- the questions that are not about a column -----------------------------

def test_the_dataset_questions_are_asked_once_for_the_series(database):
    with database.session() as session:
        stored = a_file(session)
        _, dataset = confirm_machine(session, stored, "polaris")

        before = outstanding_dataset_questions(session, dataset)
        keys = [spec["key"] for spec, _ in before]
        assert "license" in keys and "citeAs" in keys and "rai:dataLimitations" in keys

        record_dataset_answer(session, dataset, "citeAs", "Cite ALCF.")
        after = [spec["key"] for spec, _ in outstanding_dataset_questions(session, dataset)]
        assert "citeAs" not in after
        assert len(after) == len(keys) - 1


def test_a_seeded_dataset_answer_is_still_asked_about(database):
    """Real work by a real person, but nobody has confirmed it here."""
    from models import DatasetAnswer
    with database.session() as session:
        stored = a_file(session)
        _, dataset = confirm_machine(session, stored, "polaris")
        session.add(DatasetAnswer(dataset_id=dataset.id, key="citeAs",
                                  answer="Cite ALCF.", is_confirmed=False))
        session.commit()

        outstanding = dict((spec["key"], found)
                           for spec, found in outstanding_dataset_questions(session, dataset))
        assert "citeAs" in outstanding
        assert outstanding["citeAs"].answer == "Cite ALCF."   # offered, not settled


# --- notes -----------------------------------------------------------------

def test_a_note_is_recorded_against_the_column_it_is_about(database):
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_STATUS",))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "EXIT_STATUS",
                            description="The exit status.", data_type="sc:Integer",
                            note="Disagrees with EXIT_CODE on 122 rows.",
                            source_file=stored)

        work = field_work_for(session, stored, only_outstanding=False)[0]
        assert work.note == "Disagrees with EXIT_CODE on 122 rows."
        assert work.is_settled is True      # a note does not make a column unfinished


def test_another_datasets_note_is_never_offered(database):
    """A note is about the work somebody did on THIS export, not about the column."""
    with database.session() as session:
        source = a_file(session, name="ONE_20220101_20221231", kind="ONE",
                        columns=("SHARED",))
        _, first = confirm_machine(session, source, "polaris")
        record_field_answer(session, first, "SHARED", description="What it holds.",
                            data_type="sc:Text", note="Check this against the log.")

        asking = a_file(session, name="TWO_20220101_20221231", kind="TWO",
                        columns=("SHARED",))
        confirm_machine(session, asking, "polaris")

        work = field_work_for(session, asking)[0]
        assert work.description == "What it holds."     # the description is offered
        assert work.note is None                        # the note is not


def test_a_note_can_be_cleared_by_answering_with_an_empty_one(database):
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_STATUS",))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "EXIT_STATUS", description="a",
                            data_type="sc:Text", note="look at this")
        record_field_answer(session, dataset, "EXIT_STATUS", description="a",
                            data_type="sc:Text", note="")

        assert field_work_for(session, stored, only_outstanding=False)[0].note is None


def test_every_column_can_be_listed_not_only_the_outstanding_ones(database):
    """What the review screen is built on: an answered column has to stay reachable."""
    with database.session() as session:
        stored = a_file(session, columns=("JOB_NAME", "EXIT_CODE"))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "JOB_NAME", description="The id.",
                            data_type="sc:Text", source_file=stored)

        assert [each.name for each in field_work_for(session, stored)] == ["EXIT_CODE"]
        everything = field_work_for(session, stored, only_outstanding=False)
        assert [each.name for each in everything] == ["JOB_NAME", "EXIT_CODE"]
        assert [each.state for each in everything] == ["confirmed", "unanswered"]
