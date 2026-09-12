"""Tests for bake_croissants.py.

The prompting is driven by an injected `ask`, so a whole session can be scripted
and checked without a terminal.

Run with:

    cd bakery && pytest
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "parbake"))

from bake_croissants import (          # noqa: E402
    OUTPUT_SUFFIX,
    bake_document,
    bake_file,
    finish_if_complete,
    output_path_for,
    remaining_questions,
)
from questions import (                # noqa: E402
    DATA_TYPE_CHOICES,
    describe_measurements,
    find_questions,
    is_parbaked,
    set_answer,
    wrap_answer,
)
from describe_csv import describe_csv  # noqa: E402
from parbaked_croissant import build_parbaked_croissant  # noqa: E402


@pytest.fixture
def parbaked(tmp_path):
    """A real par-baked document, built the way parbake builds them."""
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text("username,exit_code\nchulwoo,0\nazamatm,143\nsomeone,0\n")
    return build_parbaked_croissant(describe_csv(csv_path))


def scripted(answers):
    """An `ask` that returns each answer in turn, then stops."""
    remaining = list(answers)

    def ask(_prompt):
        return remaining.pop(0) if remaining else "q"
    return ask


def answers_reaching(document, key, *values):
    """A script that skips past everything before `key`, then answers it.

    Counting positions is brittle: a list question swallows the answers after it
    until a blank line, so inserting one shifts everything below. Skipping by
    name does not care.
    """
    keys = [question.key for question in find_questions(document)]
    assert key in keys, f"no question named {key!r}; have {keys}"
    return scripted([""] * keys.index(key) + list(values) + ["q"])


def silent(*_args, **_kwargs):
    """A `say` that says nothing, to keep test output readable."""


# --- recognising a par-baked file -----------------------------------------

def test_a_parbaked_file_is_recognised(parbaked):
    assert is_parbaked(parbaked) is True


def test_a_finished_file_is_not(parbaked):
    parbaked["conformsTo"] = "http://mlcommons.org/croissant/1.0"
    assert is_parbaked(parbaked) is False


# --- finding the gaps -----------------------------------------------------

def test_it_asks_about_every_field_twice(parbaked):
    """A description and a type for each column."""
    questions = find_questions(parbaked)
    field_questions = [q for q in questions if len(q.where) > 3]
    assert len(field_questions) == 2 * len(parbaked["recordSet"][0]["field"])


def test_it_asks_for_the_dataset_level_things_parbake_leaves_out(parbaked):
    keys = {q.key for q in find_questions(parbaked)}
    for expected in ("citeAs", "license", "version", "rai:dataLimitations"):
        assert expected in keys


def test_the_parbaked_description_counts_as_missing(parbaked):
    """parbake fills it with the warning banner, which is not a description."""
    assert "description" in {q.key for q in find_questions(parbaked)}


def test_an_answered_question_is_not_asked_again(parbaked):
    before = remaining_questions(parbaked)
    parbaked["citeAs"] = "Cite this."
    assert remaining_questions(parbaked) == before - 1


def test_a_field_that_has_been_filled_in_is_not_asked_about(parbaked):
    field = parbaked["recordSet"][0]["field"][0]
    field["description"] = "The submitting user, anonymised."
    field["dataType"] = "sc:Text"

    remaining = {q.key for q in find_questions(parbaked)}
    assert "username / description" not in remaining
    assert "username / dataType" not in remaining


def test_dataset_questions_come_before_field_questions(parbaked):
    """The dataset answers give context for the field ones."""
    questions = find_questions(parbaked)
    first_field = next(i for i, q in enumerate(questions) if len(q.where) > 3)
    assert all(len(q.where) == 1 for q in questions[:1])
    assert first_field > 0


# --- the evidence shown with each field question --------------------------

def test_field_questions_carry_the_measurements(parbaked):
    """Without these the question is a memory test."""
    question = next(q for q in find_questions(parbaked) if q.key == "exit_code / description")
    assert "3 rows" in question.evidence
    assert "distinct" in question.evidence


def test_measurements_call_out_a_dead_column():
    evidence = describe_measurements({
        "rows_seen": 100, "empty_count": 100, "distinct_count": 0,
        "is_all_empty": True, "most_common_values": [],
    })
    assert "EVERY ROW IS EMPTY" in evidence


def test_measurements_call_out_a_constant_column():
    evidence = describe_measurements({
        "rows_seen": 100, "empty_count": 0, "distinct_count": 1,
        "holds_one_value": True, "is_all_empty": False,
        "most_common_values": [{"value": "-1", "count": 100}],
    })
    assert "ONE VALUE COVERS" in evidence
    assert "sentinel" in evidence


def test_measurements_say_when_a_count_is_a_floor():
    evidence = describe_measurements({
        "rows_seen": 50_000, "distinct_count": 1000,
        "distinct_count_is_at_least": True, "most_common_values": [],
    })
    assert "at least" in evidence


def test_a_column_with_no_measurements_gives_no_evidence():
    assert describe_measurements(None) == ""


# --- answering ------------------------------------------------------------

def test_an_answer_is_written_into_the_document(parbaked):
    bake_document(parbaked, answers_reaching(parbaked, "citeAs", "Cite this please."), silent)
    assert parbaked["citeAs"] == "Cite this please."


def test_organisations_are_wrapped_as_objects():
    assert wrap_answer("creator", "Argonne") == {
        "@type": "sc:Organization", "name": "Argonne"}


def test_a_licence_is_wrapped_as_a_creative_work():
    assert wrap_answer("license", "Attribution required")["@type"] == "sc:CreativeWork"


def test_plain_answers_are_left_alone():
    assert wrap_answer("version", "1.0") == "1.0"


def test_a_blank_answer_skips_without_recording_anything(parbaked):
    answered, skipped, _ = bake_document(parbaked, scripted([""]), silent)
    assert answered == 0 and skipped == 1
    assert "description" not in parbaked or "PAR-BAKED" in parbaked["description"]


def test_q_stops_and_keeps_what_came_before(parbaked):
    answered, _, stopped = bake_document(
        parbaked, scripted(["A description.", "1.0", "q"]), silent)
    assert stopped is True
    assert answered == 2
    assert parbaked["version"] == "1.0"


def test_a_list_question_collects_until_a_blank_line(parbaked):
    bake_document(
        parbaked,
        answers_reaching(parbaked, "keywords", "one", "two", "three", ""),
        silent,
    )
    assert parbaked["keywords"] == ["one", "two", "three"]


def test_q_stops_a_list_question_rather_than_being_collected(parbaked):
    """Without this, "q" becomes another keyword -- and an `ask` that keeps
    returning it loops forever. That is what killed the first test run."""
    answered, _, stopped = bake_document(
        parbaked, answers_reaching(parbaked, "keywords", "one", "q"), silent)

    assert stopped is True
    assert "keywords" not in parbaked


def test_a_yes_or_no_question_records_a_boolean(parbaked):
    bake_document(parbaked, answers_reaching(parbaked, "rai:hasSyntheticData", "n"), silent)
    assert parbaked["rai:hasSyntheticData"] is False


def test_a_yes_or_no_question_also_takes_yes(parbaked):
    bake_document(parbaked, answers_reaching(parbaked, "rai:hasSyntheticData", "y"), silent)
    assert parbaked["rai:hasSyntheticData"] is True


def test_a_type_can_be_chosen_by_number(parbaked):
    bake_document(parbaked, answers_reaching(parbaked, "username / dataType", "1"), silent)
    assert parbaked["recordSet"][0]["field"][0]["dataType"] == DATA_TYPE_CHOICES[0]


def test_a_type_can_also_be_typed_out(parbaked):
    bake_document(
        parbaked, answers_reaching(parbaked, "username / dataType", "sc:Integer"), silent)
    assert parbaked["recordSet"][0]["field"][0]["dataType"] == "sc:Integer"


def test_an_unrecognised_type_is_rejected_and_asked_again(parbaked):
    bake_document(
        parbaked,
        answers_reaching(parbaked, "username / dataType", "sc:Nonsense", "2"),
        silent,
    )
    assert parbaked["recordSet"][0]["field"][0]["dataType"] == DATA_TYPE_CHOICES[1]


def test_asking_for_help_does_not_count_as_an_answer(parbaked):
    answered, _, _ = bake_document(
        parbaked, scripted(["?", "A description.", "q"]), silent)
    assert answered == 1
    assert parbaked["description"] == "A description."


def test_help_is_shown_when_asked_for(parbaked):
    said = []
    bake_document(parbaked, scripted(["?", "q"]), said.append)
    assert any("two or three sentences" in line or "par-baked warning" in line
               for line in said)


# --- the par-baked marker -------------------------------------------------

def test_the_marker_stays_while_anything_is_outstanding(parbaked):
    bake_document(parbaked, scripted(["A description.", "q"]), silent)
    finish_if_complete(parbaked)

    assert is_parbaked(parbaked) is True
    assert "_parbake" in parbaked


def test_the_marker_goes_only_when_nothing_is_left(parbaked):
    """Removing it early would throw away the one thing stopping an unfinished
    file from looking finished."""
    for question in find_questions(parbaked):
        set_answer(parbaked, question.where, "answered")

    assert finish_if_complete(parbaked) is True
    assert is_parbaked(parbaked) is False
    assert "_parbake" not in parbaked
    assert "http://mlcommons.org/croissant/1.0" in parbaked["conformsTo"]


def test_the_measurements_survive_being_finished(parbaked):
    """_parbake goes, but what was measured is still worth keeping."""
    for question in find_questions(parbaked):
        set_answer(parbaked, question.where, "answered")
    finish_if_complete(parbaked)
    assert "_parbake_measurements" in parbaked


# --- files ----------------------------------------------------------------

def test_the_output_is_a_new_file_not_the_original(tmp_path):
    source = tmp_path / "jobs.parbaked.json"
    assert output_path_for(source, tmp_path).name == f"jobs{OUTPUT_SUFFIX}"


def test_it_never_writes_the_reserved_name(tmp_path):
    assert ".croissant.json" not in output_path_for(
        tmp_path / "jobs.parbaked.json", tmp_path).name


def test_the_parbaked_file_is_left_untouched(parbaked, tmp_path):
    source = tmp_path / "jobs.parbaked.json"
    source.write_text(json.dumps(parbaked, indent=2))
    before = source.read_text()

    bake_file(source, tmp_path / "out", ask=scripted(["A description.", "q"]), say=silent)
    assert source.read_text() == before


def test_answers_are_saved_as_they_are_given(parbaked, tmp_path):
    """A hundred-question file will not be finished in one sitting, so stopping
    halfway must lose nothing."""
    source = tmp_path / "jobs.parbaked.json"
    source.write_text(json.dumps(parbaked, indent=2))

    outcome = bake_file(source, tmp_path / "out",
                        ask=scripted(["A description.", "1.0", "q"]), say=silent)
    written = json.loads(outcome["output"].read_text())

    assert written["description"] == "A description."
    assert written["version"] == "1.0"


def test_running_again_carries_on_where_it_stopped(parbaked, tmp_path):
    source = tmp_path / "jobs.parbaked.json"
    source.write_text(json.dumps(parbaked, indent=2))
    output = tmp_path / "out"

    first = bake_file(source, output, ask=scripted(["A description.", "q"]), say=silent)
    second = bake_file(source, output, ask=scripted(["1.0", "q"]), say=silent)

    assert second["remaining"] == first["remaining"] - 1
    written = json.loads(second["output"].read_text())
    assert written["description"] == "A description."   # kept from the first run
    assert written["version"] == "1.0"                   # added by the second


def test_a_file_with_nothing_outstanding_asks_nothing(parbaked, tmp_path):
    for question in find_questions(parbaked):
        set_answer(parbaked, question.where, "answered")
    source = tmp_path / "jobs.parbaked.json"
    source.write_text(json.dumps(parbaked, indent=2))

    def refuse(_prompt):
        raise AssertionError("should not have asked anything")

    outcome = bake_file(source, tmp_path / "out", ask=refuse, say=silent)
    assert outcome["remaining"] == 0


# --- the checksum ---------------------------------------------------------

def test_a_checksum_is_computed_rather_than_asked_for(parbaked, tmp_path):
    """A Croissant FileObject needs one, and it is the only gap here that is
    purely mechanical. Asking a person to type 64 hex characters would waste
    their attention and invite a typo."""
    from bake_croissants import add_checksum_if_missing, sha256_of

    csv_path = Path(parbaked["_parbake"]["source_file"])
    assert add_checksum_if_missing(parbaked) == "added"
    assert parbaked["distribution"][0]["sha256"] == sha256_of(csv_path)


def test_an_existing_checksum_is_left_alone(parbaked):
    from bake_croissants import add_checksum_if_missing

    parbaked["distribution"][0]["sha256"] = "a" * 64
    assert add_checksum_if_missing(parbaked) == "already there"
    assert parbaked["distribution"][0]["sha256"] == "a" * 64


def test_a_moved_source_file_is_reported_not_guessed_at(parbaked):
    """Better to say the data has moved than to leave a silent gap."""
    from bake_croissants import add_checksum_if_missing

    parbaked["_parbake"]["source_file"] = "/nowhere/gone.csv"
    said = []
    assert add_checksum_if_missing(parbaked, said.append) == "source missing"
    assert "sha256" not in parbaked["distribution"][0]
    assert any("not where" in line for line in said)


def test_the_checksum_is_never_asked_about_as_a_question(parbaked):
    """It is filled in mechanically, so it must not also appear as a prompt."""
    assert not any("sha256" in q.key or "checksum" in q.key.lower()
                   for q in find_questions(parbaked))


# --- the whole round trip -------------------------------------------------

def test_answering_everything_produces_a_finished_file(parbaked, tmp_path):
    """The promise of the pair: parbake writes something that cannot validate,
    and working through every question produces something that can."""
    source = tmp_path / "jobs.parbaked.json"
    source.write_text(json.dumps(parbaked, indent=2))

    state = {"in_list": False}

    def ask(prompt):
        if "one per line" in prompt:
            state["in_list"] = True
            return "An entry."
        if state["in_list"] and prompt.strip() == ">":
            state["in_list"] = False
            return ""
        if "(y/n)" in prompt:
            return "n"
        if "number, or type" in prompt:
            return "1"
        if "YYYY-MM-DD" in prompt:
            return "2026-09-11"
        return "An answer."

    outcome = bake_file(source, tmp_path / "out", ask=ask, say=silent)
    finished = json.loads(outcome["output"].read_text())

    assert outcome["remaining"] == 0
    assert outcome["checksum"] == "added"
    assert not is_parbaked(finished)
    assert finished["distribution"][0]["sha256"]
    assert all("dataType" in f for f in finished["recordSet"][0]["field"])
    assert "NOT REVIEWED" not in json.dumps(finished)


# --- finding the files parbake wrote --------------------------------------

def parbake_output(root, names=("jobs", "lookup")):
    """A directory laid out the way parbake lays one out."""
    from parbake_link import (
        CROISSANT_SUBDIRECTORY, MARKDOWN_SUBDIRECTORY, TEXT_SUBDIRECTORY,
    )
    for folder in (CROISSANT_SUBDIRECTORY, MARKDOWN_SUBDIRECTORY, TEXT_SUBDIRECTORY):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "DIRECTORY_DOCUMENTATION.txt").write_text("index")
    for name in names:
        (root / CROISSANT_SUBDIRECTORY / f"{name}.parbaked.json").write_text("{}")
        (root / MARKDOWN_SUBDIRECTORY / f"{name}.parbaked.md").write_text("#")
        (root / TEXT_SUBDIRECTORY / f"{name}.txt").write_text("report")
    return root


def test_it_finds_croissants_from_the_output_root(tmp_path):
    """Anyone who has just run parbake is holding the output root, and should
    not have to know which subdirectory the Croissants went into."""
    from bake_croissants import find_parbaked_files

    root = parbake_output(tmp_path / "out")
    found = find_parbaked_files(root)

    assert [p.name for p in found] == ["jobs.parbaked.json", "lookup.parbaked.json"]


def test_it_also_accepts_the_croissants_folder_directly(tmp_path):
    from bake_croissants import find_parbaked_files
    from parbake_link import CROISSANT_SUBDIRECTORY

    root = parbake_output(tmp_path / "out")
    found = find_parbaked_files(root / CROISSANT_SUBDIRECTORY)

    assert len(found) == 2


def test_it_accepts_a_single_file(tmp_path):
    from bake_croissants import find_parbaked_files
    from parbake_link import CROISSANT_SUBDIRECTORY

    root = parbake_output(tmp_path / "out")
    one = root / CROISSANT_SUBDIRECTORY / "jobs.parbaked.json"

    assert find_parbaked_files(one) == [one]


def test_it_does_not_wander_into_the_markdown_or_text_folders(tmp_path):
    """Only .parbaked.json is a thing to bake."""
    from bake_croissants import find_parbaked_files

    root = parbake_output(tmp_path / "out")
    found = find_parbaked_files(root)

    assert all(p.suffix == ".json" for p in found)


def test_a_directory_with_nothing_to_bake_finds_nothing(tmp_path):
    from bake_croissants import find_parbaked_files

    empty = tmp_path / "somewhere"
    empty.mkdir()
    (empty / "notes.txt").write_text("not a croissant")

    assert find_parbaked_files(empty) == []


def test_results_are_in_a_stable_order(tmp_path):
    """Two runs over the same directory should ask in the same order."""
    from bake_croissants import find_parbaked_files

    root = parbake_output(tmp_path / "out", names=("zebra", "apple", "mango"))
    found = [p.name for p in find_parbaked_files(root)]

    assert found == sorted(found)
