"""Tests for notes.py -- finding what somebody left for later.

A note is written into the description as "NOTE: ...", so it travels with the
published file. The risk is the marker: too loose and it finds sentences nobody
wrote as notes, too tight and it misses the ones typed by hand before there was
a field for them.

Run with:
    cd bakery && pytest tests/test_notes.py -v
"""

import json

from notes import by_dataset, find_notes, notes_in, notes_in_document, report


def a_croissant(path, name="POLARIS_DJC", description="A dataset.", fields=(),
                parbaked=False, distribution=None, record_set_description=None):
    document = {
        "@type": "sc:Dataset",
        "name": name,
        "description": description,
        "conformsTo": ("http://mlcommons.org/croissant/PARBAKED-DO-NOT-SUBMIT"
                       if parbaked else ["http://mlcommons.org/croissant/1.0"]),
        "distribution": distribution if distribution is not None else [
            {"@type": "cr:FileObject", "name": f"{name}.csv", "description": "The file."}],
        "recordSet": [{"@type": "cr:RecordSet", "name": "records",
                       "description": record_set_description or "One row per job.",
                       "field": [{"name": column, "description": text}
                                 for column, text in fields]}],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


# --- the marker ------------------------------------------------------------

def test_a_note_is_what_follows_the_marker():
    assert notes_in("Core hours used. Unit: Core Hours. NOTE: looks low for Q4.") == [
        "looks low for Q4."]


def test_two_notes_in_one_description_are_two_notes():
    """One typed by hand, one appended by the tool, is a thing that will happen."""
    assert notes_in("Holds the code. NOTE: first thing. NOTE: second thing.") == [
        "first thing.", "second thing."]


def test_the_plural_is_accepted_because_people_write_it():
    assert notes_in("Holds the code. NOTES: several things.") == ["several things."]


def test_the_marker_is_shouted_so_prose_is_not_mistaken_for_a_note():
    """parbake's banner says "Record the responsible-AI notes: limitations, biases".

    Matched case-insensitively, that turned every par-baked file in the corpus
    into a file with a note in it. A marker that catches nineteen things nobody
    wrote is worse than no marker.
    """
    banner = ("Outstanding before this file means anything: Record the "
              "responsible-AI notes: limitations, biases, personal information.")
    assert notes_in(banner) == []
    assert notes_in("See the accompanying notes: they explain the codes.") == []


def test_the_marker_is_a_whole_word():
    assert notes_in("See the FOOTNOTE: not a note.") == []


def test_a_description_with_nothing_in_it_is_survivable():
    assert notes_in("Just a description.") == []
    assert notes_in("") == []
    assert notes_in(None) == []
    assert notes_in(42) == []          # a description that is not a string


def test_an_empty_note_is_not_a_note():
    assert notes_in("Holds the code. NOTE:   ") == []


# --- where notes can be ----------------------------------------------------

def test_a_note_is_found_wherever_a_description_can_be(tmp_path):
    """A note is put wherever the thing it is about is, so all four are read."""
    path = a_croissant(
        tmp_path / "one.json",
        description="The dataset. NOTE: the date range in the name is wrong.",
        distribution=[{"@type": "cr:FileObject", "name": "jobs.csv",
                       "description": "The file. NOTE: also distributed gzipped."}],
        record_set_description="One row per job. NOTE: except the header.",
        fields=[("EXIT_CODE", "The exit code. NOTE: two encodings coexist.")])

    found = notes_in_document(json.loads(path.read_text()), path)
    where = {note.where: note.text for note in found}

    assert where["dataset"] == "the date range in the name is wrong."
    assert where["file jobs.csv"] == "also distributed gzipped."
    assert where["record set records"] == "except the header."
    assert where["records / EXIT_CODE"] == "two encodings coexist."


def test_a_note_carries_enough_to_go_and_act_on_it(tmp_path):
    path = a_croissant(tmp_path / "one.json",
                       fields=[("EXIT_CODE", "The code. NOTE: check it.")])
    note = notes_in_document(json.loads(path.read_text()), path)[0]

    assert note.dataset == "POLARIS_DJC"
    assert note.where == "records / EXIT_CODE"
    assert note.path == path
    assert note.unfinished is False
    assert "EXIT_CODE" in str(note)


def test_a_note_from_an_unfinished_file_says_so(tmp_path):
    """Worth knowing: the note may be about work that is not done rather than data."""
    path = a_croissant(tmp_path / "one.json", parbaked=True,
                       fields=[("EXIT_CODE", "The code. NOTE: check it.")])
    assert notes_in_document(json.loads(path.read_text()), path)[0].unfinished is True

    path = tmp_path / "two.json"
    document = json.loads(a_croissant(
        tmp_path / "two.json", fields=[("A", "x. NOTE: y.")]).read_text())
    document["_unfinished"] = {"outstanding": ["something"]}
    path.write_text(json.dumps(document))
    assert notes_in_document(document, path)[0].unfinished is True


# --- scanning a directory --------------------------------------------------

def test_every_croissant_under_a_directory_is_read(tmp_path):
    a_croissant(tmp_path / "a" / "one.json", name="ONE",
                fields=[("X", "x. NOTE: first.")])
    a_croissant(tmp_path / "b" / "deeper" / "two.json", name="TWO",
                fields=[("Y", "y. NOTE: second.")])

    found, scanned, skipped = find_notes(tmp_path)
    assert scanned == 2
    assert skipped == []
    assert sorted(note.text for note in found) == ["first.", "second."]


def test_a_single_file_works_as_well_as_a_directory(tmp_path):
    path = a_croissant(tmp_path / "one.json", fields=[("X", "x. NOTE: here.")])
    found, scanned, _ = find_notes(path)
    assert scanned == 1 and [note.text for note in found] == ["here."]


def test_json_that_is_not_a_croissant_is_passed_over_silently(tmp_path):
    """A directory of output has an index and a settings file in it too."""
    a_croissant(tmp_path / "real.json", fields=[("X", "x. NOTE: here.")])
    (tmp_path / "settings.json").write_text(json.dumps({"workers": 8}))
    (tmp_path / "a_list.json").write_text(json.dumps([1, 2, 3]))

    found, scanned, skipped = find_notes(tmp_path)
    assert scanned == 1
    assert skipped == []            # not an error, just not a Croissant
    assert len(found) == 1


def test_a_broken_file_is_reported_and_does_not_stop_the_scan(tmp_path):
    a_croissant(tmp_path / "good.json", name="GOOD", fields=[("X", "x. NOTE: here.")])
    (tmp_path / "bad.json").write_text("{not json")

    found, scanned, skipped = find_notes(tmp_path)
    assert [note.text for note in found] == ["here."]
    assert len(skipped) == 1 and "bad.json" in str(skipped[0][0])


def test_scanning_several_paths_at_once(tmp_path):
    a_croissant(tmp_path / "one" / "a.json", name="A", fields=[("X", "x. NOTE: one.")])
    a_croissant(tmp_path / "two" / "b.json", name="B", fields=[("Y", "y. NOTE: two.")])
    found, scanned, _ = find_notes(tmp_path / "one", tmp_path / "two")
    assert scanned == 2 and len(found) == 2


def test_a_path_that_is_not_there_is_not_an_error(tmp_path):
    found, scanned, skipped = find_notes(tmp_path / "nowhere")
    assert (found, scanned, skipped) == ([], 0, [])


# --- the report ------------------------------------------------------------

def test_the_report_groups_by_dataset(tmp_path):
    a_croissant(tmp_path / "a.json", name="AURORA_DJC",
                fields=[("X", "x. NOTE: one."), ("Y", "y. NOTE: two.")])
    a_croissant(tmp_path / "b.json", name="POLARIS_DJC",
                fields=[("Z", "z. NOTE: three.")])

    lines = report(*find_notes(tmp_path))
    text = "\n".join(lines)

    assert "3 note(s) across 2 dataset(s)" in lines[0]
    assert text.index("AURORA_DJC") < text.index("POLARIS_DJC")     # sorted
    assert "records / X" in text and "one." in text


def test_the_report_says_when_there_is_nothing_rather_than_nothing(tmp_path):
    a_croissant(tmp_path / "a.json", fields=[("X", "x, with no note.")])
    lines = report(*find_notes(tmp_path))
    assert "No notes in any of them" in lines[0]

    assert "No Croissant files found" in report(*find_notes(tmp_path / "nowhere"))[0]


def test_the_report_marks_a_dataset_whose_file_is_unfinished(tmp_path):
    a_croissant(tmp_path / "a.json", parbaked=True, fields=[("X", "x. NOTE: one.")])
    assert any("still par-baked" in line for line in report(*find_notes(tmp_path)))


def test_grouping_keeps_every_note(tmp_path):
    a_croissant(tmp_path / "a.json", name="ONE",
                fields=[("X", "x. NOTE: a."), ("Y", "y. NOTE: b.")])
    found, _, _ = find_notes(tmp_path)
    grouped = by_dataset(found)
    assert sum(len(notes) for notes in grouped.values()) == len(found) == 2
