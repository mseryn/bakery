# bakery

Finishes par-baked Croissant files by asking a person the questions a machine
cannot answer, and recording the answers in a database so the same question is
not asked twice.

`parbake` reads a CSV and writes a par-baked Croissant: the file's identity, its
column names in order, and measurements. It omits every judgement -- what a
field means, its type, the licence, the caveats -- and fails validation on
purpose so an unreviewed file cannot be mistaken for a finished one. The bakery
is the other half: it works out what is missing, asks about each gap with the
measurements on screen, and writes the finished Croissant back out.


## Why the answers go in a database

Measured on the corpus in `../bakery_test_out`:

| | |
|---|---|
| par-baked files | 19 |
| columns across them | 1,120 |
| distinct column names | 780 |
| machines | 5 (mira, theta, thetagpu, polaris, aurora) |

Answering each file independently is roughly 2,400 questions, most of them
repeats. The five job-completion exports share all 66 of their column names; the
five machine-status exports share all 10 of theirs.

With a database, an answer given once is offered everywhere else it might apply
and accepting it is one keystroke. The end state is that importing a new export
asks one question -- which machine -- and then confirms that everything is
already documented.


## Requirements

Python 3.11 or newer, for `hashlib.file_digest`.

    pip install sqlalchemy textual pandas psutil

`sqlalchemy` and `textual` are the bakery's own. `pandas` and `psutil` are
parbake's, and are required here because the bakery imports parbake through
`parbake_link.py` -- without them nothing in the bakery starts.

The modules are run in place rather than installed; there is no package to
install, and `pyproject.toml` is there for pytest's settings and as a record of
what is needed.

Optional:

    pip install 'psycopg[binary]'   # to use PostgreSQL
    pip install pymysql             # to use MySQL
    pip install mlcroissant         # for the two tests that validate output

SQLite, the default, needs nothing further.


## Commands

    python3 bakery.py --seed ../complete_documentation ../croissant_files \
                      --import ../bakery_test_out
    python3 bakery.py                       # afterwards, just this

| option | effect |
|---|---|
| `--seed PATH...` | read finished Croissants, field dictionaries and documentation |
| `--import DIRECTORY` | read a parbake output directory |
| `--bake` | write every file that has a machine out, then stop |
| `--notes PATH...` | list every note left for later, then stop |
| `--out DIRECTORY` | where written Croissants go (default `./baked`) |
| `--database URL` | which database to use |

`--seed` and `--import` are both idempotent. Running them again is how new files
are picked up.


## Choosing a database

Taken in order from `--database`, then `BAKERY_DATABASE_URL`, then a
`bakery.sqlite` file in the working directory.

    sqlite:///bakery.sqlite                     the default; one file, no setup
    postgresql+psycopg://user@host/bakery       a shared deployment
    mysql+pymysql://user@host/bakery            likewise

A missing driver reports what to install rather than a missing module name.
Passwords are not printed.


## Screens

| screen | reached by | contents |
|---|---|---|
| files | opens here | every imported file, least finished first |
| machine | `enter` on a file with no machine | the machine question, with its evidence |
| columns | `enter` on a file with a machine | one column at a time |
| review | `a` | every column of a file, answered or not |
| change machine | `m` on review, `M` on the file list | move a file to another machine, answers and all |
| copy answers in | `c` on review or the file list | copy another dataset's answers for matching columns |
| notes | `n` | everything flagged to come back to |
| machines | `m` | each system, its iterations and its hardware |

`b` on the file list writes the selected file's Croissant out.

**Files.** Sorted least-finished first, so what needs doing is not below what is
done. The cursor holds its file as the queue reorders. A `?` before a machine
name marks a suggestion nobody has confirmed.

**Machine.** The one question that cannot be skipped, because everything else is
scoped through it. The evidence is shown rather than just the conclusion: a
filename reading POLARIS over data whose every row reads `thetagpu` is the case
that matters. Confirming the machine also places the file in a dataset.

**Columns.** One column at a time, with parbake's measurements beside it:

    ANL-ALCF-DJC-POLARIS_20220809_20221231
    [21/50 outstanding, 16/66 confirmed]

    EXIT_CODE
        39,432 rows
        42 distinct values
        most common: '0' (49.4%), '271' (12.4%), '-29' (12.9%)
        39,432 values are numbers, from -29 to 271
      offered: POLARIS_MACHINESTATUS on this machine describes the same column

Without the measurements, "what does EXIT_CODE hold?" is a memory test.

| key | effect |
|---|---|
| `ctrl+s` | record it and move to the next |
| `ctrl+a` | accept the offer as it stands |
| `ctrl+k` | next column |
| `ctrl+j` | previous column |
| `esc` | back |

Every answer is written when it is given. Closing the terminal loses the
question being typed and nothing else.

**Review.** The columns screen is a queue, so an answered column drops out of it
and becomes unreachable. The review screen lists every column of a file with its
state, type, unit, whether it carries a note, and its description; `enter` on any
row reopens that column with its values filled in for editing.


## Correcting in bulk

**Changing a file's machine.** `m` on the review screen, or `M` on the file list,
opens the machine question with the current machine filled in. The file moves to
`<MACHINE>_<KIND>` for the machine chosen, and its answers follow it:

- If it was the only file in its old dataset, the answers move and the emptied
  dataset is removed.
- If other files remain there, the answers are copied, and those files keep
  theirs.
- Where the new dataset already has a confirmed, complete answer, that answer is
  kept, and the report lists the columns where the two differed.
- A decoding-table link moves to the new machine's table of the same name, or is
  dropped and listed if there is none.
- The file's iteration is re-dated against the new machine.

**Copying answers from another dataset.** `c` opens a list of every other dataset,
sorted by how many of this file's columns it has confirmed answers for. Choosing
one shows what the copy would do before writing anything:

    From POLARIS_MACHINESTATUS into AURORA_MACHINESTATUS

      columns answered in both      10
        will be filled              0
        already the same            7
        differ, you choose          3

`ctrl+s` copies. Each difference is then shown with both versions: `u` uses the
copy, `k` keeps the current answer, `a` and `x` do the same for all remaining.
`esc` abandons the copy, and nothing is written.

A copy carries each column's description, type, unit, note and decoding-table
link, and the dataset-level answers (licence, citation, keywords, responsible-AI
blocks). Rules:

- Only answers confirmed in the source are copied, so an unreviewed seeded answer
  does not become confirmed by being copied.
- A gap -- a column unanswered, unconfirmed or incomplete in the target -- is
  filled. Parts the source leaves empty keep what the target had.
- A difference replaced by choice takes the source's answer exactly.
- Copied answers are confirmed and recorded as `copied from <dataset>`.
- A decoding-table link is copied to the target machine's table of the same name,
  or left out and listed.


## What is recorded, and at which level

    machines            the systems, by name
    machine_iterations  what a machine was made of, between two dates
    machine_partitions  the per-partition hardware of one iteration
    datasets            a series of files: POLARIS_DJC, every date range of it
    source_files        one imported par-baked Croissant
    file_fields         its columns, with parbake's measurements
    field_docs          a column's description, type, unit and note
    enumerations        a decoding table: what the codes in a column mean
    enumeration_values  one code and its meaning, flagged if provisional
    dataset_answers     licence, citation, the responsible-AI blocks
    derived_files       the .txt and .md siblings, recorded and never read

Each fact sits at the level it is true at.

- **Hardware** is true of a machine between two dates. See machine iterations
  below.
- **A licence, landing page or limitation** is true of a dataset. The finished
  Croissant for POLARIS_DJC covers every date range of it, so these are answered
  once for the series rather than once per file.
- **Measurements** are true of one file.
- **A column description** is true of one dataset's column.

That last one is not per machine. Polaris records `START_TIMESTAMP` in its job
log and in its machine-status log; one is when a job began, the other when an
outage began. Measured on the corpus, **38 (machine, column) pairs mean
different things in different datasets on the same machine**, so one description
per machine would have to be wrong for at least one of them.

Scoping that tightly would mean retyping `MACHINE_NAME` for every dataset, so
what another dataset already records is offered as a default, nearest first:

| kind of offer | source |
|---|---|
| seeded | already recorded here, from a finished file, unconfirmed |
| sibling | the same column, same machine, a different export |
| other machine | the same column name on a different system |
| respelled | the same name in another case |

The last is shown but never assumed: an export that renames its columns may have
changed more than the capitalisation. No offer is ever applied on its own.


## Import

Reads the Croissants in a parbake output directory. The `.txt` and `.md` are
generated from the Croissant, so importing them would record the same facts twice
and allow the copies to disagree; they are recorded as files that exist and never
opened. A test makes the `.txt` unreadable before importing to enforce this.

A machine is worked out from the filename and from the `MACHINE_NAME` column, and
then recorded in `machine_suggestion` rather than acted on. Where the filename and
the column disagree, the column is preferred and the disagreement is reported. A
column with several distinct values suggests nothing, because such a file covers
several machines.

Both naming conventions in the corpus are parsed:

| filename | kind | machine | dates |
|---|---|---|---|
| `ANL-ALCF-DJC-POLARIS_20220809_20221231` | DJC | polaris | range |
| `ANL-ALCF-GPU-NODE-POLARIS_20230920_20231231` | GPU-NODE | polaris | range |
| `aurora_crayex_telemetry_power_2024-05-02` | crayex_telemetry_power | aurora | one day |
| `anonymized_aurora_dim_job_comp_2026-01` | anonymized_dim_job_comp | aurora | one month |

`ANL-ALCF` is not hard-coded. The organisation and facility prefix is found from
what every name in the batch shares, so what remains before the machine is the
kind. A machine already in the database is recognised wherever it appears in the
name, which is what makes the underscore convention work and what makes the
suggestions improve as the database fills. A file naming no machine in either its
name or its data gets no suggestion and the person is asked;
`aurora_crayex_telemetry_power_2024-05-02` is that case.

Importing twice updates rather than duplicates. A file is matched on the dataset
name inside its Croissant, not its path, so a moved or copied output directory
updates the existing row. When a Croissant has been re-measured its columns are
brought into line -- added, removed, measurements refreshed -- and nothing a
person recorded is touched, because descriptions belong to the dataset and
measurements to the file.


## Seed

Three finished sources describe POLARIS_DJC, and none is complete on its own:

| source | contributes what only it has |
|---|---|
| `djc_v4.croissant.json` | every field typed, the dataset answers, the decodings |
| `*_field_dictionary.csv` | the `UNIT` column, for which Croissant has no property |
| `*_documentation*.md` | section 3, the machine profile |

Read richest first, so thinner sources fill gaps instead of being reported as
disagreements. From the real files: 2 machines with hardware profiles, 3
datasets, 93 field rows of which 39 are described and 93 typed, 11 units, 16
dataset-level answers, and one decoding table of 10 values, 3 of them
provisional.

Nothing seeded is confirmed. Each row lands with `is_confirmed = False` and a
`recorded_by` naming its file, and is offered at the prompt as a default. This is
what makes it safe for seeding to create machines and datasets without asking:
nothing it creates is treated as decided. A confirmed answer is never overwritten
by a file; where two unconfirmed sources disagree, the first read is kept and the
disagreement is reported.

Two things it refuses to do:

- **Treat a placeholder as a description.** 38 of djc_v4's 66 descriptions read
  `(no meaning supplied for NODES_USED)`. Seeding those would mark 38 fields
  described and stop them ever being asked about. The real `dataType` beside each
  is kept, so the column stays outstanding.
- **Guess which column a decoding table belongs to.** djc_v4 defines
  `exit_code_enum` and references it from neither `EXIT_CODE` nor `EXIT_STATUS`.
  The candidates are reported; a person picks.


## Notes

A note is not an unanswered question. A column can have a complete, confirmed
description and still need one: "disagrees with the accounting table by ~2%",
"cores count hardware threads, not physical cores", "nobody has been able to
explain this one". Quality notes cannot be derived from what is outstanding,
because the work on that column is finished.

A note has its own entry area on the columns screen and its own database column.
On the way out it is appended to the description, as a unit is:

    Core hours actually consumed. Unit: Core Hours. NOTE: Disagrees with the
    fact table by ~2%.

It travels with the published file, so a reader sees it. Because the marker is
fixed, every note in a corpus can be listed again:

    python3 bakery.py --notes ./baked ../croissant_files

    8 note(s) across 2 dataset(s), from 24 Croissant(s) read.

    ANL-ALCF-DJC-POLARIS_20220809_20221231  (still par-baked)
        records / EXIT_STATUS
            Disagrees with EXIT_CODE on 122 of 39,432 rows. Confirm which to trust.

`--notes` reads files rather than the database. It therefore finds notes typed
into a description by hand before there was a field for them, notes in Croissants
this database has never imported, and notes written by an earlier version. All
four places a description can appear are scanned: the dataset, each distribution
entry, each record set, each field. The `n` screen answers the other question --
what has been flagged while working, including notes not yet written out.

The marker is upper-case `NOTE:` or `NOTES:`, matched case-sensitively. parbake's
banner contains "Record the responsible-AI notes: limitations, biases, personal
information"; matching case-insensitively reported a note in every par-baked file
in the corpus.


## Output

    <name>.baked.json     one file, one date range. A concrete cr:FileObject
                          with a sha256 computed from the data.
    <DATASET>.baked.json  the series. One cr:FileSet standing for every date
                          range. The shape that gets published.

Both carry the same descriptions, decodings and responsible-AI blocks. They
differ in the distribution block and in whether a checksum can exist. A
`.baked.md` is rendered beside each using parbake's own renderer, so the readable
projection cannot drift from the one parbake defines.

**The par-baked marker is replaced only when nothing is outstanding.** The unreal
`conformsTo` is the one thing preventing unfinished work from appearing finished.
It is removed only when every column is confirmed, every dataset-level question
answered, a checksum computable, and no answer malformed. Until then the file is
still written, with the marker in place and an `_unfinished` block listing every
reason, because half-finished is the normal state and should be inspectable.

Checked with `mlcroissant` rather than asserted: a finished file passes with no
errors in both shapes, and an unfinished one fails, naming the par-baked
`conformsTo` among the reasons.

Units are appended to the description, and not twice if the text already says it.
The machine profile is written as a `_machine` block beside the measurements
parbake left, with vendor, architecture and scheduler folded into keywords where
djc_v4 lists them by hand. `contentUrl` is the bare filename, never the path the
data occupied during import.


## Differences from the hand-written djc_v4

- **A series is a `cr:FileSet`.** djc_v4 writes a `cr:FileObject` carrying
  `"sha256": "unknown-not-yet-published"` -- a placeholder in the one field whose
  purpose is to be verifiable. `cr:FileSet` exists for many files matching a
  pattern, where a single checksum cannot exist, and it validates.
- **Decoding tables are linked from the columns that use them**, through
  `references`, rather than being defined and left unreferenced.
- **The `includes` glob comes from the filenames.** The dataset is `POLARIS_DJC`
  and its files are `ANL-ALCF-DJC-POLARIS_*.csv`; using the dataset name as a
  pattern would match nothing.


## Machine iterations

Machines change underneath the data. Theta was Cobalt-scheduled and then
PBS-scheduled; a machine gains a partition, swaps an interconnect, or doubles its
node count, and an old export is then described by hardware that no longer
exists. Hardware therefore hangs off a dated iteration rather than off the
machine.

    iteration.next_iteration(date(2024, 6, 1), "after the 2024 refresh")

copies every attribute and every partition from the current iteration, closes the
previous one on the day before the new start, and records which iteration it was
derived from. Only the lines that changed are then edited. Retyping a profile is
how a GPU count becomes wrong.

An open `ends_on` is how the documentation writes "present". A retired machine
has no current iteration, which is not an error. A file's `covers_from` and
`covers_to`, read from its filename, suggest an iteration; the suggestion is
recorded only when one covers those dates.

Column descriptions hang off datasets rather than iterations: a node refresh does
not change what `EXIT_CODE` holds, and expiring descriptions with the hardware
would mean retyping all 66 the first time a machine gains a partition.


## Schema changes

`create_all` adds missing tables but never missing columns, so opening an older
database and doing nothing would fail on the first query against a column that is
absent -- and that file holds hours of answers. Each version therefore knows how
to reach the one after it, in the smallest additive step: a migration adds a
column or a table and never drops, renames or rewrites one. The version is
recorded as each step lands, so an interrupted upgrade resumes.

Forwards only. A database from a later version is refused, because this cannot
know what that version did to it.

Current schema version: 2. Version 2 added `field_docs.note`.


## Files and artifacts

Paths are relative to this repository, except Input, Output and Working state
rows, which are where a run finds or writes them. Angle brackets mark a name
that varies. parbake's files are listed in parbake's README.

Program: run from the command line. Module: imported by other files. Test: run by pytest. Test fixture: data a test reads. Documentation and Configuration: in the repository. Input: read from elsewhere. Output: written by a run. Working state: written during a run and not committed.

| Path | Category | Purpose |
|---|---|---|
| `bakery.py` | Program | Textual interface and command line: import, seed, work through files, write Croissants, list notes. |
| `bake_croissants.py` | Program | Older file-in, file-out path: asks the questions for one par-baked file and writes <name>.baked.json. importing.py and baking.py borrow parts of it. |
| `models.py` | Module | The 12 database tables and the schema version. |
| `database.py` | Module | Opens SQLite, MySQL or PostgreSQL from a URL, and migrates an older schema forward. |
| `importing.py` | Module | --import: reads par-baked Croissants into files, columns and measurements, and suggests a machine. |
| `seeding.py` | Module | --seed: reads finished Croissants, field dictionaries and documentation in, unconfirmed. |
| `outstanding.py` | Module | What each file still needs, what other datasets offer, and recording answers. |
| `transfer.py` | Module | Changes a file's machine, carrying its answers, and copies answers between datasets. |
| `baking.py` | Module | Writes finished Croissants, per file and per dataset, with Markdown. |
| `notes.py` | Module | Finds NOTE: markers in Croissant files and in the database. |
| `questions.py` | Module | The dataset-level question catalogue, and descriptions of measurements. |
| `parbake_link.py` | Module | Puts parbake on the import path and re-exports its markers, output folder names and Markdown renderer. |
| `tests/test_bakery.py` | Test | The screens, through Textual's headless driver. |
| `tests/test_bake_croissants.py` | Test | The older file-in, file-out path. |
| `tests/test_database.py` | Test | Tables, constraints, cascades and migrations. |
| `tests/test_importing.py` | Test | Reading parbake output. |
| `tests/test_seeding.py` | Test | Reading finished work. |
| `tests/test_outstanding.py` | Test | What is asked, and what is offered. |
| `tests/test_transfer.py` | Test | Changing a file's machine, and copying answers. |
| `tests/test_baking.py` | Test | Writing Croissants, including validation with mlcroissant. |
| `tests/test_notes.py` | Test | Finding notes. |
| `README.md` | Documentation | How to run bakery, its screens and rules, tests, and known gaps. |
| `docs/pipeline_map.html` | Documentation | Pipeline map page: data flow diagram, guarantees, keys, import grid, and text for Claude chat. |
| `docs/pipeline_flow.svg` | Documentation | The data flow diagram as a standalone SVG, for editing. |
| `docs/for_claude_chat.md` | Documentation | Plain-text description and Mermaid diagram of bakery. |
| `pyproject.toml` | Configuration | Dependencies, optional database drivers and validator, and pytest settings. |
| `.gitignore` | Configuration | Excludes virtual environments, caches, working databases and baked/. |
| `parbaked_croissants/*.parbaked.json` | Input | Par-baked Croissants written by parbake. Read by --import. |
| `*.croissant.json` | Input | Finished Croissants written by hand. Read by --seed. |
| `*_field_dictionary*.csv` | Input | Field dictionaries with FIELD, type, UNIT and MEANING columns. Read by --seed. |
| `*_documentation*.md` | Input | Dataset documentation; its System section gives the machine's hardware. Read by --seed. |
| `bakery.sqlite` | Working state | Default database in the working directory: machines, datasets, files and every answer. Not committed. |
| `<out>/<file>.baked.json` | Output | Croissant for one file, with the data's sha256. Keeps the par-baked marker until nothing is outstanding. <out> defaults to baked/. |
| `<out>/<file>.baked.md` | Output | Markdown rendering of the file's Croissant. |
| `<out>/<DATASET>.baked.json` | Output | Croissant for the dataset's whole series, as a cr:FileSet. |
| `<out>/<DATASET>.baked.md` | Output | Markdown rendering of the dataset's Croissant. |


`parbake_link.py` is the only module that reaches into parbake. Everything
borrowed -- the par-baked markers, the output directory names, the Markdown
renderer -- comes through it, so a rename there is a one-line change here.


## Tests

    cd bakery && pytest

260 tests.

| file | count | covers |
|---|---|---|
| `test_bake_croissants.py` | 48 | the older file-in, file-out path |
| `test_database.py` | 40 | the tables, constraints, cascades, migrations |
| `test_baking.py` | 39 | writing both shapes, and validation |
| `test_outstanding.py` | 25 | what is asked, and what is offered |
| `test_seeding.py` | 24 | reading finished work in |
| `test_bakery.py` | 27 | the screens, through Textual's headless driver |
| `test_importing.py` | 22 | reading parbake output in |
| `test_notes.py` | 20 | finding notes |
| `test_transfer.py` | 14 | changing a machine, copying answers |

Everything deciding *what* to ask lives in `outstanding.py` and is tested without
a terminal. The screens are tested with Textual's headless driver. Several tests
run against the real corpus rather than fixtures.

The ones that matter:

- answering everything produces a file that validates, in both shapes, checked
  with `mlcroissant` rather than asserted
- an unfinished file still does not validate
- a malformed version or date keeps the par-baked marker in place
- a description confirmed in one dataset does not affect another's
- an offer is shown and never applied on its own
- a confirmed answer is never overwritten by a file
- a placeholder description is not seeded as a description
- the `.txt` sibling is made unreadable before an import, and the import passes
- a re-measured file gains and loses columns without losing descriptions
- the cursor holds its file while the queue reorders
- an older database is brought forward with its contents intact; one from a later
  version is refused
- a note reaches the published description and `--notes` finds it again
- the note marker does not match the word "notes" in prose


## Known gaps

- **The dataset-level questions have no screen.** `outstanding.py` works out
  which are left, `seeding.py` fills them in and `baking.py` writes them out, but
  there is nowhere to answer them. A dataset nobody seeded cannot be finished
  through the interface. This is the largest remaining gap.
- **No screen for editing a machine profile or opening a new iteration.** The
  model and `next_iteration` exist and are tested; the interface is missing.
- **Notes are per column.** A note about a whole dataset can be typed into its
  description by hand and `--notes` will find it, but there is no entry area for
  one. It needs the dataset screen above.
- **Validation is narrow.** `version` and `datePublished` are checked against the
  shapes a validator requires. Nothing checks that a licence is a licence.
- **Stale par-baked siblings are reported, not replaced.** A `.baked.md` is
  written beside the finished JSON; the superseded par-baked `.txt` and `.md` are
  named and left in place.
- **Enumerations are recorded per machine, not per dataset.** Exit codes are a
  property of the scheduler, so this is usually right, but a machine whose
  scheduler changed between iterations would need two.
