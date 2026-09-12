#!/usr/bin/env python3
"""What a par-baked Croissant is still missing, and where each answer goes.

Two halves. A catalogue of the dataset-level questions, which is data rather
than logic and is meant to be edited. And the code that looks at a document,
works out which questions it still needs answers to, and writes an answer back
into the right place.

The measurements parbake left in the file are shown alongside each field
question. Without them "what does exit_code hold?" is a memory test.
"""

from dataclasses import dataclass, field as dataclass_field

from parbake_link import BANNER, FIELD_PLACEHOLDER

# The types Croissant understands. Offered in a fixed order, with no suggested
# answer: the measurements are shown alongside, and the choice is the person's.
DATA_TYPE_CHOICES = [
    "sc:Text",
    "sc:Integer",
    "sc:Float",
    "sc:Boolean",
    "sc:Date",
    "sc:DateTime",
    "sc:URL",
]


# --- the questions --------------------------------------------------------

@dataclass
class Question:
    """One thing a person has to answer.

    `where` is a path into the document, used to put the answer back:
    ("citeAs",) or ("recordSet", 0, "field", 3, "dataType").
    """
    key: str                        # short label, e.g. "citeAs" or "exit_code / dataType"
    prompt: str                     # the question itself
    where: tuple
    help_text: str = ""
    evidence: str = ""
    kind: str = "text"              # text | lines | boolean | choice
    choices: list = dataclass_field(default_factory=list)


# Dataset-level questions, in the order a person would sensibly answer them.
# Each is only asked when the document does not already have an answer.
DATASET_QUESTIONS = [
    {
        "key": "description",
        "prompt": "Describe the dataset in two or three sentences",
        "help_text": "What is one row? Which machine? Are users or projects "
                     "anonymised? This replaces the par-baked warning text.",
    },
    {
        "key": "version",
        "prompt": "Version",
        "help_text": "For example 1.0. The version of this documented dataset, "
                     "not of the software that made it.",
    },
    {
        "key": "url",
        "prompt": "Landing page URL",
        "help_text": "Where the dataset is published or described. If it is not "
                     "published, say so here rather than inventing a URL.",
    },
    {
        "key": "datePublished",
        "prompt": "Date published (YYYY-MM-DD)",
        "help_text": "Leave blank and skip if the data is not released.",
    },
    {
        "key": "creator",
        "prompt": "Creator organisation",
        "help_text": "The organisation that produced the data, e.g. "
                     "Argonne National Laboratory.",
    },
    {
        "key": "publisher",
        "prompt": "Publisher organisation",
        "help_text": "Often the same as the creator.",
    },
    {
        "key": "license",
        "prompt": "Licence or attribution requirement",
        "help_text": "The terms under which the data may be used. For ALCF data "
                     "this is usually the required attribution statement.",
    },
    {
        "key": "citeAs",
        "prompt": "Required citation",
        "help_text": "The exact statement anyone using this data must include.",
    },
    {
        "key": "keywords",
        "prompt": "Keywords, one per line",
        "kind": "lines",
        "help_text": "Machine, vendor, scheduler, metric family, record type. "
                     "These are what someone searching the corpus will match on.",
    },
    {
        "key": "rai:dataCollectionType",
        "prompt": "How was this data collected?",
        "help_text": "For example: direct measurement from scheduler accounting "
                     "records. Say whether any of it is synthetic.",
    },
    {
        "key": "rai:hasSyntheticData",
        "prompt": "Does this dataset contain synthetic data?",
        "kind": "boolean",
    },
    {
        "key": "rai:dataLimitations",
        "prompt": "Known limitations, one per line",
        "kind": "lines",
        "help_text": "The most valuable part of the whole file. Sentinel values, "
                     "columns that are always the same, codes that do not mean "
                     "what they look like, filename dates that disagree with the "
                     "contents, joins that do not work. Anything that would make "
                     "someone compute the wrong number.",
    },
    {
        "key": "rai:dataBiases",
        "prompt": "Known biases, one per line",
        "kind": "lines",
        "help_text": "Populations recorded differently by construction. For "
                     "example, if some records always report success regardless "
                     "of outcome, any success rate is biased upward.",
    },
    {
        "key": "rai:personalSensitiveInformation",
        "prompt": "Personal or sensitive information, one per line",
        "kind": "lines",
        "help_text": "What identifies people, and what has been done about it. "
                     "Say whether user and project columns are real or surrogate.",
    },
    {
        "key": "rai:dataUseCases",
        "prompt": "Intended use cases, one per line",
        "kind": "lines",
        "help_text": "What analyses this data was meant to support. This is what "
                     "someone matches against when looking for a dataset.",
    },
    {
        "key": "rai:dataSocialImpact",
        "prompt": "Social impact",
        "help_text": "Write 'None known.' if that is the honest answer.",
    },
]


def is_parbaked(document):
    """Has this document come from parbake and not yet been finished?"""
    conforms = document.get("conformsTo")
    conforms = conforms if isinstance(conforms, list) else [conforms]
    return any(isinstance(c, str) and "PARBAKED" in c for c in conforms)


def _is_unanswered(value):
    """Is this value missing, or still par-baked boilerplate?"""
    if value is None or value == "" or value == []:
        return True
    if isinstance(value, str):
        return BANNER in value or FIELD_PLACEHOLDER in value
    return False


def find_questions(document):
    """Every question this document still needs an answer to, in asking order.

    Dataset-level questions first, because the answers give context for the
    field-by-field ones that follow.
    """
    questions = []

    for spec in DATASET_QUESTIONS:
        if _is_unanswered(document.get(spec["key"])):
            questions.append(Question(
                key=spec["key"],
                prompt=spec["prompt"],
                where=(spec["key"],),
                help_text=spec.get("help_text", ""),
                kind=spec.get("kind", "text"),
            ))

    # The file's own description, which parbake fills with the warning.
    for index, file_object in enumerate(document.get("distribution", [])):
        if _is_unanswered(file_object.get("description")):
            questions.append(Question(
                key=f"file / {file_object.get('name', index)}",
                prompt=f"Describe the file {file_object.get('name', '')}",
                where=("distribution", index, "description"),
                help_text="What this file is, and how it relates to any siblings.",
            ))

    measurements = document.get("_parbake_measurements", {})

    for record_index, record_set in enumerate(document.get("recordSet", [])):
        if _is_unanswered(record_set.get("description")):
            questions.append(Question(
                key=f"recordSet / {record_set.get('name', record_index)}",
                prompt="What is one record in this set?",
                where=("recordSet", record_index, "description"),
                help_text="For example: one row per job, or one row per sensor reading.",
            ))

        for field_index, field in enumerate(record_set.get("field", [])):
            name = field.get("name", str(field_index))
            evidence = describe_measurements(measurements.get(name))

            if _is_unanswered(field.get("description")):
                questions.append(Question(
                    key=f"{name} / description",
                    prompt=f"What does {name} hold?",
                    where=("recordSet", record_index, "field", field_index, "description"),
                    help_text="What the values mean, and the unit if there is one. "
                              "If a value is a sentinel rather than a measurement, "
                              "say so here.",
                    evidence=evidence,
                ))

            if _is_unanswered(field.get("dataType")):
                questions.append(Question(
                    key=f"{name} / dataType",
                    prompt=f"What type is {name}?",
                    where=("recordSet", record_index, "field", field_index, "dataType"),
                    kind="choice",
                    choices=DATA_TYPE_CHOICES,
                    help_text="The type of the values as they should be read, not "
                              "how they happen to be stored in the CSV.",
                    evidence=evidence,
                ))

    return questions


def describe_measurements(measured):
    """Turn one column's measurements into something a person can read.

    This is what makes the questions answerable. Without it, "what does
    exit_code hold?" is a memory test.
    """
    if not measured:
        return ""

    rows = measured.get("rows_seen", 0)
    lines = []

    counts = [f"{rows:,} rows"]
    if measured.get("empty_count"):
        counts.append(f"{measured['empty_count']:,} empty")
    if measured.get("null_like_text_count"):
        counts.append(f"{measured['null_like_text_count']:,} NA-ish text")
    lines.append(", ".join(counts))

    distinct = measured.get("distinct_count")
    if distinct is not None:
        floor = "+ (at least)" if measured.get("distinct_count_is_at_least") else ""
        lines.append(f"{distinct:,}{floor} distinct values")

    common = measured.get("most_common_values") or []
    if common:
        shown = ", ".join(
            f"{entry['value']!r} ({entry['count'] / rows:.1%})" if rows else repr(entry["value"])
            for entry in common[:5]
        )
        lines.append(f"most common: {shown}")

    if measured.get("is_all_empty"):
        lines.append("EVERY ROW IS EMPTY -- this column holds nothing")
    elif measured.get("holds_one_value"):
        lines.append("ONE VALUE COVERS ~ALL ROWS -- constant, unused, or a sentinel?")

    numbers = measured.get("number_count", 0)
    not_numbers = measured.get("not_a_number_count", 0)
    if numbers:
        smallest, largest = measured.get("smallest_number"), measured.get("largest_number")
        note = f"{numbers:,} values are numbers, from {smallest} to {largest}"
        if not_numbers:
            note += f"; {not_numbers:,} are not numbers"
        lines.append(note)

    shortest, longest = measured.get("shortest_value_length"), measured.get("longest_value_length")
    if shortest is not None:
        lines.append(f"value lengths {shortest} to {longest}")

    return "\n".join(lines)


# --- putting answers back -------------------------------------------------

def set_answer(document, where, value):
    """Write a value into the document at `where`, creating nothing implicitly."""
    target = document
    for step in where[:-1]:
        target = target[step]
    target[where[-1]] = value


def wrap_answer(key, value):
    """Some Croissant properties want an object rather than a bare string."""
    if key in ("creator", "publisher"):
        return {"@type": "sc:Organization", "name": value}
    if key == "license":
        return {"@type": "sc:CreativeWork", "text": value}
    return value



# --- the one gap a person should not have to fill by hand ------------------

