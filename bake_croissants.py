#!/usr/bin/env python3
"""Finish a par-baked Croissant file by asking a person the questions it cannot answer.

parbake measures a CSV and writes a par-baked Croissant: the file's identity, its
column names in order, and measurements. It deliberately leaves out everything
that is a judgement -- what a field means, its type, the licence, the caveats --
and it deliberately fails validation so nobody mistakes it for finished.

This is the other half. It reads a par-baked file, works out what is missing,
and asks about each gap in turn, showing the measurements alongside so the
question is answerable. It writes the answers to a NEW file and never touches
the par-baked one.

    <name>.parbaked.json   ->   <name>.baked.json

The name `.croissant.json` is still not used. A baked file is one a person has
worked through; whether it is correct and complete is a separate question, and
validation is a later job.

Usage:
    python3 bake_croissants.py ../parbake/parbake_output
    python3 bake_croissants.py <that directory>/parbaked_croissants
    python3 bake_croissants.py <one file>.parbaked.json
    python3 bake_croissants.py <directory> --list      # what is missing, ask nothing

At any prompt: enter a value, "?" for what the question means, blank to skip it
for now, or "q" to save and stop.
"""

import argparse
import hashlib
import json
from pathlib import Path

# parbake sits alongside this directory and owns the markers that say a file is
# par-baked. Importing them keeps one definition rather than two that drift.
from parbake_link import CROISSANT_SUBDIRECTORY
from questions import (
    find_questions,
    is_parbaked,
    set_answer,
    wrap_answer,
)

# What a finished file conforms to. Only written once nothing is left unanswered.
FINISHED_CONFORMS_TO = [
    "http://mlcommons.org/croissant/1.0",
    "http://mlcommons.org/croissant/RAI/1.0",
]

# Shown once before the first question, so the controls are on screen rather
# than in the --help nobody reads first.
PROMPT_HELP = """At any prompt:
    a value    record it
    ?          show what the question means, and more evidence
    <enter>    skip this one for now, come back later
    q          save and stop"""

OUTPUT_SUFFIX = ".baked.json"
PARBAKED_SUFFIX = ".parbaked.json"


# --- the one gap a person should not have to fill by hand ------------------

def sha256_of(path):
    """Checksum a file of any size.

    hashlib.file_digest reads in blocks itself, so this does not need a loop.
    It arrived in Python 3.11, which is why that is the minimum version.
    """
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def source_file_for(document):
    """Where the described data lives, according to the par-baked provenance."""
    recorded = (document.get("_parbake") or {}).get("source_file")
    return Path(recorded) if recorded else None


def add_checksum_if_missing(document, say=None):
    """Give the file a sha256, computing it from the data rather than asking.

    A Croissant FileObject needs md5 or sha256 -- without one the file cannot
    validate no matter how many questions get answered. It is also the one gap
    here that is purely mechanical, so asking a person to type sixty-four hex
    characters would be a waste of their attention and a good way to introduce
    a typo.

    Returns one of: "added", "already there", "source missing", "no source recorded".
    """
    file_objects = document.get("distribution") or []
    if not file_objects:
        return "no source recorded"

    file_object = file_objects[0]
    if file_object.get("sha256") or file_object.get("md5"):
        return "already there"

    source = source_file_for(document)
    if source is None:
        return "no source recorded"
    if not source.is_file():
        if say:
            say(f"  No checksum: {source} is not where the par-baked file said it was.")
            say("  Put the data back and run again, or add a sha256 by hand.")
        return "source missing"

    if say:
        say(f"  Checksumming {source.name} ...")
    file_object["sha256"] = sha256_of(source)
    if say:
        say(f"  sha256 {file_object['sha256']}")
    return "added"


# --- asking ---------------------------------------------------------------

class StopBaking(Exception):
    """Raised when the person asks to stop. Everything answered so far is kept."""


def ask_question(question, ask, say, number, total):
    """Ask one question. Returns the answer, or None if it was skipped.

    Raises StopBaking if the person wants to stop.
    """
    say("")
    say(f"  [{number}/{total}] {question.key}")
    if question.evidence:
        for line in question.evidence.splitlines():
            say(f"        {line}")

    while True:
        if question.kind == "choice":
            for index, choice in enumerate(question.choices, start=1):
                say(f"        {index}. {choice}")

        suffix = {
            "lines": "  (one per line, blank line to finish)",
            "boolean": "  (y/n)",
            "choice": "  (number, or type the value)",
        }.get(question.kind, "")

        raw = ask(f"  {question.prompt}{suffix}\n  > ")

        if raw is None:
            raise StopBaking()
        answer = raw.strip()

        if answer.lower() == "q":
            raise StopBaking()
        if answer == "?":
            say(f"        {question.help_text or 'No further guidance for this one.'}")
            continue
        if answer == "":
            if question.kind == "lines":
                return None         # a blank first line means skip, same as elsewhere
            return None

        if question.kind == "boolean":
            if answer.lower() in ("y", "yes", "true"):
                return True
            if answer.lower() in ("n", "no", "false"):
                return False
            say("        Please answer y or n, or press enter to skip.")
            continue

        if question.kind == "choice":
            if answer.isdigit() and 1 <= int(answer) <= len(question.choices):
                return question.choices[int(answer) - 1]
            if answer in question.choices:
                return answer
            say(f"        Not one of the choices. Pick 1-{len(question.choices)}, "
                "or press enter to skip.")
            continue

        if question.kind == "lines":
            collected = [answer]
            while True:
                more = ask("  > ")
                if more is None:
                    raise StopBaking()
                more = more.strip()
                if not more:
                    break
                # "q" has to mean stop here too. Without this it is collected as
                # another list entry, and a caller that keeps returning it -- a
                # scripted test, or a closed stdin -- loops forever.
                if more.lower() == "q":
                    raise StopBaking()
                collected.append(more)
            return collected

        return answer


# --- baking one document --------------------------------------------------

def bake_document(document, ask, say, save=None):
    """Ask about every gap in `document`, filling in what gets answered.

    `save` is called after each answer, so stopping halfway loses nothing.
    Returns (answered_count, skipped_count, stopped_early).
    """
    questions = find_questions(document)
    answered = skipped = 0
    stopped = False

    for number, question in enumerate(questions, start=1):
        try:
            answer = ask_question(question, ask, say, number, len(questions))
        except StopBaking:
            stopped = True
            break

        if answer is None:
            skipped += 1
            continue

        set_answer(document, question.where, wrap_answer(question.key, answer))
        answered += 1
        if save is not None:
            save(document)

    return answered, skipped, stopped


def remaining_questions(document):
    """How many gaps are left. Zero means every question has an answer."""
    return len(find_questions(document))


def finish_if_complete(document, say=None):
    """Swap the par-baked conformsTo for a real one, but only when nothing is left.

    The par-baked conformsTo is what stops the file passing validation. Removing
    it while gaps remain would throw away the only thing preventing an unfinished
    file from looking finished, so it only goes when there is nothing left to ask.
    """
    if remaining_questions(document):
        return False
    if is_parbaked(document):
        document["conformsTo"] = list(FINISHED_CONFORMS_TO)
        document.pop("_parbake", None)
        if say:
            say("\n  Nothing left unanswered. conformsTo now names a real Croissant "
                "version, so this file can be validated.")
    return True


# --- files and directories ------------------------------------------------

def output_path_for(parbaked_path, output_directory):
    """Where the baked copy goes. Never over the par-baked original."""
    name = parbaked_path.name
    if name.endswith(PARBAKED_SUFFIX):
        name = name[: -len(PARBAKED_SUFFIX)]
    else:
        name = parbaked_path.stem
    return Path(output_directory) / f"{name}{OUTPUT_SUFFIX}"


def load_starting_point(parbaked_path, output_path, say):
    """Carry on from a part-baked file if one exists, otherwise start fresh.

    Half-finished work is the normal state here: a file with 66 columns has over
    a hundred questions, and nobody answers those in one sitting.
    """
    if output_path.is_file():
        if say:
            say(f"  Carrying on from {output_path.name}")
        return json.loads(output_path.read_text())
    return json.loads(parbaked_path.read_text())


def bake_file(parbaked_path, output_directory, ask=input, say=print):
    """Work through one par-baked file. Returns a small summary dictionary."""
    parbaked_path = Path(parbaked_path)
    output_path = output_path_for(parbaked_path, output_directory)
    Path(output_directory).mkdir(parents=True, exist_ok=True)

    document = load_starting_point(parbaked_path, output_path, say)
    checksum_state = add_checksum_if_missing(document, say)
    outstanding = remaining_questions(document)

    say("")
    say("=" * 72)
    say(f"  {parbaked_path.name}")
    say(f"  {outstanding} question(s) outstanding  ->  {output_path.name}")
    say("=" * 72)

    def save(current):
        output_path.write_text(json.dumps(current, indent=2), encoding="utf-8")

    if outstanding == 0:
        say("  Nothing left to ask.")
        finish_if_complete(document, say)
        save(document)
        return {"file": parbaked_path, "output": output_path, "answered": 0,
                "skipped": 0, "remaining": 0, "stopped": False,
                "checksum": checksum_state}

    answered, skipped, stopped = bake_document(document, ask, say, save)
    finish_if_complete(document, say)
    save(document)

    left = remaining_questions(document)
    say("")
    if checksum_state == "source missing":
        say("  NOTE: no checksum -- the source file was not where it was expected.")
    say(f"  {answered} answered, {skipped} skipped, {left} still outstanding")
    say(f"  Written to {output_path}")

    return {"file": parbaked_path, "output": output_path, "answered": answered,
            "skipped": skipped, "remaining": left, "stopped": stopped,
            "checksum": checksum_state}


def find_parbaked_files(target):
    """Every par-baked file under `target`, or the single file given.

    parbake sorts its output by kind, so the Croissant files live in a
    subdirectory of the output root:

        <out>/DIRECTORY_DOCUMENTATION.txt
        <out>/parbaked_croissants/*.parbaked.json
        <out>/parbaked_markdown/*.parbaked.md
        <out>/parbaked_txt/*.txt

    Both of those are reasonable things to point at, so both work: the output
    root, or the croissants directory inside it. Anyone who has just run parbake
    has the output root in their hand, and should not have to know the layout.
    """
    target = Path(target)
    if target.is_file():
        return [target]
    if not target.is_dir():
        return []

    here = sorted(target.glob(f"*{PARBAKED_SUFFIX}"))
    if here:
        return here

    inside = target / CROISSANT_SUBDIRECTORY
    if inside.is_dir():
        return sorted(inside.glob(f"*{PARBAKED_SUFFIX}"))
    return []


def report_gaps(parbaked_files, output_directory, say=print):
    """Say what is missing without asking anything. Used by --list."""
    for parbaked_path in parbaked_files:
        output_path = output_path_for(parbaked_path, output_directory)
        document = load_starting_point(parbaked_path, output_path, say=None)
        questions = find_questions(document)

        say("")
        say(f"  {parbaked_path.name}: {len(questions)} outstanding")
        dataset_level = [q for q in questions if len(q.where) == 1]
        # A field question's `where` reaches into recordSet -> field, which is
        # what distinguishes it from the file and record-set descriptions.
        field_level = [q for q in questions if len(q.where) > 3]
        other = [q for q in questions
                 if q not in dataset_level and q not in field_level]

        if dataset_level:
            say(f"      dataset: {', '.join(q.key for q in dataset_level)}")
        if other:
            say(f"      also:    {', '.join(q.key for q in other)}")
        if field_level:
            columns = {q.key.split(" / ")[0] for q in field_level}
            say(f"      fields:  {len(field_level)} question(s) across "
                f"{len(columns)} column(s)")


def bake_directory(target, output_directory, ask=input, say=print):
    """Work through every par-baked file under `target`."""
    parbaked_files = find_parbaked_files(target)
    if not parbaked_files:
        say(f"  No {PARBAKED_SUFFIX} files found in {target}")
        return []

    found_in = parbaked_files[0].parent
    if found_in != Path(target):
        say(f"  Found {len(parbaked_files)} par-baked file(s) in {found_in}")

    results = []
    for parbaked_path in parbaked_files:
        outcome = bake_file(parbaked_path, output_directory, ask, say)
        results.append(outcome)
        if outcome["stopped"]:
            say("\n  Stopped. Run again to carry on where you left off.")
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "target",
        help="a parbake output directory, its parbaked_croissants/ folder, "
             "or a single .parbaked.json file",
    )
    parser.add_argument(
        "--out", default="baked",
        help="where the baked copies go (default: ./baked)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="say what is missing and ask nothing",
    )
    arguments = parser.parse_args()

    target = Path(arguments.target)
    if not target.exists():
        raise SystemExit(f"No such file or directory: {target}")

    parbaked_files = find_parbaked_files(target)
    if not parbaked_files:
        raise SystemExit(
            f"No {PARBAKED_SUFFIX} files found in {target}.\n"
            f"Point at a parbake output directory, its {CROISSANT_SUBDIRECTORY}/ "
            "folder, or a single .parbaked.json file."
        )

    if arguments.list:
        report_gaps(parbaked_files, arguments.out)
        return

    print(PROMPT_HELP)
    try:
        bake_directory(target, arguments.out)
    except KeyboardInterrupt:
        print("\n\n  Interrupted. Everything answered so far has been saved.")


if __name__ == "__main__":
    main()
