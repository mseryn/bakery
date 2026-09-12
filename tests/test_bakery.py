"""Tests for bakery.py, the interface, driven by Textual's headless driver.

The interface is deliberately thin -- outstanding.py decides what to ask and
this only draws it -- so what is tested here is the drawing and the wiring:

    the queue shows what is there, and an unconfirmed machine is marked as a guess
    the machine screen shows the evidence, not just the suggestion
    confirming a machine goes straight on to the columns
    an offer arrives filled in, and accepting it is one keystroke
    typing over an offer records what was typed, not what was offered
    the cursor holds its file while the queue reorders underneath it

Textual's run_test() needs no terminal. The async bodies are run with
asyncio.run rather than a pytest plugin, which keeps the test dependencies to
pytest alone.

Run with:
    cd bakery && pytest tests/test_bakery.py -v
"""

import asyncio

import pytest
from sqlalchemy import select

from database import Database
from models import Dataset, FieldDoc, FileField, Machine, SourceFile
from outstanding import confirm_machine, record_field_answer
from bakery import (
    BakeryApp,
    FieldScreen,
    FileListScreen,
    MachineScreen,
    MachinesScreen,
    NotesScreen,
    ReviewScreen,
)


def shown(widget):
    """What a Static is currently displaying."""
    return str(widget.render())


@pytest.fixture
def database(tmp_path):
    with Database(directory=tmp_path) as opened:
        yield opened


def a_file(session, name="ANL-ALCF-DJC-POLARIS_20220809_20221231", kind="DJC",
           columns=("JOB_NAME", "EXIT_CODE"), suggestion="polaris", rows=39432):
    stored = SourceFile(name=name, kind=kind, machine_suggestion=suggestion, rows=rows,
                        parbaked_path=f"/out/{name}.parbaked.json",
                        parbaked_sha256="0" * 64, column_count=len(columns))
    session.add(stored)
    session.flush()
    for position, column in enumerate(columns):
        session.add(FileField(file_id=stored.id, name=column, position=position,
                              measurements={"rows_seen": rows, "distinct_count": 42,
                                            "most_common_values": [
                                                {"value": "0", "count": rows}]}))
    session.commit()
    return stored


def drive(database, body):
    """Run one scripted session against a headless app."""
    async def run():
        app = BakeryApp(database)
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            await body(app, pilot)
    asyncio.run(run())


# --- the queue -------------------------------------------------------------

def test_an_empty_database_says_what_to_do_rather_than_showing_a_blank_table(database):
    async def body(app, pilot):
        assert isinstance(app.screen, FileListScreen)
        assert app.screen.query_one("#files").row_count == 0
        assert "--import" in shown(app.screen.query_one("#hint"))
    drive(database, body)


def test_the_queue_lists_files_and_marks_an_unconfirmed_machine_as_a_guess(database):
    with database.session() as session:
        a_file(session)

    async def body(app, pilot):
        table = app.screen.query_one("#files")
        assert table.row_count == 1
        row = table.get_row_at(0)
        assert row[0] == "ANL-ALCF-DJC-POLARIS_20220809_20221231"
        assert row[1] == "DJC"
        assert row[2] == "? polaris"        # a suggestion nobody has confirmed
        assert row[5] == "imported"
        assert "suggestion nobody has confirmed" in shown(app.screen.query_one("#hint"))
    drive(database, body)


# --- the machine -----------------------------------------------------------

def test_the_machine_screen_shows_the_evidence_not_just_the_suggestion(database):
    """A filename saying POLARIS over data saying thetagpu is what someone must see."""
    with database.session() as session:
        from datetime import date
        stored = a_file(session)
        stored.covers_from, stored.covers_to = date(2022, 8, 9), date(2022, 12, 31)
        session.add(Machine("aurora"))
        session.commit()

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()

        assert isinstance(app.screen, MachineScreen)
        evidence = shown(app.screen.query_one("#evidence"))
        assert "2 columns" in evidence and "39432 rows" in evidence
        assert "polaris" in evidence
        assert "2022-08-09" in evidence
        # The machines already named are offered, and the guess is pre-typed.
        assert [each.name for each in app.screen.known] == ["aurora"]
        assert app.screen.query_one("#typed").value == "polaris"
    drive(database, body)


def test_confirming_the_machine_goes_straight_on_to_the_columns(database):
    """Somebody who just said "this is Polaris" was asking to get on with it."""
    with database.session() as session:
        a_file(session)

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        app.screen.action_confirm()
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, FieldScreen)
        assert app.screen.current.name == "JOB_NAME"
    drive(database, body)

    with database.session() as session:
        stored = session.scalars(select(SourceFile)).one()
        assert stored.machine.name == "polaris"
        assert stored.dataset.name == "POLARIS_DJC"


def test_a_machine_screen_with_no_name_typed_does_nothing(database):
    with database.session() as session:
        a_file(session, suggestion=None)

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        assert app.screen.query_one("#typed").value == ""
        app.screen.action_confirm()
        await pilot.pause()
        assert isinstance(app.screen, MachineScreen)      # still asking
    drive(database, body)

    with database.session() as session:
        assert session.scalars(select(Machine)).all() == []


# --- the columns -----------------------------------------------------------

def test_a_column_is_shown_with_what_parbake_measured(database):
    with database.session() as session:
        stored = a_file(session)
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()

        assert isinstance(app.screen, FieldScreen)
        assert "JOB_NAME" in shown(app.screen.query_one("#heading"))
        assert "1/2 outstanding" in shown(app.screen.query_one("#heading"))
        evidence = shown(app.screen.query_one("#evidence"))
        assert "39,432 rows" in evidence and "42 distinct values" in evidence
        assert "nothing on offer" in shown(app.screen.query_one("#offer"))
    drive(database, body)


def test_an_answer_is_written_when_it_is_given_and_the_next_column_comes_up(database):
    """Nobody answers 266 questions in one sitting, so nothing waits for the end."""
    with database.session() as session:
        stored = a_file(session)
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        field = app.screen
        field.query_one("#description").value = "The PBS job identifier."
        field.query_one("#data_type").value = "sc:Text"
        field.action_save()
        await pilot.pause()

        assert field.current.name == "EXIT_CODE"
        assert "1/1 outstanding" in shown(field.query_one("#heading"))
        assert field.query_one("#description").value == ""      # cleared for the next
    drive(database, body)

    with database.session() as session:
        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.field_name == "JOB_NAME"
        assert recorded.description == "The PBS job identifier."
        assert recorded.is_confirmed is True


def test_an_offer_arrives_filled_in_and_accepting_it_is_one_keystroke(database):
    """The difference between 66 questions and 66 confirmations."""
    with database.session() as session:
        source = a_file(session, name="ANL-ALCF-DJC-MIRA_20130409_20131231",
                        columns=("COBALT_JOBID",), suggestion="mira")
        _, dataset = confirm_machine(session, source, "mira")
        record_field_answer(session, dataset, "COBALT_JOBID",
                            description="The Cobalt job id.", data_type="sc:Integer")
        asking = a_file(session, name="ANL-ALCF-TH-MIRA_20130409_20131231", kind="TH",
                        columns=("COBALT_JOBID",), suggestion="mira")
        confirm_machine(session, asking, "mira")

    async def body(app, pilot):
        table = app.screen.query_one("#files")
        row = next(index for index in range(table.row_count)
                   if "TH-MIRA" in str(table.get_row_at(index)[0]))
        table.move_cursor(row=row)
        await pilot.pause()
        app.screen.action_work()
        await pilot.pause()

        field = app.screen
        assert "MIRA_DJC on this machine" in shown(field.query_one("#offer"))
        assert field.query_one("#description").value == "The Cobalt job id."
        assert field.query_one("#data_type").value == "sc:Integer"

        field.action_accept()
        await pilot.pause()
    drive(database, body)

    with database.session() as session:
        dataset = session.scalar(select(Dataset).where(Dataset.name == "MIRA_TH"))
        recorded = session.scalar(select(FieldDoc).where(
            FieldDoc.dataset_id == dataset.id))
        assert recorded.description == "The Cobalt job id."
        assert recorded.is_confirmed is True        # accepted here, on purpose


def test_typing_over_an_offer_records_what_was_typed(database):
    """START_TIMESTAMP is a job starting in one dataset and an outage in another."""
    with database.session() as session:
        jobs = a_file(session, columns=("START_TIMESTAMP",))
        _, dataset = confirm_machine(session, jobs, "polaris")
        record_field_answer(session, dataset, "START_TIMESTAMP",
                            description="When the job began execution.",
                            data_type="sc:DateTime")
        a_file(session, name="ANL-ALCF-MACHINESTATUS-POLARIS_20220809_20221231",
               kind="MACHINESTATUS", columns=("START_TIMESTAMP",))

    async def body(app, pilot):
        table = app.screen.query_one("#files")
        row = next(index for index in range(table.row_count)
                   if "MACHINESTATUS" in str(table.get_row_at(index)[0]))
        table.move_cursor(row=row)
        await pilot.pause()
        app.screen.action_work()
        await pilot.pause()
        app.screen.action_confirm()
        await pilot.pause()
        await pilot.pause()

        field = app.screen
        assert field.query_one("#description").value == "When the job began execution."
        field.query_one("#description").value = "When the outage began."
        field.action_save()
        await pilot.pause()
    drive(database, body)

    with database.session() as session:
        wording = {session.get(Dataset, each.dataset_id).name: each.description
                   for each in session.scalars(select(FieldDoc))}
        assert wording["POLARIS_DJC"] == "When the job began execution."
        assert wording["POLARIS_MACHINESTATUS"] == "When the outage began."


def test_skipping_moves_on_without_recording_anything(database):
    with database.session() as session:
        stored = a_file(session)
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        field = app.screen
        assert field.current.name == "JOB_NAME"
        field.action_skip()
        await pilot.pause()
        assert field.current.name == "EXIT_CODE"
        # And round again, rather than falling off the end.
        field.action_skip()
        await pilot.pause()
        assert field.current.name == "JOB_NAME"
    drive(database, body)

    with database.session() as session:
        assert session.scalars(select(FieldDoc)).all() == []


def test_answering_the_last_column_returns_to_the_queue(database):
    with database.session() as session:
        stored = a_file(session, columns=("JOB_NAME",))
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        field = app.screen
        field.query_one("#description").value = "The PBS job identifier."
        field.query_one("#data_type").value = "sc:Text"
        field.action_save()
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, FileListScreen)
        assert app.screen.query_one("#files").get_row_at(0)[5] == "complete"
    drive(database, body)


def test_the_cursor_holds_its_file_while_the_queue_reorders(database):
    """Finishing one file moves it down the list. The next keystroke must not open another."""
    with database.session() as session:
        a_file(session, name="AAA_20220101_20221231", kind="AAA", columns=("A", "B", "C"))
        working = a_file(session, name="ZZZ_20220101_20221231", kind="ZZZ", columns=("D",))
        confirm_machine(session, working, "polaris")

    async def body(app, pilot):
        table = app.screen.query_one("#files")
        row = next(index for index in range(table.row_count)
                   if str(table.get_row_at(index)[0]).startswith("ZZZ"))
        table.move_cursor(row=row)
        await pilot.pause()
        assert app.screen.selected_file().name.startswith("ZZZ")

        app.screen.action_work()
        await pilot.pause()
        field = app.screen
        field.query_one("#description").value = "something"
        field.query_one("#data_type").value = "sc:Text"
        field.action_save()
        await pilot.pause()
        await pilot.pause()

        # ZZZ is now finished and has sunk below AAA, and the cursor went with it.
        assert isinstance(app.screen, FileListScreen)
        assert app.screen.selected_file().name.startswith("ZZZ")
    drive(database, body)


# --- the machines ----------------------------------------------------------

def test_the_machines_screen_says_when_no_hardware_is_recorded(database):
    with database.session() as session:
        stored = a_file(session)
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_machines()
        await pilot.pause()

        assert isinstance(app.screen, MachinesScreen)
        text = shown(app.screen.query_one("#machines"))
        assert "polaris" in text
        assert "no hardware recorded" in text
        assert "dataset POLARIS_DJC" in text
    drive(database, body)


def test_the_machines_screen_shows_each_iteration_and_its_dates(database):
    from datetime import date
    from models import MachineIteration, MachinePartition
    with database.session() as session:
        machine = Machine("polaris", organization="Argonne National Laboratory")
        session.add(machine)
        session.commit()
        first = MachineIteration(machine_id=machine.id, label="as deployed",
                                 starts_on=date(2022, 8, 1), vendor="HPE",
                                 architecture="Apollo Gen10+", scheduler="PBS Professional")
        first.partitions = [MachinePartition(name="default", node_count=595,
                                             gpu="4x NVIDIA A100 per node")]
        session.add(first)
        session.commit()
        second = first.next_iteration(date(2024, 6, 1), "after the 2024 refresh")
        second.partitions[0].gpu = "4x NVIDIA H100 per node"
        session.add(second)
        session.commit()

    async def body(app, pilot):
        app.screen.action_machines()
        await pilot.pause()
        text = shown(app.screen.query_one("#machines"))

        assert "Argonne National Laboratory" in text
        assert "as deployed: 2022-08-01 to 2024-05-31" in text
        assert "after the 2024 refresh: 2024-06-01 to present" in text
        assert "A100" in text and "H100" in text
        assert "595 nodes" in text
    drive(database, body)


# --- writing it out --------------------------------------------------------

def test_writing_out_a_file_with_no_machine_asks_for_one_instead(database):
    with database.session() as session:
        a_file(session)

    async def body(app, pilot):
        app.screen.action_bake()
        await pilot.pause()
        assert isinstance(app.screen, FileListScreen)       # nothing pushed, nothing written
    drive(database, body)


def test_writing_out_an_unfinished_file_says_what_is_left(database, tmp_path):
    """Half-done is the normal state, so it is written and marked, not refused."""
    import json
    with database.session() as session:
        stored = a_file(session)
        # baking reads the @context back off the par-baked file, so it has to exist.
        parbaked = tmp_path / f"{stored.name}.parbaked.json"
        parbaked.write_text(json.dumps({
            "@context": {"@language": "en", "sc": "https://schema.org/",
                         "cr": "http://mlcommons.org/croissant/"},
            "name": stored.name,
            "conformsTo": "http://mlcommons.org/croissant/PARBAKED-DO-NOT-SUBMIT"}))
        stored.parbaked_path = str(parbaked)
        session.commit()
        confirm_machine(session, stored, "polaris")

    written = tmp_path / "written"

    async def body(app, pilot):
        app.output_directory = written
        app.screen.action_bake()
        await pilot.pause()
    drive(database, body)

    document = json.loads(
        (written / "ANL-ALCF-DJC-POLARIS_20220809_20221231.baked.json").read_text())
    assert "PARBAKED" in document["conformsTo"]
    assert document["_unfinished"]["outstanding"]
    # And the series form went out beside it.
    assert (written / "POLARIS_DJC.baked.json").is_file()


# --- notes, and looking back -----------------------------------------------

def test_a_note_typed_on_a_column_is_recorded_with_it(database):
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_STATUS",))
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        field = app.screen
        field.query_one("#description").value = "The exit status."
        field.query_one("#data_type").value = "sc:Integer"
        field.query_one("#note").value = "Disagrees with EXIT_CODE on 122 rows."
        field.action_save()
        await pilot.pause()
    drive(database, body)

    with database.session() as session:
        recorded = session.scalars(select(FieldDoc)).one()
        assert recorded.note == "Disagrees with EXIT_CODE on 122 rows."
        assert recorded.description == "The exit status."     # kept apart from it


def test_the_columns_screen_goes_backwards_as_well_as_forwards(database):
    """Which it did not: action_skip wrapped forwards and nothing went back."""
    with database.session() as session:
        stored = a_file(session, columns=("A", "B", "C"))
        confirm_machine(session, stored, "polaris")

    async def body(app, pilot):
        app.screen.action_work()
        await pilot.pause()
        field = app.screen
        assert field.current.name == "A"

        field.action_skip()
        await pilot.pause()
        assert field.current.name == "B"

        field.action_previous()
        await pilot.pause()
        assert field.current.name == "A"

        # And back from the first wraps to the last rather than sticking.
        field.action_previous()
        await pilot.pause()
        assert field.current.name == "C"
    drive(database, body)


def test_the_review_screen_lists_every_column_answered_or_not(database):
    with database.session() as session:
        stored = a_file(session, columns=("JOB_NAME", "EXIT_CODE"))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "JOB_NAME", description="The PBS id.",
                            data_type="sc:Text", unit=None,
                            note="Strip .polaris to join.", source_file=stored)

    async def body(app, pilot):
        app.screen.action_review()
        await pilot.pause()

        assert isinstance(app.screen, ReviewScreen)
        table = app.screen.query_one("#columns")
        rows = {table.get_row_at(index)[0]: table.get_row_at(index)
                for index in range(table.row_count)}

        # The answered one is still there, which is the whole point.
        assert rows["JOB_NAME"][1] == "set"
        assert rows["JOB_NAME"][2] == "Text"
        assert rows["JOB_NAME"][4] == "yes"          # it has a note
        assert rows["EXIT_CODE"][1] == "-"
        assert "1 with a note" in shown(app.screen.query_one("#title"))
    drive(database, body)


def test_picking_a_confirmed_column_from_the_review_screen_reopens_it(database):
    """Previously unreachable: an answered column left the queue for good."""
    with database.session() as session:
        stored = a_file(session, columns=("JOB_NAME", "EXIT_CODE"))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "JOB_NAME", description="The PBS id.",
                            data_type="sc:Text", source_file=stored)

    async def body(app, pilot):
        app.screen.action_review()
        await pilot.pause()
        table = app.screen.query_one("#columns")
        row = next(index for index in range(table.row_count)
                   if table.get_row_at(index)[0] == "JOB_NAME")
        table.move_cursor(row=row)
        await pilot.pause()
        app.screen.action_edit()
        await pilot.pause()
        await pilot.pause()

        field = app.screen
        assert isinstance(field, FieldScreen)
        assert field.current.name == "JOB_NAME"           # opened at the one picked
        assert field.include_settled is True
        assert "CONFIRMED, editing" in shown(field.query_one("#heading"))
        assert field.query_one("#description").value == "The PBS id."

        field.query_one("#description").value = "The PBS job identifier; strip .polaris."
        field.action_save()
        await pilot.pause()
    drive(database, body)

    with database.session() as session:
        recorded = session.scalar(select(FieldDoc).where(FieldDoc.field_name == "JOB_NAME"))
        assert recorded.description == "The PBS job identifier; strip .polaris."


def test_reviewing_a_file_with_no_machine_asks_for_one_first(database):
    with database.session() as session:
        a_file(session)

    async def body(app, pilot):
        app.screen.action_review()
        await pilot.pause()
        assert isinstance(app.screen, FileListScreen)     # nothing opened
    drive(database, body)


def test_the_notes_screen_lists_what_has_been_flagged(database):
    with database.session() as session:
        stored = a_file(session, columns=("EXIT_STATUS", "JOB_NAME"))
        _, dataset = confirm_machine(session, stored, "polaris")
        record_field_answer(session, dataset, "EXIT_STATUS", description="The status.",
                            data_type="sc:Integer",
                            note="Disagrees with EXIT_CODE on 122 rows.",
                            source_file=stored)

    async def body(app, pilot):
        app.screen.action_notes()
        await pilot.pause()

        assert isinstance(app.screen, NotesScreen)
        text = shown(app.screen.query_one("#notes"))
        assert "1 note(s) recorded" in text
        assert "POLARIS_DJC" in text
        assert "records / EXIT_STATUS" in text
        assert "Disagrees with EXIT_CODE on 122 rows." in text
        assert "JOB_NAME" not in text        # no note on it
    drive(database, body)


def test_the_notes_screen_says_how_to_make_one_when_there_are_none(database):
    with database.session() as session:
        a_file(session)

    async def body(app, pilot):
        app.screen.action_notes()
        await pilot.pause()
        text = shown(app.screen.query_one("#notes"))
        assert "No notes recorded yet" in text
        assert "--notes" in text            # and where the other listing lives
    drive(database, body)
