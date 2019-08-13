"""keywordtable - an SQLite database of keywords

Keywords can be loaded from resource files, test suite files,
and libdoc-formatted xml, or from python libraries. These
are referred to as "collections".

"""

import ast
import json
import logging
import os
import re
import sys

import robot.libraries
from robot.libdocpkg import LibraryDocumentation
from robot.errors import DataError
from sqlalchemy import and_, or_, create_engine, Column, ForeignKey, Integer, MetaData, Sequence, Table, Text
from sqlalchemy.sql import select
from watchdog.events import PatternMatchingEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

"""
Note: It seems to be possible for watchdog to fire an event
when a file is modified, but before the file is _finished_
being modified (ie: you get the event when some other program
writes the first byte, rather than waiting until the other
program closes the file)

For that reason, we might want to mark a collection as
"dirty", and only reload after some period of time has
elapsed? I haven't yet experienced this problem, but
I haven't done extensive testing.
"""


class WatchdogHandler(PatternMatchingEventHandler):
    patterns = ["*.robot", "*.txt", "*.py", "*.tsv"]

    def __init__(self, kwdb, path):
        PatternMatchingEventHandler.__init__(self)
        self.kwdb = kwdb
        self.path = path

    def on_created(self, event):
        # monitor=False because we're already monitoring
        # ancestor of the file that was created. Duh.
        self.kwdb.add(event.src_path, monitor=False)

    def on_deleted(self, event):
        # FIXME: need to implement this
        pass

    def on_modified(self, event):
        self.kwdb.on_change(event.src_path, event.event_type)


class KeywordTable(object):
    """Abstraction over database of keywords"""

    def __init__(self, conn_string, poll=False):
        self._engine = create_engine(conn_string)
        self.db = self._engine.connect()
        self.log = logging.getLogger(__name__)
        self._create_db()

        # set up watchdog observer to monitor changes to
        # keyword files (or more correctly, to directories
        # of keyword files)
        self.observer = PollingObserver() if poll else Observer()
        self.observer.start()

    def add(self, name, monitor=True):
        """Add a folder, library (.py) or resource file (.robot, .tsv, .txt) to the database
        """

        if os.path.isdir(name):
            if not os.path.basename(name).startswith("."):
                self.add_folder(name)

        elif os.path.isfile(name):
            if ((self._looks_like_resource_file(name)) or
                    (self._looks_like_libdoc_file(name)) or
                    (self._looks_like_library_file(name))):
                self.add_file(name)
                if self._looks_like_library_file(name):
                    class_names = self._get_classnames_from_file(name)
                    if class_names:
                        self.add_keywords_from_classes(name, class_names)
        else:
            # let's hope it's a library name!
            self.add_library(name)

    def add_keywords_from_classes(self, path, class_names):
        sys.path.append(os.path.dirname(path))
        file_name = os.path.splitext(os.path.basename(path))[0]
        for class_name in class_names:
            try:
                lib_mane = '{}.{}'.format(file_name, class_name)
                self.add_library(lib_mane)
            except (KeyError, AttributeError, DataError):
                pass

    def _get_classnames_from_file(self, path):
        with open(path) as file_to_read:
            source = file_to_read.read()

        p = ast.parse(source)
        class_names = [node.name for node in ast.walk(p) if isinstance(node, ast.ClassDef)]
        return class_names

    def on_change(self, path, event_type):
        """Respond to changes in the file system

        This method will be given the path to a file that
        has changed on disk. We need to reload the keywords
        from that file
        """
        # I can do all this work in a sql statement, but
        # for debugging it's easier to do it in stages.
        sql = """SELECT collection_id
                 FROM collection_table
                 WHERE path == ?
        """
        cursor = self.db.execute(select([self.collections.c.collection_id]).where(self.collections.c.path == path))
        results = cursor.fetchall()
        # there should always be exactly one result, but
        # there's no harm in using a loop to process the
        # single result
        for result in results:
            collection_id = result[0]
            # remove all keywords in this collection
            sql = """DELETE from keyword_table
                     WHERE collection_id == ?
            """
            self.db.execute(self.keywords.delete().where(self.keywords.c.collection_id == collection_id))
            self._load_keywords(collection_id, path=path)

    def _load_keywords(self, collection_id, path=None, libdoc=None):
        """Load a collection of keywords

           One of path or libdoc needs to be passed in...
        """
        if libdoc is None and path is None:
            raise (Exception("You must provide either a path or libdoc argument"))

        if libdoc is None:
            libdoc = LibraryDocumentation(path)

        if len(libdoc.keywords) > 0:
            for keyword in libdoc.keywords:
                self._add_keyword(collection_id, keyword.name, keyword.doc, keyword.args)

    def add_file(self, path):
        """Add a resource file or library file to the database"""
        libdoc = LibraryDocumentation(path)
        if len(libdoc.keywords) > 0:
            if libdoc.doc.startswith("Documentation for resource file"):
                # bah! The file doesn't have an file-level documentation
                # and libdoc substitutes some placeholder text.
                libdoc.doc = ""

            collection_id = self.add_collection(path, libdoc.name, libdoc.type,
                                                libdoc.doc, libdoc.version,
                                                libdoc.scope, libdoc.named_args,
                                                libdoc.doc_format)
            self._load_keywords(collection_id, libdoc=libdoc)

    def add_library(self, name):
        """Add a library to the database

        This method is for adding a library by name (eg: "BuiltIn")
        rather than by a file.
        """
        libdoc = LibraryDocumentation(name)
        if len(libdoc.keywords) > 0:
            # FIXME: figure out the path to the library file
            collection_id = self.add_collection(None, libdoc.name, libdoc.type,
                                                libdoc.doc, libdoc.version,
                                                libdoc.scope, libdoc.named_args,
                                                libdoc.doc_format)
            self._load_keywords(collection_id, libdoc=libdoc)

    def add_folder(self, dirname, watch=True):
        """Recursively add all files in a folder to the database

        By "all files" I mean, "all files that are resource files
        or library files". It will silently ignore files that don't
        look like they belong in the database. Pity the fool who
        uses non-standard suffixes.

        N.B. folders with names that begin with '." will be skipped
        """

        ignore_file = os.path.join(dirname, ".rfhubignore")
        try:
            with open(ignore_file, "r") as f:
                exclude_patterns = []
                for line in f.readlines():
                    line = line.strip()
                    if re.match(r'^\s*#', line):
                        continue
                    if len(line.strip()) > 0:
                        exclude_patterns.append(line)
        except:
            # should probably warn the user?
            pass

        for filename in os.listdir(dirname):
            path = os.path.join(dirname, filename)
            (basename, ext) = os.path.splitext(filename.lower())

            try:
                if os.path.isdir(path):
                    if not basename.startswith("."):
                        if os.access(path, os.R_OK):
                            self.add_folder(path, watch=False)
                else:
                    if ext in (".xml", ".robot", ".txt", ".py", ".tsv"):
                        if os.access(path, os.R_OK):
                            self.add(path)
            except Exception as e:
                # I really need to get the logging situation figured out.
                print("bummer:", str(e))

        # FIXME:
        # instead of passing a flag around, I should just keep track
        # of which folders we're watching, and don't add watchers for
        # any subfolders. That will work better in the case where
        # the user accidentally starts up the hub giving the same
        # folder, or a folder and it's children, on the command line...
        if watch:
            # add watcher on normalized path
            dirname = os.path.abspath(dirname)
            event_handler = WatchdogHandler(self, dirname)
            self.observer.schedule(event_handler, dirname, recursive=True)

    def add_collection(self, path, c_name, c_type, c_doc, c_version="unknown",
                       c_scope="", c_namedargs="yes", c_doc_format="ROBOT"):
        """Insert data into the collection table"""
        if path is not None:
            # We want to store the normalized form of the path in the
            # database
            path = os.path.abspath(path)
        insert = self.collections.insert()\
            .values(name=c_name, type=c_type, version=c_version, scope=c_scope, namedargs=c_namedargs,
                    path=path, doc=c_doc, doc_format=c_doc_format)
        result = self.db.execute(insert)
        return result.inserted_primary_key[0]

    def add_installed_libraries(self):
        """Add any installed libraries that we can find

        We do this by looking in the `libraries` folder where
        robot is installed. If you have libraries installed
        in a non-standard place, this won't pick them up.
        """

        libdir = os.path.dirname(robot.libraries.__file__)
        loaded = []
        for filename in os.listdir(libdir):
            if filename.endswith(".py") or filename.endswith(".pyc"):
                libname, ext = os.path.splitext(filename)
                if (libname.lower() not in loaded and
                        not self._should_ignore(libname)):

                    try:
                        self.add(libname)
                        loaded.append(libname.lower())
                    except Exception as e:
                        # need a better way to log this...
                        self.log.debug("unable to add library: " + str(e))

    def get_collection(self, collection_id):
        """Get a specific collection"""
        sql = """SELECT collection.collection_id, collection.type,
                        collection.name, collection.path,
                        collection.doc,
                        collection.version, collection.scope,
                        collection.namedargs,
                        collection.doc_format
                 FROM collection_table as collection
                 WHERE collection_id == ? OR collection.name like ?
        """
        query = select([self.collections]) \
            .where(self.collections.c.collection_id == collection_id)
        # need to handle the case where we get more than one result...
        sql_result = self.db.execute(query).fetchone()
        if sql_result is not None:
            return {
                "collection_id": sql_result[0],
                "name": sql_result[1],
                "type": sql_result[2],
                "version": sql_result[3],
                "scope": sql_result[4],
                "namedargs": sql_result[5],
                "path": sql_result[6],
                "doc": sql_result[7],
                "doc_format": sql_result[8]
            }

    def get_collections(self, pattern="*", libtype="*"):
        """Returns a list of collection name/summary tuples"""

        sql = """SELECT collection.collection_id, collection.name, collection.doc,
                        collection.type, collection.path
                 FROM collection_table as collection
                 WHERE name like ?
                 AND type like ?
                 ORDER BY collection.name
              """
        query = select([
            self.collections.c.collection_id,
            self.collections.c.name,
            self.collections.c.doc,
            self.collections.c.type,
            self.collections.c.path]
        ).where(
            and_(
                self.collections.c.name.ilike(self._glob_to_sql(pattern)),
                self.collections.c.type.ilike(self._glob_to_sql(libtype))
            )
        ).order_by(self.collections.c.name)

        result = self.db.execute(query)
        return [{"collection_id": result[0],
                 "name": result[1],
                 "synopsis": result[2].split("\n")[0],
                 "type": result[3],
                 "path": result[4]
                 } for result in result]

    def get_keyword_data(self, collection_id):
        sql = """SELECT keyword.keyword_id, keyword.name, keyword.args, keyword.doc
                 FROM keyword_table as keyword
                 WHERE keyword.collection_id == ?
                 ORDER BY keyword.name
              """
        query = select([
            self.keywords.c.keyword_id, self.keywords.c.name, self.keywords.c.args, self.keywords.c.doc
        ]).where(
            self.keywords.c.collection_id == collection_id
        ).order_by(self.keywords.c.name)

        result = self.db.execute(query)
        return result.fetchall()

    def get_keyword(self, collection_id, name):
        """Get a specific keyword from a library"""
        sql = """SELECT keyword.name, keyword.args, keyword.doc
                 FROM keyword_table as keyword
                 WHERE keyword.collection_id == ?
                 AND keyword.name like ?
              """
        query = select([
            self.keywords.c.name, self.keywords.c.args, self.keywords.c.doc
        ]).where(
            and_(
                self.keywords.c.collection_id == collection_id,
                self.keywords.c.name.ilike(name)
            )
        )

        result = self.db.execute(query)
        # We're going to assume no library has duplicate keywords
        # While that in theory _could_ happen, it never _should_,
        # and you get what you deserve if it does.
        row = result.fetchone()
        if row is not None:
            return {"name": row[0],
                    "args": json.loads(row[1]),
                    "doc": row[2],
                    "collection_id": collection_id
                    }
        return {}

    def get_keyword_hierarchy(self, pattern="*"):
        """Returns all keywords that match a glob-style pattern

        The result is a list of dictionaries, sorted by collection
        name.

        The pattern matching is insensitive to case. The function
        returns a list of (library_name, keyword_name,
        keyword_synopsis tuples) sorted by keyword name

        """
        query = select([
            self.collections.c.collection_id,
            self.collections.c.name,
            self.collections.c.path,
            self.keywords.c.name,
            self.keywords.c.doc
        ]).select_from(
            self.collections.join(self.keywords)
        ).where(
            self.collections.c.name.ilike(self._glob_to_sql(pattern))
        ).order_by(
            self.collections.c.name, self.collections.c.collection_id, self.keywords.c.name
        )
        sql = """SELECT collection.collection_id, collection.name, collection.path,
                 keyword.name, keyword.doc
                 FROM collection_table as collection
                 JOIN keyword_table as keyword
                 WHERE collection.collection_id == keyword.collection_id
                 AND keyword.name like ?
                 ORDER by collection.name, collection.collection_id, keyword.name
             """
        result = self.db.execute(query)
        libraries = []
        current_library = None
        for row in result.fetchall():
            (c_id, c_name, c_path, k_name, k_doc) = row
            if c_id != current_library:
                current_library = c_id
                libraries.append({"name": c_name, "collection_id": c_id, "keywords": [], "path": c_path})
            libraries[-1]["keywords"].append({"name": k_name, "doc": k_doc})
        return libraries

    def search(self, pattern="*", mode="both"):
        """Perform a pattern-based search on keyword names and documentation

        The pattern matching is insensitive to case. The function
        returns a list of tuples of the form library_id, library_name,
        keyword_name, keyword_synopsis, sorted by library id,
        library name, and then keyword name

        If a pattern begins with "name:", only the keyword names will
        be searched. Otherwise, the pattern is searched for in both
        the name and keyword documentation.

        You can limit the search to a single library by specifying
        "in:" followed by the name of the library or resource
        file. For example, "screenshot in:Selenium2Library" will only
        search for the word 'screenshot' in the Selenium2Library.

        """
        pattern = self._glob_to_sql(pattern)

        sql = """SELECT collection.collection_id, collection.name, keyword.name, keyword.doc
                 FROM collection_table as collection
                 JOIN keyword_table as keyword
                 WHERE collection.collection_id == keyword.collection_id
                 AND %s
                 ORDER by collection.collection_id, collection.name, keyword.name
             """
        where_clause = or_(
                self.keywords.c.name.ilike(pattern),
                self.keywords.c.doc.ilike(pattern)
            )
        if mode == "name":
            where_clause = self.keywords.c.name.ilike(pattern)

        query = select([
            self.collections.c.collection_id,
            self.collections.c.name,
            self.keywords.c.name,
            self.keywords.c.doc
        ]).select_from(
            self.collections.join(self.keywords)
        ).where(
            where_clause
        ).order_by(
            self.collections.c.collection_id, self.collections.c.name, self.keywords.c.name
        )

        cursor = self.db.execute(query)
        result = [(row[0], row[1], row[2], row[3].strip().split("\n")[0])
                  for row in cursor]
        return list(set(result))

    def get_keywords(self, pattern="*"):
        """Returns all keywords that match a glob-style pattern

        The pattern matching is insensitive to case. The function
        returns a list of (library_name, keyword_name,
        keyword_synopsis tuples) sorted by keyword name

        """
        query = select([
            self.collections.c.collection_id,
            self.collections.c.name,
            self.keywords.c.name,
            self.keywords.c.doc,
            self.keywords.c.args
        ]).select_from(
            self.collections.join(self.keywords)
        ).where(
            self.keywords.c.name.ilike(self._glob_to_sql(pattern))
        ).order_by(
            self.collections.c.name, self.keywords.c.name
        )
        sql = """SELECT collection.collection_id, collection.name,
                        keyword.name, keyword.doc, keyword.args
                 FROM collection_table as collection
                 JOIN keyword_table as keyword
                 WHERE collection.collection_id == keyword.collection_id
                 AND keyword.name like ?
                 ORDER by collection.name, keyword.name
             """
        cursor = self.db.execute(query)
        result = [(row[0], row[1], row[2], row[3], row[4])
                  for row in cursor]
        return list(set(result))

    def reset(self):
        """Remove all data from the database, but leave the tables intact"""
        self.db.execute(self.keywords.delete())
        self.db.execute(self.collections.delete())

    def _looks_like_library_file(self, name):
        return name.endswith(".py")

    def _looks_like_libdoc_file(self, name):
        """Return true if an xml file looks like a libdoc file"""
        # inefficient since we end up reading the file twice,
        # but it's fast enough for our purposes, and prevents
        # us from doing a full parse of files that are obviously
        # not libdoc files
        if name.lower().endswith(".xml"):
            with open(name, "r") as f:
                # read the first few lines; if we don't see
                # what looks like libdoc data, return false
                data = f.read(200)
                index = data.lower().find("<keywordspec ")
                if index > 0:
                    return True
        return False

    def _looks_like_resource_file(self, name):
        """Return true if the file has a keyword table but not a testcase table"""
        # inefficient since we end up reading the file twice,
        # but it's fast enough for our purposes, and prevents
        # us from doing a full parse of files that are obviously
        # not robot files

        if re.search(r'__init__.(txt|robot|html|tsv)$', name):
            # These are initialize files, not resource files
            return False

        found_keyword_table = False
        if (name.lower().endswith(".robot") or
                name.lower().endswith(".txt") or
                name.lower().endswith(".tsv")):

            with open(name, "r") as f:
                data = f.read()
                for match in re.finditer(r'^\*+\s*(Test Cases?|(?:User )?Keywords?)',
                                         data, re.MULTILINE | re.IGNORECASE):
                    if re.match(r'Test Cases?', match.group(1), re.IGNORECASE):
                        # if there's a test case table, it's not a keyword file
                        return False

                    if (not found_keyword_table and
                            re.match(r'(User )?Keywords?', match.group(1), re.IGNORECASE)):
                        found_keyword_table = True
        return found_keyword_table

    def _should_ignore(self, name):
        """Return True if a given library name should be ignored

        This is necessary because not all files we find in the library
        folder are libraries. I wish there was a public robot API
        for "give me a list of installed libraries"...
        """
        _name = name.lower()
        return (_name.startswith("deprecated") or
                _name.startswith("_") or
                _name in ("remote", "reserved", "Easter", 
                          "dialogs_py", "dialogs_ipy", "dialogs_jy"))

    def _add_keyword(self, collection_id, name, doc, args):
        """Insert data into the keyword table

        'args' should be a list, but since we can't store a list in an
        sqlite database we'll make it json we can can convert it back
        to a list later.
        """
        argstring = json.dumps(args)
        insert = self.keywords.insert() \
            .values(collection_id=collection_id, name=name, doc=doc, args=argstring)
        self.db.execute(insert)

    def _create_db(self):
        self._metadata = MetaData()
        self.collections = Table("collections", self._metadata,
                                 Column("collection_id", Integer, Sequence('collection_id_seq'), primary_key=True),
                                 Column('name', Text, index=True),
                                 Column('type', Text),
                                 Column('version', Text),
                                 Column('scope', Text),
                                 Column('namedargs', Text),
                                 Column('path', Text),
                                 Column('doc', Text),
                                 Column('doc_format', Text)
                                 )
        self.keywords = Table("keywords", self._metadata,
                              Column("keyword_id", Integer, Sequence('keyword_id_seq'), primary_key=True),
                              Column('name', Text, index=True),
                              Column('collection_id', Integer, ForeignKey('collections.collection_id')),
                              Column('doc', Text),
                              Column('args', Text)
                              )
        self._metadata.create_all(bind=self._engine)

    def _glob_to_sql(self, string):
        """Convert glob-like wildcards to SQL wildcards

        * becomes %
        ? becomes _
        % becomes \%
        \\ remains \\
        \* remains \*
        \? remains \?

        This also adds a leading and trailing %, unless the pattern begins with
        ^ or ends with $
        """

        # What's with the chr(1) and chr(2) nonsense? It's a trick to
        # hide \* and \? from the * and ? substitutions. This trick
        # depends on the substitutions being done in order.  chr(1)
        # and chr(2) were picked because I know those characters
        # almost certainly won't be in the input string
        table = ((r'\\', chr(1)), (r'\*', chr(2)), (r'\?', chr(3)),
                 (r'%', r'\%'), (r'?', '_'), (r'*', '%'),
                 (chr(1), r'\\'), (chr(2), r'\*'), (chr(3), r'\?'))

        for (a, b) in table:
            string = string.replace(a, b)

        string = string[1:] if string.startswith("^") else "%" + string
        string = string[:-1] if string.endswith("$") else string + "%"

        return string
