#!/usr/bin/env python3
"""Moving a file to another machine, and copying answers between datasets.

Two corrections that the question-by-question screens cannot make efficiently:

    change_machine   a file was confirmed as the wrong machine. It moves to the
                     right machine's dataset and its answers follow it.
    plan_copy        another dataset already answers most of these columns.
    apply_copy       Those answers are copied in, filling gaps; where this
                     dataset already has a different confirmed answer, the
                     person chooses, column by column.

Both work on datasets because answers belong to datasets: a column description
is recorded per (dataset, column), and a dataset is <MACHINE>_<KIND>.

## What a copy carries

For each column: description, type, unit, note, and the link to a decoding
table. Dataset-level answers too: licence, citation, keywords and the
responsible-AI blocks.

Only answers confirmed in the source are copied. A seeded answer nobody has
confirmed would otherwise become confirmed by being copied, which is the one
thing confirmation exists to prevent. Copied answers are confirmed in the
target and recorded as "copied from <source dataset>".

Decoding tables belong to a machine. A link is copied to the target machine's
table of the same name; when the target machine has no such table, the link is
left out and listed.
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import select

from models import Dataset, DatasetAnswer, Enumeration, FieldDoc, now
from outstanding import dataset_for, date_to_iteration, refresh_state

PARTS = ("description", "data_type", "unit", "note")
PART_LABELS = {"description": "description", "data_type": "type", "unit": "unit",
               "note": "note", "table": "decoding table", "answer": "answer"}


def _has_answer(doc):
    return bool(doc and (doc.description or doc.data_type))


def _values(doc, table_name):
    values = {part: getattr(doc, part) or None for part in PARTS}
    values["table"] = table_name
    return values


def _table_name(session, doc):
    if doc is None or doc.enumeration_id is None:
        return None
    table = session.get(Enumeration, doc.enumeration_id)
    return table.name if table else None


def _table_on(session, machine_id, name):
    """The machine's decoding table with this name, or None."""
    if name is None:
        return None
    return session.scalar(select(Enumeration).where(
        Enumeration.machine_id == machine_id, Enumeration.name == name))


def columns_of(dataset):
    """The dataset's column names, in file order, across all its files.

    A dataset with no files yet -- one only seeded from finished work -- has no
    measured columns, so its recorded descriptions stand in for them.
    """
    names = {}
    for source_file in dataset.files:
        for column in source_file.fields:
            names.setdefault(column.name, None)
    if not names:
        for doc in dataset.field_docs:
            names.setdefault(doc.field_name, None)
    return list(names)


def describe_values(values):
    """A set of values as lines for a screen, one per part that has something."""
    lines = []
    for part in PARTS + ("table", "answer"):
        if part not in values:
            continue
        value = values[part]
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{PART_LABELS[part]}: {value if value not in (None, '') else '(none)'}")
    return lines


# --- copying ---------------------------------------------------------------

@dataclass
class Difference:
    """A column or dataset answer the target has already confirmed differently."""

    kind: str                           # "column" or "answer"
    name: str
    current: dict
    incoming: dict
    take_incoming: Optional[bool] = None


@dataclass
class CopyPlan:
    """What copying one dataset's answers into another would do, before it does it."""

    source: Dataset
    target: Dataset
    fills: List[str] = field(default_factory=list)
    same: List[str] = field(default_factory=list)
    differences: List[Difference] = field(default_factory=list)
    not_confirmed: List[str] = field(default_factory=list)
    unanswered_in_source: int = 0
    answer_fills: List[str] = field(default_factory=list)
    answer_same: List[str] = field(default_factory=list)
    links_left_out: List[str] = field(default_factory=list)
    incoming: Dict[str, dict] = field(default_factory=dict)
    tables: Dict[str, Optional[int]] = field(default_factory=dict)

    @property
    def column_differences(self):
        return [each for each in self.differences if each.kind == "column"]

    @property
    def answer_differences(self):
        return [each for each in self.differences if each.kind == "answer"]

    def lines(self):
        columns = len(self.fills) + len(self.same) + len(self.column_differences)
        out = [f"From {self.source.name} into {self.target.name}",
               "",
               f"  columns answered in both      {columns}",
               f"    will be filled              {len(self.fills)}",
               f"    already the same            {len(self.same)}",
               f"    differ, you choose          {len(self.column_differences)}"]
        if self.not_confirmed:
            out.append(f"  not copied, unconfirmed in {self.source.name}: "
                       f"{len(self.not_confirmed)}")
        if self.unanswered_in_source:
            out.append(f"  not answered in {self.source.name}: {self.unanswered_in_source}")
        out += ["",
                f"  dataset-level answers: {len(self.answer_fills)} to fill, "
                f"{len(self.answer_same)} the same, "
                f"{len(self.answer_differences)} differ"]
        if self.links_left_out:
            out.append(f"  decoding-table links left out, no table of that name on "
                       f"{self.target.machine.name}: {', '.join(self.links_left_out)}")
        return out


def plan_copy(session, source: Dataset, target: Dataset) -> CopyPlan:
    """Work out what copying source's answers into target would change. Writes nothing."""
    plan = CopyPlan(source=source, target=target)
    source_docs = {doc.field_name: doc for doc in source.field_docs}
    target_docs = {doc.field_name: doc for doc in target.field_docs}

    for name in columns_of(target):
        incoming_doc = source_docs.get(name)
        if not _has_answer(incoming_doc):
            plan.unanswered_in_source += 1
            continue
        if not incoming_doc.is_confirmed:
            plan.not_confirmed.append(name)
            continue

        table_name = _table_name(session, incoming_doc)
        table = _table_on(session, target.machine_id, table_name)
        if table_name and table is None:
            plan.links_left_out.append(name)
        plan.tables[name] = table.id if table else None
        incoming = _values(incoming_doc, table.name if table else None)
        plan.incoming[name] = incoming

        current_doc = target_docs.get(name)
        if current_doc is None or not (current_doc.is_confirmed and current_doc.is_complete):
            plan.fills.append(name)
        elif _values(current_doc, _table_name(session, current_doc)) == incoming:
            plan.same.append(name)
        else:
            plan.differences.append(Difference(
                "column", name, _values(current_doc, _table_name(session, current_doc)),
                incoming))

    target_answers = {row.key: row for row in target.answers}
    for row in source.answers:
        if not row.is_confirmed:
            continue
        current = target_answers.get(row.key)
        if current is None or not current.is_confirmed:
            plan.answer_fills.append(row.key)
        elif current.answer == row.answer:
            plan.answer_same.append(row.key)
        else:
            plan.differences.append(Difference(
                "answer", row.key, {"answer": current.answer}, {"answer": row.answer}))
    return plan


def apply_copy(session, plan: CopyPlan) -> List[str]:
    """Write a plan. Differences are applied only where take_incoming is True.

    Filling keeps whatever the target already had for a part the source leaves
    empty. Taking the incoming answer for a difference replaces every part, so
    the column ends up exactly as the source has it.

    Returns lines saying what was done.
    """
    recorded_by = f"copied from {plan.source.name}"
    stamp = now()
    target_docs = {doc.field_name: doc for doc in plan.target.field_docs}

    def write(name, replace):
        doc = target_docs.get(name)
        if doc is None:
            doc = FieldDoc(dataset_id=plan.target.id, field_name=name)
            session.add(doc)
            target_docs[name] = doc
        incoming = plan.incoming[name]
        for part in PARTS:
            if replace or incoming[part] is not None:
                setattr(doc, part, incoming[part])
        if replace or plan.tables[name] is not None:
            doc.enumeration_id = plan.tables[name]
        doc.is_confirmed = True
        doc.recorded_by = recorded_by
        doc.recorded_at = stamp

    for name in plan.fills:
        write(name, replace=False)
    taken_columns = [each.name for each in plan.column_differences if each.take_incoming]
    for name in taken_columns:
        write(name, replace=True)

    source_answers = {row.key: row for row in plan.source.answers}
    target_answers = {row.key: row for row in plan.target.answers}
    taken_answers = [each.name for each in plan.answer_differences if each.take_incoming]
    for key in plan.answer_fills + taken_answers:
        row = target_answers.get(key)
        if row is None:
            row = DatasetAnswer(dataset_id=plan.target.id, key=key,
                                answer=source_answers[key].answer)
            session.add(row)
        row.answer = source_answers[key].answer
        row.is_confirmed = True
        row.recorded_by = recorded_by
        row.recorded_at = stamp

    session.flush()
    for source_file in plan.target.files:
        refresh_state(session, source_file)
    session.commit()

    kept_columns = len(plan.column_differences) - len(taken_columns)
    kept_answers = len(plan.answer_differences) - len(taken_answers)
    lines = [f"Copied from {plan.source.name} into {plan.target.name}",
             "",
             f"  columns filled                {len(plan.fills)}",
             f"  columns replaced, you chose   {len(taken_columns)}",
             f"  columns kept as they were     {kept_columns + len(plan.same)}",
             f"  dataset-level answers written {len(plan.answer_fills) + len(taken_answers)}",
             f"  dataset-level answers kept    {kept_answers + len(plan.answer_same)}"]
    if plan.links_left_out:
        lines.append(f"  decoding-table links left out: {', '.join(plan.links_left_out)}")
    return lines


def copy_candidates(session, target: Dataset):
    """Every other dataset, with how many of target's columns it has confirmed answers for.

    Most useful first, so the dataset worth copying from is at the top.
    """
    wanted = set(columns_of(target))
    found = []
    for dataset in session.scalars(select(Dataset).where(Dataset.id != target.id)):
        confirmed = [doc for doc in dataset.field_docs
                     if doc.is_confirmed and _has_answer(doc)]
        matching = sum(1 for doc in confirmed if doc.field_name in wanted)
        found.append((dataset, matching, len(confirmed)))
    found.sort(key=lambda row: (-row[1], -row[2], row[0].name))
    return found


# --- changing the machine --------------------------------------------------

@dataclass
class MachineChange:
    """What moving a file to another machine did."""

    file_name: str
    old_dataset: Optional[str]
    new_dataset: str
    moved: bool = False                 # answers moved, rather than copied
    columns: int = 0
    answers: int = 0
    kept_differences: List[str] = field(default_factory=list)
    links_dropped: List[str] = field(default_factory=list)
    removed_dataset: Optional[str] = None

    def lines(self):
        if self.old_dataset == self.new_dataset:
            return [f"{self.file_name} is already in {self.new_dataset}. Nothing changed."]
        verb = "moved" if self.moved else "copied"
        out = [f"{self.file_name}",
               f"  {self.old_dataset or 'no machine'} -> {self.new_dataset}",
               "",
               f"  column answers {verb}         {self.columns}",
               f"  dataset-level answers {verb}  {self.answers}"]
        if self.kept_differences:
            out.append(f"  kept {self.new_dataset}'s own confirmed answer, which "
                       f"differs, for: {', '.join(self.kept_differences)}")
        if self.links_dropped:
            out.append(f"  decoding-table links dropped, no table of that name on the "
                       f"new machine: {', '.join(self.links_dropped)}")
        if self.removed_dataset:
            out.append(f"  {self.removed_dataset} had no other files and was removed")
        elif self.old_dataset and not self.moved:
            out.append(f"  {self.old_dataset} still has other files and keeps its answers")
        return out


def change_machine(session, source_file, machine_name) -> MachineChange:
    """Move a file to another machine's dataset, and carry its answers across.

    The answers follow the file. When it was the only file in its old dataset,
    they move and the emptied dataset is removed. When other files remain there,
    they are copied, so those files keep theirs.

    Where the new dataset already has a confirmed, complete answer for a column
    or a dataset-level question, that answer is kept, and the column is listed if
    the two differ. Anything else there is replaced by the file's answer.
    """
    old = source_file.dataset
    machine, new = dataset_for(session, source_file, machine_name)
    change = MachineChange(file_name=source_file.name,
                           old_dataset=old.name if old else None,
                           new_dataset=new.name)
    if old is not None and old.id == new.id:
        return change

    # Through the relationships, not the id columns: the old dataset's lists of
    # files and answers are loaded, and a flush would write the old id back.
    source_file.dataset = new
    date_to_iteration(source_file, machine)
    session.flush()

    if old is None:
        refresh_state(session, source_file)
        session.commit()
        return change

    remaining = [each for each in old.files if each.id != source_file.id]
    change.moved = not remaining
    new_docs = {doc.field_name: doc for doc in new.field_docs}

    for doc in list(old.field_docs):
        existing = new_docs.get(doc.field_name)
        old_table = _table_name(session, doc)
        if existing is not None and existing.is_confirmed and existing.is_complete:
            if (_values(existing, _table_name(session, existing))
                    != _values(doc, old_table)):
                change.kept_differences.append(doc.field_name)
            continue
        if existing is not None:
            session.delete(existing)
            session.flush()

        table = _table_on(session, machine.id, old_table)
        if old_table and table is None:
            change.links_dropped.append(doc.field_name)
        if change.moved:
            doc.dataset = new
            doc.enumeration = table
        else:
            session.add(FieldDoc(
                dataset_id=new.id, field_name=doc.field_name,
                enumeration_id=table.id if table else None,
                is_confirmed=doc.is_confirmed, recorded_by=doc.recorded_by,
                recorded_at=doc.recorded_at, first_seen_in=doc.first_seen_in,
                **{part: getattr(doc, part) for part in PARTS}))
        change.columns += 1

    new_answers = {row.key: row for row in new.answers}
    for row in list(old.answers):
        existing = new_answers.get(row.key)
        if existing is not None and existing.is_confirmed:
            if existing.answer != row.answer:
                change.kept_differences.append(row.key)
            continue
        if existing is not None:
            session.delete(existing)
            session.flush()
        if change.moved:
            row.dataset = new
        else:
            session.add(DatasetAnswer(
                dataset_id=new.id, key=row.key, answer=row.answer,
                is_confirmed=row.is_confirmed, recorded_by=row.recorded_by,
                recorded_at=row.recorded_at, first_seen_in=row.first_seen_in))
        change.answers += 1

    session.flush()
    if change.moved:
        # Everything worth keeping has been moved off it. Expired first, so the
        # cascade does not act on the moved rows it still holds in memory.
        session.expire(old)
        change.removed_dataset = old.name
        session.delete(old)

    session.flush()
    session.expire(new)
    refresh_state(session, source_file)
    session.commit()
    return change
