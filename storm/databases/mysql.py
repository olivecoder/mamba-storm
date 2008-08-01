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
from datetime import time, timedelta
from array import array
import sys

from storm.databases import dummy

try:
    import MySQLdb
    import MySQLdb.converters
except ImportError:
    MySQLdb = dummy

from storm.expr import (
    compile, Insert, Select, compile_select, Undef, And, Eq,
    SQLRaw, SQLToken, is_safe_token)
from storm.variables import Variable
from storm.database import Database, Connection, Result
from storm.exceptions import (
    install_exceptions, DatabaseModuleError, OperationalError)


install_exceptions(MySQLdb)


compile = compile.create_child()

@compile.when(Select)
def compile_select_mysql(compile, select, state):
    if select.offset is not Undef and select.limit is Undef:
        select.limit = sys.maxint
    return compile_select(compile, select, state)

@compile.when(SQLToken)
def compile_sql_token_mysql(compile, expr, state):
    """MySQL uses ` as the escape character by default."""
    if is_safe_token(expr) and not compile.is_reserved_word(expr):
        return expr
    return '`%s`' % expr.replace('`', '``')


class MySQLResult(Result):

    def get_insert_identity(self, primary_key, primary_variables):
        equals = []
        for column, variable in zip(primary_key, primary_variables):
            if variable.is_defined():
                equals.append(Eq(column, variable))
        return And(*equals)

    @staticmethod
    def from_database(row):
        """Convert MySQL-specific datatypes to "normal" Python types.

        If there are any C{array} instances in the row, convert them
        to strings.
        """
        for value in row:
            if isinstance(value, array):
                yield value.tostring()
            else:
                yield value


class MySQLConnection(Connection):

    result_factory = MySQLResult
    param_mark = "%s"
    compile = compile

    def execute(self, statement, params=None, noresult=False):
        if (isinstance(statement, Insert) and
            statement.primary_variables is not Undef):

            result = Connection.execute(self, statement, params)

            # The lastrowid value will be non-NULL under the following
            # conditions:
            #  - the table had an AUTO INCREMENT column
            #  - the column was not set during the insert, or set to 0
            #
            # If these conditions are met, then lastrowid will be the
            # value of the first such column set.  We assume that it
            # is the first undefined primary key variable.
            if result._raw_cursor.lastrowid:
                for variable in statement.primary_variables:
                    if not variable.is_defined():
                        variable.set(result._raw_cursor.lastrowid)
                        break
            if noresult:
                result = None
            return result
        return Connection.execute(self, statement, params, noresult)

    def to_database(self, params):
        for param in params:
            if isinstance(param, Variable):
                param = param.get(to_db=True)
            if isinstance(param, timedelta):
                yield str(param)
            else:
                yield param

    def is_disconnection_error(self, exc):
        # http://dev.mysql.com/doc/refman/5.0/en/gone-away.html
        return (isinstance(exc, OperationalError) and
                exc.args[0] in (2006, 2013)) # (SERVER_GONE_ERROR, SERVER_LOST)


class MySQL(Database):

    connection_factory = MySQLConnection
    _converters = None

    def __init__(self, uri):
        if MySQLdb is dummy:
            raise DatabaseModuleError("'MySQLdb' module not found")
        self._connect_kwargs = {}
        if uri.database is not None:
            self._connect_kwargs["db"] = uri.database
        if uri.host is not None:
            self._connect_kwargs["host"] = uri.host
        if uri.port is not None:
            self._connect_kwargs["port"] = uri.port
        if uri.username is not None:
            self._connect_kwargs["user"] = uri.username
        if uri.password is not None:
            self._connect_kwargs["passwd"] = uri.password
        for option in ["unix_socket"]:
            if option in uri.options:
                self._connect_kwargs[option] = uri.options.get(option)

        if self._converters is None:
            # MySQLdb returns a timedelta by default on TIME fields.
            converters = MySQLdb.converters.conversions.copy()
            converters[MySQLdb.converters.FIELD_TYPE.TIME] = _convert_time
            self.__class__._converters = converters

        self._connect_kwargs["conv"] = self._converters
        self._connect_kwargs["use_unicode"] = True
        self._connect_kwargs["charset"] = uri.options.get("charset", "utf8")

    def raw_connect(self):
        raw_connection = MySQLdb.connect(**self._connect_kwargs)

        # Here is another sad story about bad transactional behavior.  MySQL
        # offers a feature to automatically reconnect dropped connections.
        # What sounds like a dream, is actually a nightmare for anyone who
        # is dealing with transactions.  When a reconnection happens, the
        # currently running transaction is transparently rolled back, and
        # everything that was being done is lost, without notice.  Not only
        # that, but the connection may be put back in AUTOCOMMIT mode, even
        # when that's not the default MySQLdb behavior.  The MySQL developers
        # quickly understood that this is a terrible idea, and removed the
        # behavior in MySQL 5.0.3.  Unfortunately, Debian and Ubuntu still
        # have a patch for the MySQLdb module which *reenables* that
        # behavior by default even past version 5.0.3 of MySQL.
        #
        # Some links:
        #   http://dev.mysql.com/doc/refman/5.0/en/auto-reconnect.html
        #   http://dev.mysql.com/doc/refman/5.0/en/mysql-reconnect.html
        #   http://dev.mysql.com/doc/refman/5.0/en/gone-away.html
        #
        # What we do here is to explore something that is a very weird
        # side-effect, discovered by reading the code.  When we call the
        # ping() with a False argument, the automatic reconnection is
        # disabled in a *permanent* way for this connection.  The argument
        # to ping() is new in 1.2.2, though.
        if MySQLdb.version_info >= (1, 2, 2):
            raw_connection.ping(False)

        return raw_connection


create_from_uri = MySQL


def _convert_time(time_str):
    h, m, s = time_str.split(":")
    if "." in s:
        f = float(s)
        s = int(f)
        return time(int(h), int(m), s, (f-s)*1000000)
    return time(int(h), int(m), int(s), 0)
