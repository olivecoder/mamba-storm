# -*- encoding: utf-8 -*-
#
# Copyright (c) 2006, 2007 Canonical
#
# Written by Gustavo Niemeyer <gustavo@niemeyer.net>
#
# This file is part of Storm Object Relational Mapper.
#
# Storm is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation; either version 2.1 of
# the License, or (at your option) any later version.
#
# Storm is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
from datetime import datetime, date, time, timedelta
import cPickle as pickle
import shutil
import sys
import os

from storm.uri import URI
from storm.expr import Select, Column, Undef, SQLToken, SQLRaw, Count, Alias
from storm.variables import (Variable, PickleVariable, RawStrVariable,
                             DecimalVariable, DateTimeVariable, DateVariable,
                             TimeVariable, TimeDeltaVariable)
from storm.database import *
from storm.event import EventSystem
from storm.exceptions import (
    DatabaseModuleError, DisconnectionError, OperationalError)

from tests.databases.proxy import ProxyTCPServer
from tests.helper import MakePath


class Marker(object):
    pass

marker = Marker()


class DatabaseTest(object):

    supports_microseconds = True

    def setUp(self):
        super(DatabaseTest, self).setUp()
        self.create_database()
        self.create_connection()
        self.drop_tables()
        self.create_tables()
        self.create_sample_data()

    def tearDown(self):
        self.drop_sample_data()
        self.drop_tables()
        self.drop_connection()
        self.drop_database()
        super(DatabaseTest, self).tearDown()

    def create_database(self):
        raise NotImplementedError

    def create_connection(self):
        self.connection = self.database.connect()

    def create_tables(self):
        raise NotImplementedError

    def create_sample_data(self):
        self.connection.execute("INSERT INTO number VALUES (1, 2, 3)")
        self.connection.execute("INSERT INTO test VALUES (10, 'Title 10')")
        self.connection.execute("INSERT INTO test VALUES (20, 'Title 20')")
        self.connection.commit()

    def drop_sample_data(self):
        pass

    def drop_tables(self):
        for table in ["number", "test", "datetime_test", "bin_test"]:
            try:
                self.connection.execute("DROP TABLE " + table)
                self.connection.commit()
            except:
                self.connection.rollback()

    def drop_connection(self):
        self.connection.close()

    def drop_database(self):
        pass

    def test_create(self):
        self.assertTrue(isinstance(self.database, Database))
        
    def test_connection(self):
        self.assertTrue(isinstance(self.connection, Connection))

    def test_rollback(self):
        self.connection.execute("INSERT INTO test VALUES (30, 'Title 30')")
        self.connection.rollback()
        result = self.connection.execute("SELECT id FROM test WHERE id=30")
        self.assertFalse(result.get_one())

    def test_rollback_twice(self):
        self.connection.execute("INSERT INTO test VALUES (30, 'Title 30')")
        self.connection.rollback()
        self.connection.rollback()
        result = self.connection.execute("SELECT id FROM test WHERE id=30")
        self.assertFalse(result.get_one())

    def test_commit(self):
        self.connection.execute("INSERT INTO test VALUES (30, 'Title 30')")
        self.connection.commit()
        self.connection.rollback()
        result = self.connection.execute("SELECT id FROM test WHERE id=30")
        self.assertTrue(result.get_one())

    def test_commit_twice(self):
        self.connection.execute("INSERT INTO test VALUES (30, 'Title 30')")
        self.connection.commit()
        self.connection.commit()
        result = self.connection.execute("SELECT id FROM test WHERE id=30")
        self.assertTrue(result.get_one())

    def test_execute_result(self):
        result = self.connection.execute("SELECT 1")
        self.assertTrue(isinstance(result, Result))
        self.assertTrue(result.get_one())

    def test_execute_unicode_result(self):
        result = self.connection.execute(u"SELECT title FROM test")
        self.assertTrue(isinstance(result, Result))
        row = result.get_one()
        self.assertEquals(row, ("Title 10",))
        self.assertTrue(isinstance(row[0], unicode))

    def test_execute_params(self):
        result = self.connection.execute("SELECT one FROM number "
                                         "WHERE 1=?", (1,))
        self.assertTrue(result.get_one())
        result = self.connection.execute("SELECT one FROM number "
                                         "WHERE 1=?", (2,))
        self.assertFalse(result.get_one())

    def test_execute_empty_params(self):
        result = self.connection.execute("SELECT one FROM number", ())
        self.assertTrue(result.get_one())

    def test_execute_expression(self):
        result = self.connection.execute(Select(1))
        self.assertTrue(result.get_one(), (1,))

    def test_execute_expression_empty_params(self):
        result = self.connection.execute(Select(SQLRaw("1")))
        self.assertTrue(result.get_one(), (1,))

    def test_get_one(self):
        result = self.connection.execute("SELECT * FROM test ORDER BY id")
        self.assertEquals(result.get_one(), (10, "Title 10"))

    def test_get_all(self):
        result = self.connection.execute("SELECT * FROM test ORDER BY id")
        self.assertEquals(result.get_all(),
                          [(10, "Title 10"), (20, "Title 20")])

    def test_iter(self):
        result = self.connection.execute("SELECT * FROM test ORDER BY id")
        self.assertEquals([item for item in result],
                          [(10, "Title 10"), (20, "Title 20")])

    def test_simultaneous_iter(self):
        result1 = self.connection.execute("SELECT * FROM test "
                                          "ORDER BY id ASC")
        result2 = self.connection.execute("SELECT * FROM test "
                                          "ORDER BY id DESC")
        iter1 = iter(result1)
        iter2 = iter(result2)
        self.assertEquals(iter1.next(), (10, "Title 10"))
        self.assertEquals(iter2.next(), (20, "Title 20"))
        self.assertEquals(iter1.next(), (20, "Title 20"))
        self.assertEquals(iter2.next(), (10, "Title 10"))
        self.assertRaises(StopIteration, iter1.next)
        self.assertRaises(StopIteration, iter2.next)

    def test_get_insert_identity(self):
        result = self.connection.execute("INSERT INTO test (title) "
                                         "VALUES ('Title 30')")
        primary_key = (Column("id", SQLToken("test")),)
        primary_variables = (Variable(),)
        expr = result.get_insert_identity(primary_key, primary_variables)
        select = Select(Column("title", SQLToken("test")), expr)
        result = self.connection.execute(select)
        self.assertEquals(result.get_one(), ("Title 30",))

    def test_get_insert_identity_composed(self):
        result = self.connection.execute("INSERT INTO test (title) "
                                         "VALUES ('Title 30')")
        primary_key = (Column("id", SQLToken("test")),
                       Column("title", SQLToken("test")))
        primary_variables = (Variable(), Variable("Title 30"))
        expr = result.get_insert_identity(primary_key, primary_variables)
        select = Select(Column("title", SQLToken("test")), expr)
        result = self.connection.execute(select)
        self.assertEquals(result.get_one(), ("Title 30",))

    def test_datetime(self):
        value = datetime(1977, 4, 5, 12, 34, 56, 78)
        self.connection.execute("INSERT INTO datetime_test (dt) VALUES (?)",
                                (value,))
        result = self.connection.execute("SELECT dt FROM datetime_test")
        variable = DateTimeVariable()
        result.set_variable(variable, result.get_one()[0])
        if not self.supports_microseconds:
            value = value.replace(microsecond=0)
        self.assertEquals(variable.get(), value)

    def test_date(self):
        value = date(1977, 4, 5)
        self.connection.execute("INSERT INTO datetime_test (d) VALUES (?)",
                                (value,))
        result = self.connection.execute("SELECT d FROM datetime_test")
        variable = DateVariable()
        result.set_variable(variable, result.get_one()[0])
        self.assertEquals(variable.get(), value)

    def test_time(self):
        value = time(12, 34, 56, 78)
        self.connection.execute("INSERT INTO datetime_test (t) VALUES (?)",
                                (value,))
        result = self.connection.execute("SELECT t FROM datetime_test")
        variable = TimeVariable()
        result.set_variable(variable, result.get_one()[0])
        if not self.supports_microseconds:
            value = value.replace(microsecond=0)
        self.assertEquals(variable.get(), value)

    def test_timedelta(self):
        value = timedelta(12, 34, 56)
        self.connection.execute("INSERT INTO datetime_test (td) VALUES (?)",
                                (value,))
        result = self.connection.execute("SELECT td FROM datetime_test")
        variable = TimeDeltaVariable()
        result.set_variable(variable, result.get_one()[0])
        self.assertEquals(variable.get(), value)

    def test_pickle(self):
        value = {"a": 1, "b": 2}
        value_dump = pickle.dumps(value, -1)
        self.connection.execute("INSERT INTO bin_test (b) VALUES (?)",
                                (value_dump,))
        result = self.connection.execute("SELECT b FROM bin_test")
        variable = PickleVariable()
        result.set_variable(variable, result.get_one()[0])
        self.assertEquals(variable.get(), value)

    def test_binary(self):
        """Ensure database works with high bits and embedded zeros."""
        value = "\xff\x00\xff\x00"
        self.connection.execute("INSERT INTO bin_test (b) VALUES (?)",
                                (value,))
        result = self.connection.execute("SELECT b FROM bin_test")
        variable = RawStrVariable()
        result.set_variable(variable, result.get_one()[0])
        self.assertEquals(variable.get(), value)

    def test_binary_ascii(self):
        """Some databases like pysqlite2 may return unicode for strings."""
        self.connection.execute("INSERT INTO bin_test VALUES (10, 'Value')")
        result = self.connection.execute("SELECT b FROM bin_test")
        variable = RawStrVariable()
        # If the following doesn't raise a TypeError we're good.
        result.set_variable(variable, result.get_one()[0])
        self.assertEquals(variable.get(), "Value")

    def test_order_by_group_by(self):
        self.connection.execute("INSERT INTO test VALUES (100, 'Title 10')")
        self.connection.execute("INSERT INTO test VALUES (101, 'Title 10')")
        id = Column("id", "test")
        title = Column("title", "test")
        expr = Select(Count(id), group_by=title, order_by=Count(id))
        result = self.connection.execute(expr)
        self.assertEquals(result.get_all(), [(1,), (3,)])

    def test_set_decimal_variable_from_str_column(self):
        self.connection.execute("INSERT INTO test VALUES (40, '40.5')")
        variable = DecimalVariable()
        result = self.connection.execute("SELECT title FROM test WHERE id=40")
        result.set_variable(variable, result.get_one()[0])

    def test_get_decimal_variable_to_str_column(self):
        variable = DecimalVariable()
        variable.set("40.5", from_db=True)
        self.connection.execute("INSERT INTO test VALUES (40, ?)", (variable,))
        result = self.connection.execute("SELECT title FROM test WHERE id=40")
        self.assertEquals(result.get_one()[0], "40.5")

    def test_quoting(self):
        # FIXME "with'quote" should be in the list below, but it doesn't
        #       work because it breaks the parameter mark translation.
        for reserved_name in ["with space", 'with`"escape', "SELECT"]:
            reserved_name = SQLToken(reserved_name)
            expr = Select(reserved_name,
                          tables=Alias(Select(Alias(1, reserved_name))))
            result = self.connection.execute(expr)
            self.assertEquals(result.get_one(), (1,))

    def test_concurrent_behavior(self):
        """The default behavior should be to handle transactions in isolation.

        Data committed in one transaction shouldn't be visible to another
        running transaction before the later is committed or aborted.  If
        this isn't the case, the caching made by Storm (or by anything
        that works with data in memory, in fact) becomes a dangerous thing.
 
        For PostgreSQL, isolation level must be SERIALIZABLE.
        For MySQL, isolation level must be REPEATABLE READ (the default),
        and the InnoDB engine must be in use.
        For SQLite, the isolation level already is SERIALIZABLE when not
        in autocommit mode.  OTOH, PySQLite is nuts regarding transactional
        behavior, and will easily offer READ COMMITTED behavior inside a
        "transaction" (it didn't tell SQLite to open a transaction, in fact).
        """
        connection1 = self.connection
        connection2 = self.database.connect()
        try:
            result = connection1.execute("SELECT title FROM test WHERE id=10")
            self.assertEquals(result.get_one(), ("Title 10",))
            try:
                connection2.execute("UPDATE test SET title='Title 100' "
                                    "WHERE id=10")
                connection2.commit()
            except OperationalError, e:
                self.assertEquals(str(e), "database is locked") # SQLite blocks
            result = connection1.execute("SELECT title FROM test WHERE id=10")
            self.assertEquals(result.get_one(), ("Title 10",))
        finally:
            connection1.rollback()

    def test_connect_sets_event_system(self):
        connection = self.database.connect(marker)
        self.assertEqual(connection._event, marker)

    def test_execute_sends_event(self):
        event = EventSystem(marker)
        calls = []
        def register_transaction(owner):
            calls.append(owner)
        event.hook("register-transaction", register_transaction)

        connection = self.database.connect(event)
        connection.execute("SELECT 1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], marker)

    def from_database(self, row):
        return [int(item)+1 for item in row]

    def test_wb_result_get_one_goes_through_from_database(self):
        result = self.connection.execute("SELECT one, two FROM number")
        result.from_database = self.from_database
        self.assertEquals(result.get_one(), (2, 3))

    def test_wb_result_get_all_goes_through_from_database(self):
        result = self.connection.execute("SELECT one, two FROM number")
        result.from_database = self.from_database
        self.assertEquals(result.get_all(), [(2, 3)])

    def test_wb_result_iter_goes_through_from_database(self):
        result = self.connection.execute("SELECT one, two FROM number")
        result.from_database = self.from_database
        self.assertEquals(iter(result).next(), (2, 3))


class UnsupportedDatabaseTest(object):
    
    helpers = [MakePath]

    dbapi_module_names = []
    db_module_name = None

    def test_exception_when_unsupported(self):

        # Install a directory in front of the search path.
        module_dir = self.make_path()
        os.mkdir(module_dir)
        sys.path.insert(0, module_dir)

        # Copy the real module over to a new place, since the old one is
        # already using the real module, if it's available.
        db_module = __import__("storm.databases."+self.db_module_name,
                               None, None, [""])
        db_module_filename = db_module.__file__
        if db_module_filename.endswith(".pyc"):
            db_module_filename = db_module_filename[:-1]
        shutil.copyfile(db_module_filename,
                        os.path.join(module_dir, "_fake_.py"))

        dbapi_modules = {}
        for dbapi_module_name in self.dbapi_module_names:

            # If the real module is available, remove it from sys.modules.
            dbapi_module = sys.modules.pop(dbapi_module_name, None)
            if dbapi_module is not None:
                dbapi_modules[dbapi_module_name] = dbapi_module

            # Create a module which raises ImportError when imported, to fake
            # a missing module.
            dirname = self.make_path(path=os.path.join(module_dir,
                                                       dbapi_module_name))
            os.mkdir(dirname)
            self.make_path("raise ImportError",
                           os.path.join(module_dir, dbapi_module_name,
                                        "__init__.py"))

        # Finally, test it.
        import _fake_
        uri = URI("_fake_://db")

        try:
            self.assertRaises(DatabaseModuleError,
                              _fake_.create_from_uri, uri)
        finally:
            # Unhack the environment.
            del sys.path[0]
            del sys.modules["_fake_"]

            sys.modules.update(dbapi_modules)


class DatabaseDisconnectionTest(object):

    environment_variable = ""
    host_environment_variable = ""
    default_port = None

    def setUp(self):
        super(DatabaseDisconnectionTest, self).setUp()
        self.create_database_and_proxy()
        self.create_connection()

    def tearDown(self):
        self.drop_database()
        self.proxy.close()
        super(DatabaseDisconnectionTest, self).tearDown()

    def is_supported(self):
        return bool(self.get_uri())

    def get_uri(self):
        """Return URI instance with a defined host and port."""
        if not self.environment_variable or not self.default_port:
            raise RuntimeError("Define at least %s.environment_variable and "
                               "%s.default_port" % (type(self).__name__,
                                                    type(self).__name__))
        uri_str = os.environ.get(self.host_environment_variable)
        if uri_str:
            uri = URI(uri_str)
            if not uri.host:
                raise RuntimeError("The URI in %s must include a host." %
                                   self.host_environment_variable)
            if not uri.port:
                uri.port = self.default_port
            return uri
        else:
            uri_str = os.environ.get(self.environment_variable)
            if uri_str:
                uri = URI(uri_str)
                if uri.host:
                    if not uri.port:
                        uri.port = self.default_port
                    return uri
        return None

    def create_database_and_proxy(self):
        """Set up the TCP proxy and database object.

        The TCP proxy should forward requests on to the database.  The
        database object should point at the TCP proxy.
        """
        uri = self.get_uri()
        self.proxy = ProxyTCPServer((uri.host, uri.port))
        uri.host, uri.port = self.proxy.server_address
        self.database = create_database(uri)

    def create_connection(self):
        self.connection = self.database.connect()

    def drop_database(self):
        pass

    def test_proxy_works(self):
        """Ensure that we can talk to the database through the proxy."""
        result = self.connection.execute("SELECT 1")
        self.assertEqual(result.get_one(), (1,))

    def test_catch_disconnect_on_execute(self):
        """Test that database disconnections get caught on execute()."""
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())
        self.proxy.restart()
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")

    def test_catch_disconnect_on_commit(self):
        """Test that database disconnections get caught on commit()."""
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())
        self.proxy.restart()
        self.assertRaises(DisconnectionError, self.connection.commit)

    def test_connection_stays_disconnected_in_transaction(self):
        """Test that connection does not immediately reconnect."""
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())
        self.proxy.restart()
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")

    def test_reconnect_after_rollback(self):
        """Test that we reconnect after rolling back the connection."""
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())
        self.proxy.restart()
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")
        self.connection.rollback()
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())

    def test_catch_disconnect_on_reconnect(self):
        """Test that reconnection failures result in DisconnectionError."""
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())
        self.proxy.stop()
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")
        # Rollback the connection, but because the proxy is still
        # down, we get a DisconnectionError again.
        self.connection.rollback()
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")

    def test_close_connection_after_disconnect(self):
        result = self.connection.execute("SELECT 1")
        self.assertTrue(result.get_one())
        self.proxy.stop()
        self.assertRaises(DisconnectionError,
                          self.connection.execute, "SELECT 1")
        self.connection.close()
