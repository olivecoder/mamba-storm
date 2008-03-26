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
"""A SQLObject emulation layer for Storm.

L{SQLObjectBase} is the central point of compatibility.
"""

import re
import warnings

from storm.properties import (
    RawStr, Int, Bool, Float, DateTime, Date, TimeDelta)
from storm.references import Reference, ReferenceSet
from storm.properties import SimpleProperty, PropertyPublisherMeta
from storm.variables import Variable
from storm.exceptions import StormError
from storm.info import get_cls_info
from storm.store import AutoReload, Store
from storm.base import Storm
from storm.expr import (
    SQL, SQLRaw, Desc, And, Or, Not, In, Like, AutoTables, LeftJoin)
from storm.tz import tzutc
from storm import Undef


__all__ = ["SQLObjectBase", "StringCol", "IntCol", "BoolCol", "FloatCol",
           "DateCol", "UtcDateTimeCol", "IntervalCol", "ForeignKey",
           "SQLMultipleJoin", "SQLRelatedJoin", "SingleJoin", "DESC",
           "AND", "OR", "NOT", "IN", "LIKE", "SQLConstant",
           "SQLObjectNotFound", "CONTAINSSTRING"]


DESC, AND, OR, NOT, IN, LIKE, SQLConstant = Desc, And, Or, Not, In, Like, SQL

_IGNORED = object()


class SQLObjectNotFound(StormError):
    pass


class SQLObjectStyle(object):

    longID = False

    def idForTable(self, table_name):
        if self.longID:
            return self.tableReference(table_name)
        else:
            return "id"

    def pythonClassToAttr(self, class_name):
        return self._lowerword(class_name)

    def instanceAttrToIDAttr(self, attr_name):
        return attr_name + "ID"

    def pythonAttrToDBColumn(self, attr_name):
        return self._mixed_to_under(attr_name)

    def dbColumnToPythonAttr(self, column_name):
        return self._under_to_mixed(column_name)

    def pythonClassToDBTable(self, class_name):
        return class_name[0].lower()+self._mixed_to_under(class_name[1:])

    def dbTableToPythonClass(self, table_name):
        return table_name[0].upper()+self._under_to_mixed(table_name[1:])

    def pythonClassToDBTableReference(self, class_name):
        return self.tableReference(self.pythonClassToDBTable(class_name))

    def tableReference(self, table_name):
        return table_name+"_id"

    def _mixed_to_under(self, name, _re=re.compile("[A-Z]+")):
        if name.endswith("ID"):
            return self._mixed_to_under(name[:-2]+"_id")
        name = _re.sub(self._mixed_to_under_sub, name)
        if name.startswith("_"):
            return name[1:]
        return name

    def _mixed_to_under_sub(self, match):
        m = match.group(0).lower()
        if len(m) > 1:
            return "_%s_%s" % (m[:-1], m[-1])
        else:
            return "_%s" % m

    def _under_to_mixed(self, name, _re=re.compile("_.")):
        if name.endswith("_id"):
            return self._under_to_mixed(name[:-3] + "ID")
        return _re.sub(self._under_to_mixed_sub, name)

    def _under_to_mixed_sub(self, match):
        return match.group(0)[1].upper()

    @staticmethod
    def _capword(s):
        return s[0].upper() + s[1:]

    @staticmethod
    def _lowerword(s):
        return s[0].lower() + s[1:]


class SQLObjectMeta(PropertyPublisherMeta):

    @staticmethod
    def _get_attr(attr, bases, dict):
        value = dict.get(attr)
        if value is None:
            for base in bases:
                value = getattr(base, attr, None)
                if value is not None:
                    break
        return value

    def __new__(cls, name, bases, dict):
        if Storm in bases or SQLObjectBase in bases:
            # Do not parse abstract base classes.
            return type.__new__(cls, name, bases, dict)

        style = cls._get_attr("_style", bases, dict)
        if style is None:
            dict["_style"] = style = SQLObjectStyle()

        table_name = cls._get_attr("_table", bases, dict)
        if table_name is None:
            table_name = style.pythonClassToDBTable(name)

        id_name = cls._get_attr("_idName", bases, dict)
        if id_name is None:
            id_name = style.idForTable(table_name)

        # Handle this later to call _parse_orderBy() on the created class.
        default_order = cls._get_attr("_defaultOrder", bases, dict)

        dict["__storm_table__"] = table_name

        attr_to_prop = {}
        for attr, prop in dict.items():
            attr_to_prop[attr] = attr
            if isinstance(prop, ForeignKey):
                db_name = prop.kwargs.get("dbName", attr)
                local_prop_name = style.instanceAttrToIDAttr(attr)
                dict[local_prop_name] = local_prop = Int(db_name)
                dict[attr] = Reference(local_prop,
                                       "%s.<primary key>" % prop.foreignKey)
                attr_to_prop[attr] = local_prop_name
            elif isinstance(prop, PropertyAdapter):
                db_name = prop.dbName or attr
                method_name = prop.alternateMethodName
                if method_name is None and prop.alternateID:
                    method_name = "by" + db_name[0].upper() + db_name[1:]
                if method_name is not None:
                    def func(cls, key, attr=attr):
                        store = cls._get_store()
                        obj = store.find(cls, getattr(cls, attr) == key).one()
                        if obj is None:
                            raise SQLObjectNotFound
                        return obj
                    func.func_name = method_name
                    dict[method_name] = classmethod(func)
            elif isinstance(prop, SQLMultipleJoin):
                # Generate addFoo/removeFoo names.
                def define_add_remove(dict, prop):
                    capitalised_name = (prop._otherClass[0].capitalize() +
                                        prop._otherClass[1:])
                    def add(self, obj):
                        prop._get_bound_reference_set(self).add(obj)
                    add.__name__ = "add" + capitalised_name
                    dict.setdefault(add.__name__, add)

                    def remove(self, obj):
                        prop._get_bound_reference_set(self).remove(obj)
                    remove.__name__ = "remove" + capitalised_name
                    dict.setdefault(remove.__name__, remove)
                define_add_remove(dict, prop)


        id_type = dict.setdefault("_idType", int)
        id_cls = {int: Int, str: RawStr, unicode: AutoUnicode}[id_type]
        dict["id"] = id_cls(id_name, primary=True, default=AutoReload)
        attr_to_prop[id_name] = "id"

        # Notice that obj is the class since this is the metaclass.
        obj = super(SQLObjectMeta, cls).__new__(cls, name, bases, dict)

        property_registry = obj._storm_property_registry

        property_registry.add_property(obj, getattr(obj, "id"),
                                       "<primary key>")

        # Let's explore this same mechanism to register table names,
        # so that we can find them to handle prejoinClauseTables.
        property_registry.add_property(obj, getattr(obj, "id"),
                                       "<table %s>" % table_name)

        for fake_name, real_name in attr_to_prop.items():
            prop = getattr(obj, real_name)
            if fake_name != real_name:
                property_registry.add_property(obj, prop, fake_name)
            attr_to_prop[fake_name] = prop

        obj._attr_to_prop = attr_to_prop

        if default_order is not None:
            cls_info = get_cls_info(obj)
            cls_info.default_order = obj._parse_orderBy(default_order)

        return obj


class DotQ(object):
    """A descriptor that mimics the SQLObject 'Table.q' syntax"""

    def __get__(self, obj, cls=None):
        return BoundDotQ(cls)


class BoundDotQ(object):

    def __init__(self, cls):
        self._cls = cls

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        elif attr == "id":
            cls_info = get_cls_info(self._cls)
            return cls_info.primary_key[0]
        else:
            return getattr(self._cls, attr)


class SQLObjectBase(Storm):
    """The root class of all SQLObject-emulating classes in your application.

    The general strategy for using Storm's SQLObject emulation layer
    is to create an application-specific subclass of SQLObjectBase
    (probably named "SQLObject") that provides an implementation of
    _get_store to return an instance of L{storm.store.Store}. It may
    even be implemented as returning a global L{Store} instance. Then
    all database classes should subclass that class.
    """
    __metaclass__ = SQLObjectMeta

    q = DotQ()

    def __init__(self, *args, **kwargs):
        self._get_store().add(self)
        self._create(None, **kwargs)

    def __storm_loaded__(self):
        self._init(None)

    def _init(self, id, *args, **kwargs):
        pass

    def _create(self, _id_, **kwargs):
        self.set(**kwargs)
        self._init(None)

    def set(self, **kwargs):
        for attr, value in kwargs.iteritems():
            setattr(self, attr, value)

    def destroySelf(self):
        Store.of(self).remove(self)

    @staticmethod
    def _get_store():
        raise NotImplementedError("SQLObjectBase._get_store() "
                                  "must be implemented")

    @classmethod
    def delete(cls, id):
        # destroySelf() should be extended to support cascading, so
        # we'll mimic what SQLObject does here, even if more expensive.
        obj = cls.get(id)
        obj.destroySelf()

    @classmethod
    def get(cls, id):
        id = cls._idType(id)
        store = cls._get_store()
        obj = store.get(cls, id)
        if obj is None:
            raise SQLObjectNotFound("Object not found")
        return obj

    @classmethod
    def _parse_orderBy(cls, orderBy):
        result = []
        if not isinstance(orderBy, (tuple, list)):
            orderBy = (orderBy,)
        for item in orderBy:
            if isinstance(item, basestring):
                desc = item.startswith("-")
                if desc:
                    item = item[1:]
                item = cls._attr_to_prop.get(item, item)
                if desc:
                    item = Desc(item)
            result.append(item)
        return tuple(result)

    @classmethod
    def select(cls, *args, **kwargs):
        return SQLObjectResultSet(cls, *args, **kwargs)

    @classmethod
    def selectBy(cls, orderBy=None, **kwargs):
        return SQLObjectResultSet(cls, orderBy=orderBy, by=kwargs)

    @classmethod
    def selectOne(cls, *args, **kwargs):
        return SQLObjectResultSet(cls, *args, **kwargs)._one()

    @classmethod
    def selectOneBy(cls, **kwargs):
        return SQLObjectResultSet(cls, by=kwargs)._one()

    @classmethod
    def selectFirst(cls, *args, **kwargs):
        return SQLObjectResultSet(cls, *args, **kwargs)._first()

    @classmethod
    def selectFirstBy(cls, orderBy=None, **kwargs):
        result = SQLObjectResultSet(cls, orderBy=orderBy, by=kwargs)
        return result._first()

    def syncUpdate(self):
        self._get_store().flush()

    def sync(self):
        store = self._get_store()
        store.flush()
        store.autoreload(self)


class SQLObjectResultSet(object):
    """SQLObject-equivalent of the ResultSet class in Storm.

    Storm handles joins in the Store interface, while SQLObject
    does that in the result one.  To offer support for prejoins,
    we can't simply wrap our ResultSet instance, and instead have
    to postpone the actual find until the very last moment.
    """

    def __init__(self, cls, clause=None, clauseTables=None, orderBy=None,
                 limit=None, distinct=None, prejoins=None,
                 prejoinClauseTables=None, selectAlso=None,
                 by={}, prepared_result_set=None, slice=None):
        self._cls = cls
        self._clause = clause
        self._clauseTables = clauseTables
        self._orderBy = orderBy
        self._limit = limit
        self._distinct = distinct
        self._prejoins = prejoins
        self._prejoinClauseTables = prejoinClauseTables
        self._selectAlso = selectAlso

        # Parameters not mapping SQLObject:
        self._by = by
        self._slice = slice
        self._prepared_result_set = prepared_result_set
        self._finished_result_set = None

    def _copy(self, **kwargs):
        copy = self.__class__(self._cls, **kwargs)
        for name, value in self.__dict__.iteritems():
            if name[1:] not in kwargs and name != "_finished_result_set":
                setattr(copy, name, value)
        return copy

    def _prepare_result_set(self):
        store = self._cls._get_store()

        if self._clause is None:
            args = ()
        else:
            args = (self._clause,)

        tables = []

        if self._clauseTables is not None:
            tables.extend(table.lower() for table in self._clauseTables)

        if not (self._prejoins or self._prejoinClauseTables):
            find_spec = self._cls
        else:
            find_spec = [self._cls]

            if self._prejoins:
                for prejoin in self._prejoins:
                    relation = getattr(self._cls, prejoin)._relation
                    tables.append(LeftJoin(relation.remote_cls,
                                           relation.get_where_for_join()))
                    find_spec.append(relation.remote_cls)

            if self._prejoinClauseTables:
                property_registry = self._cls._storm_property_registry
                for table in tables:
                    cls = property_registry.get("<table %s>" % table).cls
                    find_spec.append(cls)

            find_spec = tuple(find_spec)

        if tables:
            # Inject an AutoTables expression with a dummy true value to
            # be ANDed in the WHERE clause, so that we can introduce our
            # tables into the dynamic table handling of Storm without
            # disrupting anything else.
            args += (AutoTables(SQL("1=1"), tables),)

        return store.find(find_spec, *args, **self._by)

    def _finish_result_set(self):
        if self._prepared_result_set is not None:
            result = self._prepared_result_set
        else:
            result = self._prepare_result_set()

        if self._orderBy is not None:
            result.order_by(*self._cls._parse_orderBy(self._orderBy))

        if self._selectAlso is not None:
            result._add_select_also(SQL(self._selectAlso))

        if self._limit is not None or self._distinct is not None:
            result.config(limit=self._limit, distinct=self._distinct)

        if self._slice is not None:
            result = result[self._slice]

        return result

    @property
    def _result_set(self):
        if self._finished_result_set is None:
            self._finished_result_set = self._finish_result_set()
        return self._finished_result_set

    def _one(self):
        """Internal API for the base class."""
        return detuplelize(self._result_set.one())

    def _first(self):
        """Internal API for the base class."""
        return detuplelize(self._result_set.first())

    def __iter__(self):
        for item in self._result_set:
            yield detuplelize(item)

    def __getitem__(self, index):
        if isinstance(index, slice):
            if not index.start and not index.stop:
                return self

            if index.start and index.start < 0 or (
                index.stop and index.stop < 0):
                L = list(self)
                if len(L) > 100:
                    warnings.warn('Negative indices when slicing are slow: '
                                  'fetched %d rows.' % (len(L),))
                start, stop, step = index.indices(len(L))
                assert step == 1, "slice step must be 1"
                index = slice(start, stop)
            return self._copy(slice=index)
        else:
            if index < 0:
                L = list(self)
                if len(L) > 100:
                    warnings.warn('Negative indices are slow: '
                                  'fetched %d rows.' % (len(L),))
                return detuplelize(L[index])
            return detuplelize(self._result_set[index])

    def __nonzero__(self):
        return self._result_set.any() is not None

    def count(self):
        return self._result_set.count()

    def orderBy(self, orderBy):
        return self._copy(orderBy=orderBy)

    def limit(self, limit):
        return self._copy(limit=limit)

    def distinct(self):
        return self._copy(distinct=True, orderBy=None)

    def union(self, otherSelect, unionAll=False, orderBy=None):
        result_set = self._result_set.union(otherSelect._result_set,
                                            all=unionAll)
        result_set.order_by() # Remove default order.
        return self._copy(prepared_result_set=result_set, orderBy=orderBy)

    def except_(self, otherSelect, exceptAll=False, orderBy=None):
        result_set = self._result_set.difference(otherSelect._result_set,
                                                 all=exceptAll)
        result_set.order_by() # Remove default order.
        return self._copy(prepared_result_set=result_set, orderBy=orderBy)

    def intersect(self, otherSelect, intersectAll=False, orderBy=None):
        result_set = self._result_set.intersection(otherSelect._result_set,
                                                   all=intersectAll)
        result_set.order_by() # Remove default order.
        return self._copy(prepared_result_set=result_set, orderBy=orderBy)

    def prejoin(self, prejoins):
        return self._copy(prejoins=prejoins)

    def prejoinClauseTables(self, prejoinClauseTables):
        return self._copy(prejoinClauseTables=prejoinClauseTables)


def detuplelize(item):
    """If item is a tuple, return first element, otherwise the item itself.

    The tuple syntax is used to implement prejoins, so we have to hide from
    the user the fact that more than a single object are being selected at
    once.
    """
    if type(item) is tuple:
        return item[0]
    return item



class PropertyAdapter(object):

    _kwargs = {}

    def __init__(self, dbName=None, notNull=False, default=Undef,
                 alternateID=None, unique=_IGNORED, name=_IGNORED,
                 alternateMethodName=None, length=_IGNORED, immutable=None,
                 prejoins=_IGNORED):
        if default is None and notNull:
            raise RuntimeError("Can't use default=None and notNull=True")

        self.dbName = dbName
        self.alternateID = alternateID
        self.alternateMethodName = alternateMethodName

        # XXX Implement handler for:
        #
        #   - immutable (causes setting the attribute to fail)
        #
        # XXX Implement tests for ignored parameters:
        #
        #   - unique (for tablebuilder)
        #   - length (for tablebuilder for StringCol)
        #   - name (for _columns stuff)
        #   - prejoins

        if callable(default):
            default_factory = default
            default = Undef
        else:
            default_factory = Undef
        super(PropertyAdapter, self).__init__(dbName, allow_none=not notNull,
                                              default_factory=default_factory,
                                              default=default, **self._kwargs)


class AutoUnicodeVariable(Variable):
    """Unlike UnicodeVariable, this will try to convert str to unicode."""

    def parse_set(self, value, from_db):
        if not isinstance(value, basestring):
            raise TypeError("Expected basestring, found %s" % repr(type(value)))
        return unicode(value)

class AutoUnicode(SimpleProperty):
    variable_class = AutoUnicodeVariable


class StringCol(PropertyAdapter, AutoUnicode):
    pass

class IntCol(PropertyAdapter, Int):
    pass

class BoolCol(PropertyAdapter, Bool):
    pass

class FloatCol(PropertyAdapter, Float):
    pass

class UtcDateTimeCol(PropertyAdapter, DateTime):
    _kwargs = {"tzinfo": tzutc()}

class DateCol(PropertyAdapter, Date):
    pass

class IntervalCol(PropertyAdapter, TimeDelta):
    pass


class ForeignKey(object):

    def __init__(self, foreignKey, **kwargs):
        self.foreignKey = foreignKey
        self.kwargs = kwargs


class SQLMultipleJoin(ReferenceSet):

    def __init__(self, otherClass=None, joinColumn=None,
                 intermediateTable=None, otherColumn=None, orderBy=None,
                 prejoins=_IGNORED):
        if intermediateTable:
            args = ("<primary key>",
                    "%s.%s" % (intermediateTable, joinColumn),
                    "%s.%s" % (intermediateTable, otherColumn),
                    "%s.<primary key>" % otherClass)
        else:
            args = ("<primary key>", "%s.%s" % (otherClass, joinColumn))
        ReferenceSet.__init__(self, *args)
        self._orderBy = orderBy
        self._otherClass = otherClass

    def __get__(self, obj, cls=None):
        if obj is None:
            return self
        bound_reference_set = ReferenceSet.__get__(self, obj)
        target_cls = bound_reference_set._target_cls
        result_set = bound_reference_set.find()
        return SQLObjectResultSet(target_cls, prepared_result_set=result_set,
                                  orderBy=self._orderBy)

    def _get_bound_reference_set(self, obj):
        assert obj is not None
        return ReferenceSet.__get__(self, obj)


SQLRelatedJoin = SQLMultipleJoin


class SingleJoin(Reference):

    def __init__(self, otherClass, joinColumn, prejoins=_IGNORED):
        super(SingleJoin, self).__init__(
            "<primary key>", "%s.%s" % (otherClass, joinColumn))


class CONTAINSSTRING(Like):

    def __init__(self, expr, string):
        string = string.replace("!", "!!") \
                       .replace("_", "!_") \
                       .replace("%", "!%")
        Like.__init__(self, expr, "%"+string+"%", SQLRaw("'!'"))
