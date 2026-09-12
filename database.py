#!/usr/bin/env python3
"""Opening the bakery's database, wherever it lives.

One person documenting one corpus wants a file they can copy to a laptop. A
group documenting a facility's whole history wants something several people can
write to at once. Those are the same tables behind a different URL, so the
choice is one string and nothing above this module knows which was picked:

    sqlite:///bakery.sqlite                        the default, no setup
    postgresql+psycopg://user@host/bakery          a shared deployment
    mysql+pymysql://user@host/bakery               likewise

Taken in order from: the url passed in, then BAKERY_DATABASE_URL, then a
bakery.sqlite file in the working directory. The default is a path rather than
:memory: so that running the tool twice in a row remembers the first run, which
is the entire point of it.

## Why the version check, and why migrations

The tables will change. A database written by a later version and read by an
earlier one would not fail loudly -- SQLAlchemy would simply not see the new
columns, and the tool would quietly believe fewer questions had been answered
than had been. So the version is written into the database on creation and
checked on open.

A mismatch the other way -- an older database, opened by a newer bakery -- is
not a reason to refuse. Somebody has hours of answers in that file, and
`create_all` adds missing TABLES but never missing COLUMNS, so opening it
without doing anything would fail on the first query against a column that is
not there. So each version knows how to get from the one before it, in the
smallest step that does not touch anything already recorded.

Forwards only. A database from a LATER version is still refused, because this
cannot know what a future version did.

## Why foreign keys are turned on by hand

SQLite has enforced foreign keys since 3.6.19 but leaves them off unless asked,
per connection. Without the pragma, `ondelete="CASCADE"` on the models is
decoration: deleting a file would leave its columns behind as orphans on SQLite
and remove them on Postgres, and the same tool would behave differently
depending on a URL. The listener below makes the backends agree.
"""

import os
from pathlib import Path

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoSuchModuleError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from models import SCHEMA_VERSION, Base, Meta

# Read when no url is given. Set it once in a shell profile and every bakery
# command in that shell talks to the same deployment.
URL_VARIABLE = "BAKERY_DATABASE_URL"

DEFAULT_FILENAME = "bakery.sqlite"

# Drivers are not dependencies of the bakery. SQLite needs nothing; the other
# two need a package installed, and the error they raise on their own names a
# module rather than saying what to install.
SUGGESTED_DRIVERS = {
    "postgresql": "pip install 'psycopg[binary]'   (url: postgresql+psycopg://...)",
    "mysql": "pip install pymysql                  (url: mysql+pymysql://...)",
    "mariadb": "pip install pymysql                (url: mariadb+pymysql://...)",
}


class DatabaseVersionMismatch(RuntimeError):
    """The database was written by a different version of these tables."""


# How to get to each version from the one before it. Additive only: a step adds
# a column or a table and never drops, renames or rewrites one, so a database
# half way through an upgrade is a database that still has everything in it.
#
# 2: field_docs.note -- somewhere to put "a person should look at this again"
#    that is not the middle of the description.
MIGRATIONS = {
    2: ["ALTER TABLE field_docs ADD COLUMN note TEXT"],
}


def resolve_url(url=None, directory=None):
    """Which database to open: what was asked for, the environment, or a local file."""
    if url:
        return url
    from_environment = os.environ.get(URL_VARIABLE)
    if from_environment:
        return from_environment
    path = Path(directory or Path.cwd()) / DEFAULT_FILENAME
    return f"sqlite:///{path}"


def describe(url):
    """The database in a few words, for a status line.

    Passwords are not shown: SQLAlchemy's own rendering hides them, which is the
    reason for going through make_url rather than printing the string given.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        return f"SQLite at {parsed.database or ':memory:'}"
    return parsed.render_as_string(hide_password=True)


def create_engine_for(url, echo=False):
    """Build the engine, and turn foreign keys on if it is SQLite."""
    try:
        engine = create_engine(url, echo=echo, future=True)
    except NoSuchModuleError as problem:
        backend = make_url(url).get_backend_name()
        hint = SUGGESTED_DRIVERS.get(backend)
        raise SystemExit(
            f"Cannot open {describe(url)}: {problem}\n"
            + (f"The driver is not installed. Try:\n    {hint}" if hint else
               "That database backend needs a driver installed.")
        ) from problem

    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enforce_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


class Database:
    """An open database, and sessions onto it.

    Thin on purpose. Everything that knows what the bakery does with these
    tables lives above this; what lives here is opening, versioning and closing.
    """

    def __init__(self, url=None, directory=None, echo=False, create=True):
        self.url = resolve_url(url, directory)
        self.engine = create_engine_for(self.url, echo=echo)
        self.new_session = sessionmaker(bind=self.engine, future=True)
        self.created = False
        self.migrated_from = None
        if create:
            self.prepare()

    def prepare(self):
        """Create the tables if they are missing, and check the version if not."""
        Base.metadata.create_all(self.engine)

        with self.new_session() as session:
            recorded = session.get(Meta, "schema_version")
            if recorded is None:
                session.add(Meta(name="schema_version", value=str(SCHEMA_VERSION)))
                session.commit()
                self.created = True
                return
            found = int(recorded.value)
            if found > SCHEMA_VERSION:
                raise DatabaseVersionMismatch(
                    f"{describe(self.url)} was written for schema version "
                    f"{found}, and this is version {SCHEMA_VERSION}. Refusing to "
                    "read a database from a later version rather than guess at "
                    "what changed in it."
                )
            if found < SCHEMA_VERSION:
                self.migrate_from(found)

    def migrate_from(self, found):
        """Bring an older database up to this version, a step at a time.

        Each step is run in its own transaction and the version is recorded as
        each one lands, so an interrupted upgrade resumes rather than restarting
        or being left in a state nothing knows the shape of.

        A step whose column is somehow already there is not an error. It means
        this has been run before, or a column was added by hand, and either way
        the wanted state is the state that exists.
        """
        for version in range(found + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS.get(version, []):
                try:
                    with self.engine.begin() as connection:
                        connection.execute(text(statement))
                except (OperationalError, ProgrammingError) as problem:
                    if "duplicate column" not in str(problem).lower() and \
                            "already exists" not in str(problem).lower():
                        raise DatabaseVersionMismatch(
                            f"Could not bring {describe(self.url)} from version "
                            f"{found} to {SCHEMA_VERSION}: {problem}\n"
                            f"The step that failed was: {statement}"
                        ) from problem
            with self.new_session() as session:
                session.get(Meta, "schema_version").value = str(version)
                session.commit()
        self.migrated_from = found

    def session(self) -> Session:
        """A session to work in. Use it as a context manager."""
        return self.new_session()

    def is_empty(self):
        """Has anything been imported yet? Used to say so rather than show a blank list."""
        from models import SourceFile
        with self.new_session() as session:
            return session.scalar(select(SourceFile.id).limit(1)) is None

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()

    def __repr__(self):
        return f"Database({describe(self.url)})"
