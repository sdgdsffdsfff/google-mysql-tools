#!/usr/bin/python2.6
#
# Copyright 2011 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A thread-safe interface to a sharded database.

Connect() parses a dbspec and returns a Connection or MultiConnection object.
The objects can both be instantiated directly with args that are passed through
to MySQLdb.connect.  The resulting objects have an Execute() method that
enforces that all shards return the same result, and a MultiExecute() method
that returns a dictionary of results by expanded host name.

Query results are returned as VirtualTable objects, which act like dictionary
cursor return values (lists of dictionaries), though they are stored more
efficiently internally.  Failures are returned as QueryErrors or QueryWarnings
objects, which also act like tables, but with error contents.

Use example:
import db
cb = db.Connect('dbhost{0..9}:dbuser:?:dbname#')
cb.Execute('UPDATE foo SET bar=5 WHERE id=10')
cb.Close()
....
cb.Execute('UPDATE foo SET bar=10 WHERE id=10')
cb.Close()

There is also a SQL syntax extension for selecting from specific shard(s):
cb.Execute('ON SHARD 3,5 SELECT foo FROM bar')
"""

__author__ = 'flamingcow@google.com (Ian Gulliver)'

import getpass
import logging
import os
import Queue
import random
import re
import socket
import threading
import time
import traceback

import thread_tools

import MySQLdb


class Error(Exception):
  pass


class ResolutionError(Error):
  pass


class InconsistentResponses(Error):
  pass


class InconsistentSchema(Error):
  pass


class QueryErrorsException(Error):
  pass


class QueryWarningsException(Error):
  pass


class Spec(dict):
  """Represent a database specification.

  This is a dict of dbargs which can be passed to Connection or MultiConnection.
  """

  # Valid DB types (first part of a 5-part dbspec)
  _DB_TYPES = [
      'mysql',
  ]

  # (user, host) -> password
  _PW_CACHE = {}

  @classmethod
  def Parse(cls, spec, **kwargs):
    """Parse the spec string into a dict of dbargs.

    Several types of dbspec hostnames are supported:
      dbhost3: A single DNS host
      dbhost#: Shard count read from ConfigurationGlobals.NumShards on shard 0
      dbhost0,dbhost1: List of hosts
      dbhost{0..9}: A range of shards that match a DNS name
      socket=/var/run/mysql/mysqld.sock: UNIX socket

    The database part of the dbspec can be:
      dbname3: A single database
      dbname#: Shard substitution from the hostname list, or from NumShards
      dbname0,dbname1: List of databases

      Mixing the list and # forms will produce undefined results.

    Args:
      spec: The dbspec to connect to (see above)
      kwargs: Additional arguments to pass down to the MySQL connection.
        Note that these override the parsed contents of 'spec'.
    Returns:
      a Spec object (a dictionary of connect parameters)
    Raises:
      ValueError: if spec is invalid
    """

    parts = spec.split(':')
    if parts[0] in cls._DB_TYPES:
      (parts, dbtype) = (parts[1:], parts[0])
    else:
      dbtype = 'mysql'
    kwargs.setdefault('dbtype', dbtype)
    if len(parts) == 5:
      (parts, portstr) = (parts[:4], parts[4])
      kwargs.setdefault('port', int(portstr))
    if len(parts) != 4:
      raise ValueError('Invalid DBSpec: wrong number of parts')
    kwargs.setdefault('host', parts[0])
    if not kwargs.setdefault('user', parts[1]):
      kwargs['user'] = os.getenv('USER')
    kwargs.setdefault('passwd', parts[2])
    kwargs.setdefault('db', parts[3])
    return Spec(**kwargs)

  def __init__(self, **args):
    dict.__init__(self, args)

    # Handle UNIX socket host syntax
    if self['host'].startswith('socket='):
      self['unix_socket'] = self['host'][7:]
      self['host'] = 'localhost'

    # Handle special password syntax
    if self.get('passwd', '?') in ('', '?'):
      userhost = (self['user'], self['host'])
      if userhost not in self._PW_CACHE:
        self._PW_CACHE[userhost] = getpass.getpass(
            'Password for %s@%s: ' % userhost)
      self['passwd'] = self._PW_CACHE[userhost]
    elif self['passwd'].startswith('pfile='):
      self['passwd'] = open(self['passwd'][6:]).read().strip()

    self._expander = _GetExpander(args['host'], self)

  def IsSingle(self):
    """Return whether this spec refers to a single host."""
    return isinstance(self._expander, _NoOpExpander)

  def __iter__(self):
    """Iterate over the dbspecs, expanding hosts and dbs."""
    if self.IsSingle():
      yield self
    else:
      for shard, host in self._expander().iteritems():
        args = self.copy()
        if args.get('db'):
          if ',' in args['db']:
            args['db'] = args['db'].split(',')[shard]
          args['db'] = args['db'].replace('#', str(shard))
        args['host'] = host
        yield Spec(**args)

  def Connect(self):
    if self.IsSingle():
      return Connection(**self)
    else:
      return MultiConnection(**self)


def Connect(spec, **kwargs):
  """Connect to a database.

  See Spec for a description of the accepted spec.

  Args:
    spec: The dbspec to connect to (see above)
    kwargs: Additional arguments to pass down to the MySQL connection.

  Returns:
    A Connection or MultiConnection object.
  """

  return Spec.Parse(spec, **kwargs).Connect()


class VirtualTable(object):
  """A class to hold a SQL query result.

  VirtualTable objects store the result internally as a list of field names and
  rows as tuples, but pretend to the world to be full-fledged dictionary
  cursors.  This is just a memory-saving hack.
  """

  _contents = 'Rows'

  def __init__(self, fields, result, types=None):
    """Constructor.

    Args:
      fields: A list of field names
      result: A list of lists of rows with cell data
      types: A list of python type classes for fields
    """
    self._fields = fields
    self._types = types or []
    self._result = []
    for row in result:
      self.Append(row)

  def __getitem__(self, i):
    return dict(zip(self._fields, self._result[i]))

  def __len__(self):
    return len(self._result)

  def __eq__(self, y):
    return (self.__class__ == y.__class__ and
            self.GetFields() == y.GetFields() and
            self.GetRows() == y.GetRows())

  def __ne__(self, y):
    return not self.__eq__(y)

  def __str__(self):
    rows = []
    for row in self._result:
      fields = ['%s: %s' % x for x in zip(self._fields, row)]
      rows.append('\n'.join(fields))
    return '%s returned: %d\n*****\n%s\n' % (
        self._contents, len(self._result), '\n*****\n'.join(rows))

  def __hash__(self):
    """Ordered hash of field names and unordered hash of rows."""
    ret = hash(tuple(self._fields))
    for row in self._result:
      ret ^= hash(tuple(row))
    return ret

  def Append(self, row):
    """Append a row to the table."""
    if len(row) != len(self._fields):
      raise TypeError('Incorrect column count')
    if isinstance(row, tuple):
      row = list(row)
    self._result.append(row)

  def AddField(self, name, value):
    """Add a field to the table and fill all cells with value."""
    if name in self._fields:
      raise ValueError('Field %s already exists' % name)
    if isinstance(self._fields, tuple):
      self._fields = list(self._fields)
    self._fields.append(name)
    for row in self._result:
      row.append(value)

  def GetFields(self):
    """Get the list of fields from this result.

    Returns:
      A list of string field names.
    """
    return self._fields

  def GetRows(self):
    """Get the raw, writable row data from this result.

    Returns:
      A list of lists containing cell data.
    """
    return self._result

  def GetTypes(self):
    """Get the list of Python types for fields."""
    return self._types

  def GetInsertSQLList(self, table_name, max_size=0, extended_insert=True):
    """Turn this table into SQL that would be required to recreate it.

    Args:
      table_name: The name to insert the data in to
      max_size: The maximum size, in bytes, to make each output query; 0 for
        unlimited
      extended_insert: If false, one insert per line.

    Yields:
      A list of SQL commands to be executed.
    """
    if not self._result:
      yield '-- Table %s is empty' % table_name
      raise StopIteration

    header = 'INSERT INTO %s (%s) VALUES ' % (
        table_name, ','.join(self._fields))
    statements = []
    statement_parts = [header]
    statement_len = 0
    for row in self._result:
      # Quote field contents and assemble
      quoted_values = []
      for value in row:
        if isinstance(value, tuple) and value[0] == 'literal':
          quoted_values.append(value[1])
        else:
          quoted_values.append(_BaseConnection.Escape(value))
      values = '(%s)' % ','.join(quoted_values)

      if ((len(statement_parts) > 1 and not extended_insert)
          or
          (max_size and statement_len + len(values) >= max_size)):
        # Start a new statement
        statement_parts.append(';')
        yield ''.join(statement_parts)
        statement_parts = [header]
        statement_len = len(header)
      if len(statement_parts) > 1:
        statement_parts.append(',')
      statement_parts.append(values)
      statement_len += len(values)

    if len(statement_parts) > 1:
      statement_parts.append(';')
      yield ''.join(statement_parts)

  def GetInsertSQL(self, table_name):
    """Turn this table into SQL that would be required to recreate it."""
    # GetInsertSQLList will only return one item
    return self.GetInsertSQLList(table_name).next()

  def Merge(self, table):
    """Merge the contents of another VirtualTable."""
    if self.GetFields() != table.GetFields():
      raise TypeError("Field lists don't match (%s vs. %s)" %
                      (self.GetFields(), table.GetFields()))
    self._result.extend(table.GetRows())


class QueryErrors(VirtualTable):
  """Hold SQL errors in a table format."""

  _contents = 'Errors'


class QueryWarnings(VirtualTable):
  """Hold SQL warnings in a table format."""

  _contents = 'Warnings'


class Operation(object):
  """An operation that can block between threads."""

  def __init__(self, args):
    self._args = args
    self._notification = thread_tools.Notification()
    self._result = None
    self._canceled = False

  def GetArgs(self):
    return self._args

  def SetDone(self, result):
    self._result = result
    self._notification.Notify()

  def Wait(self):
    self._notification.WaitForNotification()
    return self._result

  def TryWait(self):
    return self._notification.HasBeenNotified()

  def MarkCanceled(self):
    self._canceled = True

  def IsCanceled(self):
    return self._canceled


class _BaseConnection(object):
  """Common methods for connection objects."""

  def __init__(self):
    self._closed = False
    self._creation = ''.join(traceback.format_stack())
    self._cache = {}

  def __del__(self):
    """Destructor."""
    if not self._closed:
      logging.error('Implicitly closed database handle, created here:\n%s',
                    self._creation)
    self.Close()

  @staticmethod
  def Escape(value):
    """Escape MySQL characters in a value and wrap in quotes."""
    return "'%s'" % str(value).replace("'", "''")

  def Submit(self, query):
    """Submit a query for execution, return an opaque operation handle."""
    raise NotImplementedError

  def Wait(self, op):
    """Return a dictionary as described in MultiExecute()."""
    raise NotImplementedError

  def TryWait(self, op):
    """Check if Wait() will succeed immediately."""
    raise NotImplementedError

  def Cancel(self, op):
    """Cancel the currently running query."""
    raise NotImplementedError

  def Execute(self, query, params=None):
    """Execute a query on all targets in parallel, return the common result.

    Args:
      query: The SQL query string
      params: A dictionary of named parameters to be escaped and substituted
        into query.

    Returns:
      A VirtualTable, QueryErrors or QueryWarnings instance.

    Raises:
      InconsistentResponses: When different targets return different responses
    """
    results = self.MultiExecute(query, params)
    by_result = {}
    for name, result in results.iteritems():
      by_result.setdefault(str(result), []).append(name)
    if len(by_result) == 1:
      return results.popitem()[1]
    else:
      text = ''
      for result, names in by_result.iteritems():
        names.sort()
        text += '%s:\n%s' % (names, result)
      raise InconsistentResponses(text)

  def MultiExecute(self, query, params=None):
    """Execute a query on all targets in parallel, return all results.

    Args:
      query: The SQL query string
      params: A dictionary of named parameters to be escaped and substituted
        into query.

    Returns:
      A dictionary of host -> result, where result is a VirtualTable,
      QueryErrors or QueryWarnings instance and host is a string representation
      of the individual host.
    """
    if params:
      query %= dict(zip(params.keys(), map(self.Escape, params.values())))
    op = self.Submit(query)
    return self.Wait(op)

  def ExecuteMerged(self, query, params=None):
    """Execute a query on all targets in parallel, return all results merged.

    Args:
      query: The SQL query string
      params: A dictionary of named parameters to be escaped and substituted
        into query.

    Returns:
      A merged VirtualTable with consolidated results from all hosts, plus a
      'host' column indicating where results originated from.

    Raises:
      InconsistentSchema: When different targets return different schema.
      QueryErrorsException: When any target returns errors.
      QueryWarningsException: When any target returns warnings.
    """
    results = self.MultiExecute(query, params)

    merged = None
    for host, result in results.iteritems():
      if isinstance(result, QueryErrors):
        raise QueryErrorsException(result)
      if isinstance(result, QueryWarnings):
        raise QueryWarningsException(result)
      if not result and not merged:
        # Might be the result of a query that returns no data
        continue
      result.AddField('host', host)
      if merged:
        # Verify that the field list from this host is the same as all that came
        # before.
        if result.GetFields() != merged.GetFields():
          raise InconsistentSchema(
              '%s vs. %s' % (result.GetFields(), merged.GetFields()))
      else:
        # First time through the loop, create a new result table.
        merged = VirtualTable(result.GetFields(), [])
      merged.Merge(result)

    return merged

  def CachedExecute(self, query, params=None):
    """Execute() with a caching layer to execute each query only once."""
    if params:
      # We have to merge params before we check the cache.
      query %= dict(zip(params.keys(), map(self.Escape, params.values())))
    if query not in self._cache:
      self._cache[query] = self.Execute(query)
    return self._cache[query]

  def ExecuteOrDie(self, query, params=None, execute=None):
    """Execute() a query and raise an exception on failure."""
    result = (execute or self.Execute)(query, params)
    if isinstance(result, QueryErrors):
      raise QueryErrorsException(result)
    if isinstance(result, QueryWarnings):
      raise QueryWarningsException(result)
    return result

  def CachedExecuteOrDie(self, query, params=None):
    """Combination of CachedExecute() and ExecuteOrDie()."""
    return self.ExecuteOrDie(query, params, execute=self.CachedExecute)

  def Close(self):
    """Close database connections to all targets.

    This MUST be called before the handle is implicitly destroyed, or we log an
    error (to encourage closing ASAP after use completion).
    """
    self.Execute('exit')
    self._closed = True


class QueryConsumer(threading.Thread):
  """Consume SQL queries from a queue and return results."""

  _ERR_QUERY_CANCELED = QueryErrors(('Code', 'Message'),
                                    ((2, 'Query canceled'),))
  _ERR_UNKNOWN = QueryErrors(('Code', 'Message'),
                             ((3, 'Unknown problem'),))
  _TYPES = {
      0: float,
      1: int,
      2: int,
      3: int,
      4: float,
      5: float,
      8: int,
      9: int,
      246: float,
      249: str,
      250: str,
      251: str,
      252: str,
      253: str,
      254: str,
  }

  _DBTYPE_SETUP = {
      'mysql': None,
  }

  def __init__(self, **kwargs):
    threading.Thread.__init__(self)
    self.setDaemon(True)
    self._execute_on_connect = kwargs.pop('execute_on_connect', [])
    self._stream_results = kwargs.pop('stream_results', False)
    self._fatal_errors = kwargs.pop('fatal_errors',
                                    [1142, 1143, 1148, 2003, 2006, 2013, 2014])
    self._dbargs = kwargs
    self._dbh = None
    self._queue = Queue.Queue(0)
    self.connection_info = None
    self.in_progress = None
    self.in_progress_lock = threading.Lock()
    self._resolver = None

    if 'host' in self._dbargs:
      self.setName(self._dbargs['host'])
      self._resolver = GetResolver(self._dbargs['host'])
    if 'conv' not in self._dbargs:
      # Default to no field conversion
      self._dbargs['conv'] = {}

  def run(self):
    """Main loop inside the consumer thread."""
    while True:
      op = self._queue.get()
      query = op.GetArgs()[0]

      if query == 'exit' or query == 'exit;':
        self._Close()
        op.SetDone(None)
        continue

      if query == 'destroy' or query == 'destroy;':
        self._Close()
        op.SetDone(None)
        return

      result = self._ERR_UNKNOWN
      try:
        self.in_progress_lock.acquire()
        self.in_progress = op
        self.in_progress_lock.release()
        if op.IsCanceled():
          logging.debug('Not executing canceled query %s', query)
          result = self._ERR_QUERY_CANCELED
        else:
          result = self._Execute(query)
          if not result and op.IsCanceled():
            # hack around MySQLdb swallowing the cancel error
            result = self._ERR_QUERY_CANCELED
        self.in_progress = None
      finally:
        op.SetDone(result)

  def _Execute(self, query):
    if not self._dbh:
      args = self._dbargs.copy()
      # Custom dbtypes get to do their own resolution
      if self._resolver and not (
          'dbtype' in args and args['dbtype'] != 'mysql'):
        try:
          (args['host'], port) = self._resolver()
        except ResolutionError, e:
          logging.exception('Resolution failure.')
          return QueryErrors(('Code', 'Message'), ((1, str(e)),))
        if not args.get('port'):
          args['port'] = port
      try:
        self._Connect(args)
      except MySQLdb.OperationalError, e:
        logging.exception('Connection returned error.')
        self._Close()
        return QueryErrors(('Code', 'Message'), ((e[0], e[1]),))
      except Exception, e:
        logging.exception('Connection returned unknown error.')
        self._Close()
        return QueryErrors(('Code', 'Message'), ((3, str(e)),))
    try:
      logging.debug('Executing %s', query)
      self._dbh.query(query)
      if self._stream_results:
        data = self._dbh.use_result()
      else:
        data = self._dbh.store_result()
      info = self._dbh.info()
      if info:
        # TODO(flamingcow): Find a better way to get warning count
        warnings = int(info.split()[-1])
        if warnings:
          # MySQLdb doesn't let us get the actual warning text, just the count
          return QueryWarnings(('Level', 'Code', 'Message'), [])
      if not data:
        return None
      fields = [i[0] for i in data.describe()]
      types = [self._TYPES.get(i[1], None) for i in data.describe()]
      if self._stream_results:
        def StreamResults():
          while True:
            row = data.fetch_row()
            if not row:
              raise StopIteration
            yield row[0]
        result = StreamResults()
      else:
        result = data.fetch_row(0)
    except MySQLdb.Error, e:
      code, message = e.args
      logging.exception('Query returned error.')
      if code in self._fatal_errors:
        self._Close()
      return QueryErrors(('Code', 'Message'), ((code, message),))
    except Exception, e:
      logging.exception('Query returned unknown error.')
      return QueryErrors(('Code', 'Message'), ((4, str(e))))
    return VirtualTable(fields, result, types)

  def _Connect(self, args):
    log_args = args.copy()
    if 'passwd' in log_args:
      log_args['passwd'] = 'XXXXXXX'
    logging.debug('Connecting with %s', log_args)

    if 'dbtype' in args:
      setup = self._DBTYPE_SETUP[args['dbtype']]
      if setup:
        setup(args)
      del args['dbtype']  # MySQLdb doesn't like extra arguments.

    self._dbh = MySQLdb.connect(**args)
    self._dbh.autocommit(True)
    self._dbh.query('SELECT CONNECTION_ID()')
    data = self._dbh.store_result()
    self.connection_info = {'args': args,
                            'id': int(data.fetch_row(0)[0][0])}
    for init_query in self._execute_on_connect:
      logging.debug('Executing on-connect query: %s', init_query)
      self._dbh.query(init_query)

  def _Close(self):
    if self._dbh:
      logging.debug('Closing connection to %s', self._dbargs['host'])
      self._dbh.close()
      self._dbh = None
      self.connection_info = None

  def Submit(self, op):
    self._queue.put(op)


class Connection(_BaseConnection):
  """A connection to a single database host."""

  def __init__(self, **kwargs):
    _BaseConnection.__init__(self)
    self._consumer = QueryConsumer(**kwargs)
    self._consumer.start()

  def __del__(self):
    _BaseConnection.__del__(self)
    self.Wait(self.Submit('destroy'))
    self._consumer.join()

  def Submit(self, query):
    op = Operation((query,))
    self._consumer.Submit(op)
    return op

  def Wait(self, op):
    return {self._consumer.getName(): op.Wait()}

  def TryWait(self, op):
    return op.TryWait()

  def Cancel(self, op):
    """Cancel a pending or running operation, if possible.

    Args:
      op: The opaque handle returned by the Submit() call.
    """
    # Hold the lock to stop the consumer from starting new queries.  If it's
    # currently running our query, kill it repeatedly until the operation
    # returns.
    with self._consumer.in_progress_lock:
      op.MarkCanceled()
      if self._consumer.in_progress != op: return
      while not op.TryWait():  # wait until our query completes
        connection_info = self._consumer.connection_info
        if not connection_info:
          time.sleep(0.1)
          continue
        try:
          temp_dbh = MySQLdb.connect(**connection_info['args'])
          temp_dbh.query('KILL QUERY %d' % connection_info['id'])
          temp_dbh.close()
        except MySQLdb.Error, e:
          logging.error('Failed to cancel query: %s', e)
        time.sleep(0.1)


class MultiConnection(_BaseConnection):
  """Wrap a set of real connections; execute in parallel."""

  _SHARD_RE = re.compile('^\s*ON\s+SHARD\s+(?P<shard>[\d,]+)\s+(?P<query>.*)$',
                         re.IGNORECASE | re.DOTALL)

  def __init__(self, **kwargs):
    _BaseConnection.__init__(self)
    self._dbargs = Spec(**kwargs)
    self._connections = {}
    for i, spec in enumerate(self._dbargs):
      # Make a copy before we add to it, in case the list is shared across
      # specs.
      spec['execute_on_connect'] = list(spec.get('execute_on_connect', []) +
                                        ['SET @shard=%d' % i])
      self._connections[i] = (spec, spec.Connect())

  def __del__(self):
    _BaseConnection.__del__(self)
    for _, connection in self._connections.itervalues():
      connection.Close()

  def Submit(self, query):
    """Submit a query for execution without blocking for completion.

    Args:
      query: SQL query string.

    Returns:
      An opaque handle to the running query, to be passed to Wait() or Cancel().
    """
    shard_match = self._SHARD_RE.match(query)
    if shard_match:
      shards = [int(shard) for shard in shard_match.group('shard').split(',')]
      query = shard_match.group('query')
    else:
      shards = self._connections.keys()

    ops = []
    for shard, (spec, connection) in self._connections.iteritems():
      if shard in shards:
        ops.append((spec['host'], connection, connection.Submit(query)))
    return ops

  def Wait(self, ops):
    results = {}
    for name, connection, op in ops:
      results[name] = connection.Wait(op).values()[0]
    return results

  def TryWait(self, ops):
    for name, connection, op in ops:
      if not op.TryWait():
        return False
    return True

  def Cancel(self, op):
    for _, connection, subop in op:
      connection.Cancel(subop)


class ConnectionPool(_BaseConnection):
  """Thread-safe self-resizing pool of connections."""

  def __init__(self, spec, max_open_unused=1, max_open=5, **kwargs):
    """Constructor.

    Args:
      spec: dbspec; see Connect()
      max_open_unused: Maximum number of connections to keep open and unused in
        the pool.
      max_open: Maximum number of connections total.  If the dbspec is to
        multiple shards (a MultiConnection underneath), this is the number of
        connections *per shard*.
      kwargs: Additional arguments to be passed down to the MySQL connection.
    """
    _BaseConnection.__init__(self)
    self._max_open_unused = max_open_unused
    self._cv = threading.Condition()
    self._open_spares = []    # GUARDED_BY(_cv)
    self._closed_spares = []  # GUARDED_BY(_cv)
    # We create all connections now, but don't connect them.  Connection objects
    # don't open connections until their first use.
    for _ in xrange(max_open):
      self._closed_spares.append(Connect(spec, **kwargs))

  def IsAvailable(self):
    """Check if sending a query won't block for connection limit.

    This value is out-of-date before this function returns, so only use it in
    advisory capacities.

    Returns:
      True if a connection is available at the moment of the check, otherwise
      False.
    """
    return (len(self._open_spares) + len(self._closed_spares)) > 0

  def Close(self):
    # Close every connection that has been returned.  Those still checked out
    # are lost.
    with self._cv:
      for conn in self._open_spares + self._closed_spares:
        conn.Close()
      self._closed = True

  def Acquire(self):
    """Get a connection from the pool.

    Blocks if there are no spare connections.  You must call Release() when you
    are done with the connection, or it will orphaned and not be usable by other
    pool callers.

    Returns:
      A Connection or MultiConnection instance.
    """
    with self._cv:
      while not self._open_spares and not self._closed_spares:
        logging.info('ConnectionPool blocking waiting for a connection.')
        start_time = time.time()
        self._cv.wait()
        logging.info('ConnectionPool waited %f seconds to get a connection.',
                     time.time() - start_time)
      try:
        return self._open_spares.pop()
      except IndexError:
        return self._closed_spares.pop()

  def Release(self, conn):
    """Return a connection to the pool.

    The caller may not use the conn object after calling Release().

    Args:
      conn: The connection instance to return.
    """
    with self._cv:
      if len(self._open_spares) < self._max_open_unused:
        self._open_spares.append(conn)
      else:
        conn.Close()
        self._closed_spares.append(conn)
      self._cv.notify()

  def Submit(self, query):
    """Submit a query for execution, return an opaque operation handle."""
    conn = self.Acquire()
    return [conn, conn.Submit(query)]

  def Wait(self, op):
    """Return a dictionary as described in MultiExecute()."""
    conn, opobj = op
    ret = conn.Wait(opobj)
    self.Release(conn)
    return ret

  def TryWait(self, op):
    """Return true if the operation has completed."""
    conn, opobj = op
    return conn.TryWait(opobj)

  def Cancel(self, op):
    conn, subop = op
    conn.Cancel(subop)


# Matches, e.g., {0..89}
_RANGE_RE = re.compile(r'{(?P<start>\d+)\.\.(?P<end>\d+)}')


def _GetExpander(name, dbargs):
  if '#' in name:
    return _HashExpander(name, dbargs.copy())

  if ',' in name:
    return _ListExpander(name)

  if _RANGE_RE.search(name):
    return _RangeExpander(name)

  return _NoOpExpander(name)


def GetResolver(name):
  # If it's nothing else, we assume that it's DNS
  return DNSResolver(name)


class Cache(object):
  """Simple wrapper to store args and cache result."""

  # Entirely arbitrary value
  _CACHE_TTL = 60

  def __init__(self, name, args=None):
    self._name = name
    self._args = args
    self._last_lookup_time = 0

  def __call__(self):
    if time.time() - self._last_lookup_time > self._CACHE_TTL:
      self._last_lookup_value = self._Lookup()
    return self._last_lookup_value


class _HashExpander(Cache):
  """Expand # in a name."""

  def _Lookup(self):
    # As long as we remove at least one # from the name, this can't be
    # infinitely recursive.
    self._args['host'] = self._name.replace('#', '0')
    self._args['db'] = self._args['db'].replace('#', '0')
    conn = MultiConnection(**self._args)
    result = conn.ExecuteOrDie('SELECT NumShards FROM ConfigurationGlobals')
    expansion = {}
    for x in xrange(int(result[0]['NumShards'])):
      expansion[x] = self._name.replace('#', str(x))
    conn.Close()
    return expansion


class _ListExpander(Cache):
  """Expand , in a name (list of hosts)."""

  def _Lookup(self):
    hosts = self._name.split(',')
    return dict(enumerate(hosts))


class _RangeExpander(Cache):
  """Expand {0..9} in a name."""

  def _Lookup(self):
    range_result = _RANGE_RE.search(self._name)
    expansion = {}
    range_params = range_result.groupdict()
    for x in xrange(int(range_params['start']), int(range_params['end']) + 1):
      expansion[x] = self._name.replace(range_result.group(0), str(x))
    return expansion


class _NoOpExpander(Cache):
  """Expand a name to itself, as shard zero."""

  def _Lookup(self):
    return {0: self._name}


_DEFAULT_PORT = 3306


class DNSResolver(Cache):
  """Resolve a single DNS host."""

  def _Lookup(self):
    if self._name == 'localhost':
      # Hack to allow connecting via the UNIX socket.
      return ('localhost', _DEFAULT_PORT)
    try:
      ip = random.choice(socket.gethostbyname_ex(self._name)[2])
    except socket.gaierror:
      raise ResolutionError('Failed to resolve %s' % self._name)
    return (ip, _DEFAULT_PORT)
