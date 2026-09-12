#!/usr/bin/env python3
"""The bakery's interface: pick a file, say which machine it is, describe its columns.

    bakery                          work through whatever is in the database
    bakery --import <parbake out>   read a parbake output directory in first
    bakery --seed <finished files>  read work already done in first
    bakery --bake <directory>       write every finished Croissant out and stop
    bakery --notes <directory>      list every note left for later, and stop
    bakery --database <url>         somewhere other than ./bakery.sqlite

Five screens. Three are the questions in the order they can be answered:

    the files      what is here, and how much of each is done. A queue, so the
                   least finished is at the top
    the machine    which system this file came from, with the evidence for the
                   suggestion and every machine already named
    the columns    one at a time, with what parbake measured beside each, and
                   whatever another dataset already says offered as a default

and two are for looking back at what has been done:

    review         every column of a file, answered or not, with what was
                   recorded. Pick one and go straight back into it
    notes          everything flagged for somebody to come back to

Everything that decides *what* to ask lives in outstanding.py, so this file is
only ever drawing what that decided. It is also why the deciding is tested
without a terminal and this is tested with Textual's headless driver rather than
the two being tangled together.

This is the executable: `python3 bakery.py`. The older file-in, file-out path
still lives in bake_croissants.py and still works.

## Saving happens per answer

Nobody answers 266 questions in one sitting. Every answer is written when it is
given, so closing the terminal loses the question being typed and nothing else.
"""

import argparse
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Select,
    Static,
)
from sqlalchemy import select

from baking import bake_dataset, bake_file
from database import Database, describe
from importing import import_target
from models import Machine, SourceFile
from notes import notes_recorded_in
from outstanding import (
    confirm_machine,
    coverage_of,
    field_work_for,
    files_for_listing,
    record_field_answer,
)
from questions import DATA_TYPE_CHOICES
from seeding import seed_from


# --- the files -------------------------------------------------------------

class FileListScreen(Screen):
    """Every imported file, least finished first.

    A queue rather than an inventory: what still needs doing should not be below
    what is finished.
    """

    BINDINGS = [
        Binding("enter", "work", "Work on this file", priority=True),
        Binding("a", "review", "Review everything set"),
        Binding("b", "bake", "Write it out"),
        Binding("n", "notes", "Notes"),
        Binding("r", "refresh", "Refresh"),
        Binding("m", "machines", "Machines"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="where")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
        yield Static(id="hint")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#files", DataTable)
        table.add_columns("file", "kind", "machine", "columns", "progress", "state")
        self.query_one("#where", Static).update(f"  {describe(self.app.database.url)}")
        self.working_on = None
        self.action_refresh()

    def action_refresh(self):
        """Redraw the queue, keeping the cursor on the same file.

        The queue is sorted least-finished first, so answering something moves
        it down the list. Without holding the cursor to the file rather than to
        the row number, finishing one file silently selects another -- and the
        next keystroke opens the wrong one.
        """
        table = self.query_one("#files", DataTable)
        was_on = self.selected_file()
        table.clear()
        rows = files_for_listing(self.app.session)
        self.rows = rows

        for source_file, coverage in rows:
            machine = source_file.machine
            shown = machine.name if machine else f"? {source_file.machine_suggestion or ''}"
            table.add_row(
                source_file.name, source_file.kind or "", shown,
                str(coverage.columns), coverage.describe(), source_file.state,
                key=str(source_file.id))

        if was_on is not None:
            for position, (source_file, _) in enumerate(rows):
                if source_file.id == was_on.id:
                    table.move_cursor(row=position)
                    break

        if not rows:
            self.query_one("#hint", Static).update(
                "  Nothing imported yet. Start with:  bakery --import <parbake output>")
        else:
            outstanding = sum(1 for _, coverage in rows if coverage.outstanding)
            self.query_one("#hint", Static).update(
                f"  {len(rows)} file(s), {outstanding} with something outstanding. "
                "A '?' machine is a suggestion nobody has confirmed.")

    def selected_file(self):
        """Which file the cursor is on.

        Rows are keyed by the file's id rather than by position, so the queue can
        reorder underneath the cursor as answers land without the wrong file
        being opened.
        """
        table = self.query_one("#files", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        if key is None or key.value is None:
            return None
        return self.app.session.get(SourceFile, int(key.value))

    def action_work(self):
        source_file = self.selected_file()
        if source_file is None:
            return
        # Held rather than looked up again afterwards: the machine screen
        # answers, the queue reorders, and the cursor is no longer the question.
        self.working_on = source_file
        # The machine comes first because everything else hangs off it: naming
        # it is what puts the file in a dataset, and the descriptions belong to
        # the dataset.
        if source_file.machine is None:
            self.app.push_screen(MachineScreen(source_file), self.after_machine)
        else:
            self.app.push_screen(FieldScreen(source_file), lambda _: self.action_refresh())

    def after_machine(self, confirmed):
        """Naming the machine was the first question, not the only one.

        So it goes straight on to the columns rather than dropping back to the
        list, which is what somebody who just said "this is Polaris" was asking
        for.
        """
        self.action_refresh()
        source_file = self.working_on
        if confirmed and source_file is not None and source_file.machine is not None:
            self.app.push_screen(FieldScreen(source_file),
                                 lambda _: self.action_refresh())

    @on(DataTable.RowSelected)
    def row_chosen(self, event):
        self.action_work()

    def action_review(self):
        """Look back at every column of this file, answered or not.

        The work screen only ever shows what is outstanding, which is right
        while there is a queue and useless once there is not: an answer given
        an hour ago disappears from it entirely. This is how you find one again.
        """
        source_file = self.selected_file()
        if source_file is None:
            return
        if source_file.dataset is None:
            self.app.notify("Say which machine this came from first.",
                            severity="warning")
            return
        self.working_on = source_file
        self.app.push_screen(ReviewScreen(source_file),
                             lambda _: self.action_refresh())

    def action_notes(self):
        self.app.push_screen(NotesScreen())

    def action_bake(self):
        """Write this file's Croissant, and its dataset's, into ./baked.

        Both shapes, because they are for different things: the per-file one can
        be checksummed and validated, and the per-dataset one is what gets
        published. Writing an unfinished file is allowed and normal -- it comes
        out with the par-baked marker still on it and a list of what is left, so
        the half-done state is visible rather than being a thing you cannot look
        at until it is perfect.
        """
        source_file = self.selected_file()
        if source_file is None:
            return
        if source_file.dataset is None:
            self.app.notify("Say which machine this came from first.", severity="warning")
            return

        report = bake_file(self.app.session, source_file, self.app.output_directory)
        bake_dataset(self.app.session, source_file.dataset,
                     self.app.output_directory, report)
        self.action_refresh()

        if report.finished:
            self.app.notify(
                f"{len(report.finished)} finished Croissant(s) written to "
                f"{self.app.output_directory}.")
        else:
            outstanding = max((len(reasons) for reasons in report.unfinished.values()),
                              default=0)
            self.app.notify(
                f"Written to {self.app.output_directory}, still par-baked: "
                f"{outstanding} thing(s) outstanding.", severity="warning")

    def action_machines(self):
        self.app.push_screen(MachinesScreen())


# --- the machine -----------------------------------------------------------

class MachineScreen(Screen):
    """Which system did this file come from?

    The one question that cannot be skipped, and the one the import refused to
    answer on its own. The evidence for the suggestion is shown rather than just
    the suggestion, because a filename saying POLARIS over data saying thetagpu
    is exactly what someone needs to see.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Back"),
        Binding("ctrl+s", "confirm", "Confirm", priority=True),
    ]

    def __init__(self, source_file):
        super().__init__()
        self.source_file = source_file

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(f"  {self.source_file.name}", id="title")
            yield Static(id="evidence")
            yield Label("  Machines already named (enter to pick one):")
            yield OptionList(id="machines")
            yield Label("  ...or type a name for a machine not in the list:")
            yield Input(placeholder="polaris", id="typed")
        yield Footer()

    def on_mount(self):
        suggestion = self.source_file.machine_suggestion
        dates = ""
        if self.source_file.covers_from:
            dates = (f"\n  Covers {self.source_file.covers_from} to "
                     f"{self.source_file.covers_to}, read off the filename.")
        self.query_one("#evidence", Static).update(
            f"  {self.source_file.column_count} columns, "
            f"{self.source_file.rows or 'an unrecorded number of'} rows.\n"
            f"  Suggested machine: {suggestion or 'none -- nothing in the file names one'}"
            + dates)

        options = self.query_one("#machines", OptionList)
        self.known = [row for row in self.app.session.scalars(select(Machine).order_by(Machine.name))]
        for machine in self.known:
            profile = machine.current_iteration
            options.add_option(
                f"{machine.name}"
                + (f"  ({profile.vendor}, {len(profile.partitions)} partition(s))"
                   if profile else "  (no hardware recorded yet)"))
        if suggestion:
            self.query_one("#typed", Input).value = suggestion

    @on(OptionList.OptionSelected)
    def pick(self, event):
        self.query_one("#typed", Input).value = self.known[event.option_index].name
        self.action_confirm()

    @on(Input.Submitted)
    def typed(self, event):
        self.action_confirm()

    def action_confirm(self):
        name = self.query_one("#typed", Input).value.strip()
        if not name:
            return
        machine, dataset = confirm_machine(self.app.session, self.source_file, name)
        self.app.notify(f"{self.source_file.name} is {machine.name}; "
                        f"its columns belong to {dataset.name}.")
        self.dismiss(True)

    def action_cancel(self):
        self.dismiss(False)


# --- the columns -----------------------------------------------------------

class FieldScreen(Screen):
    """One column at a time, with the measurements beside it.

    The measurements are what make the question answerable rather than a memory
    test: "what does exit_code hold?" is unanswerable until you can see that it
    has 42 distinct values running from -29 to 271.

    An offer from another dataset arrives pre-filled. Accepting it is one
    keystroke and rejecting it is deleting the text -- which is the difference
    between 66 questions and 66 confirmations.
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save and next", priority=True),
        Binding("ctrl+k", "skip", "Next", priority=True),
        Binding("ctrl+j", "previous", "Previous", priority=True),
        Binding("ctrl+a", "accept", "Accept the offer", priority=True),
        Binding("escape", "back", "Back", priority=True),
    ]

    def __init__(self, source_file, include_settled=False, start_at=None):
        super().__init__()
        self.source_file = source_file
        # Showing settled columns as well turns this from a queue into an
        # editor. It is how a column that was answered an hour ago can be
        # reached at all, since an answered column leaves the queue.
        self.include_settled = include_settled
        self.start_at = start_at
        self.work = []
        self.at = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(id="heading")
            yield Static(id="evidence")
            yield Static(id="offer")
            yield Label("  What does this column hold?")
            yield Input(id="description")
            with Horizontal():
                yield Select([(each, each) for each in DATA_TYPE_CHOICES],
                             prompt="type", id="data_type", allow_blank=True)
                yield Input(placeholder="unit, e.g. Seconds (optional)", id="unit")
            yield Label("  Anything a person should come back to? (optional)")
            yield Input(placeholder="e.g. disagrees with the accounting table by ~2%",
                        id="note")
        yield Footer()

    def on_mount(self):
        self.reload()

    def reload(self):
        self.work = field_work_for(self.app.session, self.source_file,
                                   only_outstanding=not self.include_settled)
        if not self.work:
            self.app.notify(f"{self.source_file.name}: every column is confirmed.")
            self.dismiss(True)
            return
        if self.start_at is not None:
            # Opened at a particular column, from the review screen.
            names = [item.name for item in self.work]
            self.at = names.index(self.start_at) if self.start_at in names else 0
            self.start_at = None
        self.at = min(self.at, len(self.work) - 1)
        self.show()

    @property
    def current(self):
        return self.work[self.at] if self.work else None

    def show(self):
        item = self.current
        if item is None:
            return
        coverage = coverage_of(self.app.session, self.source_file)
        counted = ("column" if self.include_settled else "outstanding")
        self.query_one("#heading", Static).update(
            f"  {self.source_file.name}\n"
            f"  [{self.at + 1}/{len(self.work)} {counted}, {coverage.describe()}]"
            + ("  -- CONFIRMED, editing" if item.is_settled else "")
            + f"\n\n  {item.name}")
        self.query_one("#evidence", Static).update(
            "\n".join(f"      {line}" for line in item.evidence.splitlines())
            or "      no measurements recorded for this column")
        self.query_one("#offer", Static).update(
            f"  offered: {item.offer.describe()}" if item.offer
            else "  nothing on offer -- this one is new")

        self.query_one("#description", Input).value = item.description or ""
        self.query_one("#unit", Input).value = item.unit or ""
        self.query_one("#note", Input).value = item.note or ""
        chooser = self.query_one("#data_type", Select)
        # clear() rather than assigning a sentinel: Select.BLANK is a stale
        # alias for False in this version of Textual and setting it raises.
        if item.data_type in DATA_TYPE_CHOICES:
            chooser.value = item.data_type
        else:
            chooser.clear()
        self.set_focus(self.query_one("#description", Input))

    def action_save(self):
        item = self.current
        if item is None:
            return
        chooser = self.query_one("#data_type", Select)
        record_field_answer(
            self.app.session, self.source_file.dataset, item.name,
            description=self.query_one("#description", Input).value.strip(),
            data_type="" if chooser.is_blank() else chooser.value,
            unit=self.query_one("#unit", Input).value.strip(),
            note=self.query_one("#note", Input).value.strip(),
            source_file=self.source_file)
        self.advance()

    def action_accept(self):
        """Take the offer exactly as it stands. The reason seeding is worth doing."""
        item = self.current
        if item is None or item.offer is None:
            return
        self.query_one("#description", Input).value = item.description or ""
        self.query_one("#unit", Input).value = item.unit or ""
        self.query_one("#note", Input).value = item.note or ""
        self.action_save()

    def action_skip(self):
        self.at = (self.at + 1) % len(self.work)
        self.show()

    def action_previous(self):
        """Back one. The thing that was missing: nothing here only goes forwards."""
        self.at = (self.at - 1) % len(self.work)
        self.show()

    def advance(self):
        """Move on. The list shrinks as answers land, so it is rebuilt each time.

        Except when settled columns are being shown, where the list does not
        shrink and staying in place would mean answering the same column twice.
        """
        remaining = field_work_for(self.app.session, self.source_file,
                                   only_outstanding=not self.include_settled)
        if self.include_settled and remaining:
            self.work = remaining
            self.at = (self.at + 1) % len(self.work)
            self.show()
            return
        if not remaining:
            self.app.notify(f"{self.source_file.name}: every column is confirmed.")
            self.dismiss(True)
            return
        self.work = remaining
        self.at = min(self.at, len(self.work) - 1)
        self.show()

    def action_back(self):
        self.dismiss(True)



# --- looking back ----------------------------------------------------------

class ReviewScreen(Screen):
    """Every column of one file, answered or not, with what was recorded.

    The work screen is a queue: it shows what is outstanding and an answered
    column drops out of it. That is right while there is a queue and useless
    afterwards, because the answer given an hour ago is then unreachable. This
    is the other view -- everything, in file order, with its state -- and
    picking a row goes straight back into it.
    """

    BINDINGS = [
        Binding("enter", "edit", "Edit this column", priority=True),
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, source_file):
        super().__init__()
        self.source_file = source_file
        self.work = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="title")
        yield DataTable(id="columns", cursor_type="row", zebra_stripes=True)
        yield Static(id="detail")
        yield Footer()

    def on_mount(self):
        table = self.query_one("#columns", DataTable)
        table.add_columns("column", "state", "type", "unit", "note", "description")
        self.action_refresh()

    def action_refresh(self):
        table = self.query_one("#columns", DataTable)
        was_on = table.cursor_row if table.row_count else 0
        table.clear()

        self.work = field_work_for(self.app.session, self.source_file,
                                   only_outstanding=False)
        for item in self.work:
            table.add_row(
                item.name,
                {"confirmed": "set", "offered": "offered",
                 "unanswered": "-"}[item.state],
                (item.data_type or "").replace("sc:", ""),
                item.unit or "",
                "yes" if item.note else "",
                (item.description or "")[:64],
                key=item.name)

        if table.row_count:
            table.move_cursor(row=min(was_on, table.row_count - 1))

        coverage = coverage_of(self.app.session, self.source_file)
        with_notes = sum(1 for item in self.work if item.note)
        self.query_one("#title", Static).update(
            f"  {self.source_file.name}\n"
            f"  {coverage.describe()}"
            + (f", {with_notes} with a note" if with_notes else "")
            + "  --  enter to edit any of them")

    def selected(self):
        table = self.query_one("#columns", DataTable)
        if not table.row_count:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return key.value if key else None

    @on(DataTable.RowHighlighted)
    def show_detail(self, event):
        """The whole description and note, since the table truncates them."""
        name = self.selected()
        item = next((each for each in self.work if each.name == name), None)
        if item is None:
            return
        lines = [f"  {item.description or '(nothing recorded)'}"]
        if item.note:
            lines.append(f"  NOTE: {item.note}")
        if item.offer and not item.is_settled:
            lines.append(f"  offered: {item.offer.describe()}")
        self.query_one("#detail", Static).update("\n".join(lines))

    @on(DataTable.RowSelected)
    def row_chosen(self, event):
        self.action_edit()

    def action_edit(self):
        name = self.selected()
        if name is None:
            return
        self.app.push_screen(
            FieldScreen(self.source_file, include_settled=True, start_at=name),
            lambda _: self.action_refresh())

    def action_back(self):
        self.dismiss(None)


class NotesScreen(Screen):
    """Everything flagged for somebody to come back to.

    What is recorded in this database, which is a different question from what
    is in the files on disk -- a note here may not have been written out yet,
    and `--notes <directory>` answers the other one. Both are worth asking.
    """

    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(id="notes")
        yield Footer()

    def on_mount(self):
        found = notes_recorded_in(self.app.session)
        if not found:
            self.query_one("#notes", Static).update(
                "\n  No notes recorded yet.\n\n"
                "  A note is the thing somebody should come back to: a count that\n"
                "  looks wrong, a meaning that is a guess, a join to check. Put one\n"
                "  on any column while working, and it is written into the published\n"
                "  description as \"NOTE: ...\" so a reader sees it too.\n\n"
                "  To list the notes in Croissant files on disk instead:\n"
                "      python3 bakery.py --notes <directory>")
            return

        lines = [f"\n  {len(found)} note(s) recorded."]
        current = None
        for note in found:
            if note.dataset != current:
                current = note.dataset
                lines.append(f"\n  {current}")
            marker = "  (column not finished)" if note.unfinished else ""
            lines.append(f"      {note.where}{marker}")
            lines.append(f"          {note.text}")
        self.query_one("#notes", Static).update("\n".join(lines))

    def action_back(self):
        self.dismiss(None)


# --- the machines ----------------------------------------------------------

class MachinesScreen(Screen):
    """What is recorded about each system, and when it was that way."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(id="machines")
        yield Footer()

    def on_mount(self):
        lines = []
        for machine in self.app.session.scalars(select(Machine).order_by(Machine.name)):
            lines.append(f"\n  {machine.name}   {machine.organization or ''}")
            if not machine.iterations:
                lines.append("      no hardware recorded -- seed a documentation "
                             "file, or type it in")
            for iteration in machine.iterations:
                span = f"{iteration.starts_on or '?'} to {iteration.ends_on or 'present'}"
                lines.append(f"      {iteration.label}: {span}")
                lines.append(f"          {iteration.vendor or '?'} / "
                             f"{iteration.architecture or '?'} / "
                             f"{iteration.scheduler or '?'}")
                for partition in iteration.partitions:
                    lines.append(
                        f"          [{partition.name}] "
                        f"{partition.node_count or '?'} nodes, "
                        f"{partition.rack_count or '?'} racks")
                    lines.append(f"              cpu {partition.cpu or '?'}")
                    lines.append(f"              gpu {partition.gpu or '?'}")
            for dataset in machine.datasets:
                lines.append(f"      dataset {dataset.name}: {len(dataset.files)} file(s), "
                             f"{len(dataset.field_docs)} column(s) described")
        self.query_one("#machines", Static).update(
            "\n".join(lines) or "\n  No machines yet. Confirm one against a file.")

    def action_back(self):
        self.dismiss(None)


# --- the application -------------------------------------------------------

class BakeryApp(App):
    """The bakery."""

    TITLE = "bakery"
    SUB_TITLE = "finishing par-baked Croissants"
    CSS = """
    #title { text-style: bold; padding: 1 0 0 0; }
    #heading { text-style: bold; padding: 1 0 0 0; }
    #evidence { color: $text-muted; padding: 0 0 1 0; }
    #offer { color: $accent; padding: 0 0 1 0; }
    #where, #hint { color: $text-muted; }
    DataTable { height: 1fr; }
    Select { width: 24; }
    """

    def __init__(self, database, output_directory="baked"):
        super().__init__()
        self.database = database
        self.output_directory = Path(output_directory)
        self.session = database.session()

    def on_mount(self):
        self.push_screen(FileListScreen())

    def on_unmount(self):
        self.session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", help="a database URL; default ./bakery.sqlite")
    parser.add_argument("--import", dest="import_from", metavar="DIRECTORY",
                        help="read a parbake output directory in before starting")
    parser.add_argument("--seed", nargs="+", metavar="PATH",
                        help="read finished Croissants, field dictionaries and "
                             "documentation in before starting")
    parser.add_argument("--out", default="baked", metavar="DIRECTORY",
                        help="where written Croissants go (default: ./baked)")
    parser.add_argument("--bake", action="store_true",
                        help="write every file that has a machine out, and stop")
    parser.add_argument("--notes", nargs="+", metavar="PATH",
                        help="list every note left for later in the Croissants "
                             "under these paths, and stop")
    arguments = parser.parse_args()

    if arguments.notes:
        # Reads files rather than the database, so it finds notes in Croissants
        # this database has never seen -- and notes written by hand into a
        # description before there was a field for them.
        from notes import find_notes, report
        for line in report(*find_notes(*arguments.notes)):
            print(line)
        return

    database = Database(arguments.database)

    # Both of these print rather than being shown in the interface: they say what
    # changed in the database, which is worth having in a terminal's scrollback
    # rather than in a screen that goes away.
    if arguments.seed:
        with database.session() as session:
            for line in seed_from(session, *arguments.seed).lines():
                print(f"  {line}")
    if arguments.import_from:
        with database.session() as session:
            for line in import_target(session, arguments.import_from).lines():
                print(f"  {line}")

    if arguments.bake:
        # Deliberately not the interface: this is the step somebody puts in a
        # script after a session, and its report belongs in a terminal's
        # scrollback where it can be read and kept.
        from baking import bake_everything
        with database.session() as session:
            for line in bake_everything(session, arguments.out).lines():
                print(f"  {line}")
        return

    BakeryApp(database, arguments.out).run()


if __name__ == "__main__":
    main()
