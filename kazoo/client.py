"""Kazoo Zookeeper Client"""
import inspect
import logging
from collections import defaultdict
from functools import partial
from os.path import split

from kazoo.exceptions import AuthFailedError
from kazoo.exceptions import ConnectionClosedError
from kazoo.exceptions import NoNodeError
from kazoo.exceptions import NodeExistsError
from kazoo.exceptions import ConfigurationError
from kazoo.handlers.threading import SequentialThreadingHandler
from kazoo.hosts import collect_hosts
from kazoo.recipe.lock import Lock
from kazoo.recipe.party import Party
from kazoo.recipe.party import ShallowParty
from kazoo.recipe.election import Election
from kazoo.protocol.paths import normpath
from kazoo.protocol.paths import _prefix_root
from kazoo.protocol.serialization import Create
from kazoo.protocol.serialization import Close
from kazoo.protocol.serialization import Exists
from kazoo.protocol.serialization import GetChildren
from kazoo.protocol.states import KazooState
from kazoo.protocol.states import KeeperState
from kazoo.protocol import proto_writer
from kazoo.retry import KazooRetry
from kazoo.security import OPEN_ACL_UNSAFE

log = logging.getLogger(__name__)


class KazooClient(object):
    """An Apache Zookeeper Python wrapper supporting alternate callback
    handlers and high-level functionality

    Watch functions registered with this class will not get session
    events, unlike the default Zookeeper watch's. They will also be
    called with a single argument, a :class:`WatchedEvent` instance.

    """
    def __init__(self, hosts='127.0.0.1:2181', watcher=None,
                 timeout=10.0, client_id=None, max_retries=None, retry_delay=0.1,
                 retry_backoff=2, retry_jitter=0.8, handler=None,
                 default_acl=None, read_only=None):
        """Create a KazooClient instance. All time arguments are in seconds.

        :param hosts: Comma-separated list of hosts to connect to
                      (e.g. 127.0.0.1:2181,127.0.0.1:2182).
        :param watcher: Set a default watcher. This will be called by
                        the actual default watcher that
                        :class:`KazooClient` establishes.
        :param timeout: The longest to wait for a Zookeeper connection.
        :param client_id: A Zookeeper client id, used when
                          re-establishing a prior session connection.
        :param max_retries: Maximum retries when using the
                            :meth:`KazooClient.retry` method.
        :param retry_delay: Initial delay when retrying a call.
        :param retry_backoff: Backoff multiplier between retry attempts.
                              Defaults to 2 for exponential backoff.
        :param retry_jitter: How much jitter delay to introduce per call. An
                             amount of time up to this will be added per retry
                             call to avoid hammering the server.
        :param handler: An instance of a class implementing the
                        :class:`~kazoo.interfaces.IHandler` interface
                        for callback handling.
        :param default_acl: A default ACL used on node creation.


        Retry parameters will be used for connection establishment attempts
        and reconnects.

        """
        # Record the handler strategy used
        self.handler = handler if handler else SequentialThreadingHandler()
        if inspect.isclass(self.handler):
            raise ConfigurationError("Handler must be an instance of a class, "
                                     "not the class: %s" % self.handler)

        self.default_acl = default_acl
        self.hosts, chroot = collect_hosts(hosts)
        if chroot:
            self.chroot = normpath(chroot)
            if not self.chroot.startswith('/'):
                raise ValueError('chroot not absolute')
        else:
            self.chroot = ''

        # Curator like simplified state tracking, and listeners for state
        # transitions
        self._state_lock = self.handler.rlock_object()
        self._state = KeeperState.CLOSED
        self.state = KazooState.LOST
        self.state_listeners = set()

        self._reset()
        self.read_only = read_only

        if client_id:
            self._session_id = client_id[0]
            self._session_passwd = client_id[1]
        else:
            self._session_id = None
            self._session_passwd = str(bytearray([0] * 16))

        # ZK uses milliseconds
        self._session_timeout = int(timeout * 1000)

        # We use events like twitter's client to track current and desired
        # state (connected, and whether to shutdown)
        self._live = self.handler.event_object()
        self._writer_stopped = self.handler.event_object()
        self._stopped = self.handler.event_object()
        self._stopped.set()
        self._writer_stopped.set()

        self.retry = KazooRetry(
            max_tries=max_retries,
            delay=retry_delay,
            backoff=retry_backoff,
            max_jitter=retry_jitter,
            sleep_func=self.handler.sleep_func
        )
        self.retry_sleeper = self.retry.retry_sleeper.copy()

        # convenience API
        from kazoo.recipe.barrier import Barrier
        from kazoo.recipe.barrier import DoubleBarrier
        from kazoo.recipe.partitioner import SetPartitioner
        from kazoo.recipe.watchers import ChildrenWatch
        from kazoo.recipe.watchers import DataWatch

        self.Barrier = partial(Barrier, self)
        self.DoubleBarrier = partial(DoubleBarrier, self)
        self.ChildrenWatch = partial(ChildrenWatch, self)
        self.DataWatch = partial(DataWatch, self)
        self.Election = partial(Election, self)
        self.Lock = partial(Lock, self)
        self.Party = partial(Party, self)
        self.SetPartitioner = partial(SetPartitioner, self)
        self.ShallowParty = partial(ShallowParty, self)

    def _reset(self):
        """Resets a variety of client states for a new connection"""
        with self._state_lock:
            self._queue = self.handler.peekable_queue()
            self._pending = self.handler.peekable_queue()
            self._child_watchers = defaultdict(set)
            self._data_watchers = defaultdict(set)

        self._session_id = None
        self._session_passwd = str(bytearray([0] * 16))
        self.last_zxid = 0

    def add_listener(self, listener):
        """Add a function to be called for connection state changes

        This function will be called with a :class:`KazooState`
        instance indicating the new connection state.

        """
        if not (listener and callable(listener)):
            raise ConfigurationError("listener must be callable")
        self.state_listeners.add(listener)

    def remove_listener(self, listener):
        """Remove a listener function"""
        self.state_listeners.discard(listener)

    @property
    def connected(self):
        """Returns whether the Zookeeper connection has been established"""
        return self._live.is_set()

    @property
    def client_id(self):
        """Returns the client id for this Zookeeper session if connected"""
        if self._live.is_set():
            return (self._session_id, self._session_passwd)
        return None

    def _make_state_change(self, state):
        # skip if state is current
        if self.state == state:
            return
        self.state = state

        # Create copy of listeners for iteration in case one needs to
        # remove itself
        for listener in list(self.state_listeners):
            try:
                remove = listener(state)
                if remove is True:
                    self.remove_listener(listener)
            except Exception:
                log.exception("Error in connection state listener")

    def _session_callback(self, state):
        if state == self._state:
            return

        with self._state_lock:
            self._state = state

        if state == KeeperState.CONNECTED:
            log.info("Zookeeper connection established")
            self._live.set()
            self._make_state_change(KazooState.CONNECTED)
        elif state in (KeeperState.EXPIRED_SESSION,
                       KeeperState.AUTH_FAILED,
                       KeeperState.CLOSED):
            log.info("Zookeeper session lost, state: %s", state)
            self._live.clear()
            self._make_state_change(KazooState.LOST)
            self._reset()
        else:
            log.info("Zookeeper connection lost")
            # Connection lost
            self._live.clear()
            self._make_state_change(KazooState.SUSPENDED)

    def _safe_close(self):
        self.handler.stop()

        if not self._writer_stopped.is_set():
            self._writer_stopped.wait(10)
            if not self._writer_stopped.is_set():
                raise Exception("Writer still open from prior connection"
                                " and wouldn't close after 10 seconds")

    def _call(self, request, async_object):
        with self._state_lock:
            if self._state == KeeperState.AUTH_FAILED:
                raise AuthFailedError()
            if self._state == KeeperState.CLOSED:
                raise ConnectionClosedError("Connection has been closed")

            self._queue.put((request, async_object))

    def start_async(self):
        """Asynchronously initiate connection to ZK

        :returns: An event object that can be checked to see if the
                  connection is alive.
        :rtype: :class:`~threading.Event` compatible object

        """
        # If we're already connected, ignore
        if self._live.is_set():
            return self._live

        # Make sure we're safely closed
        self._safe_close()

        # We've been asked to connect, clear the stop and our writer
        # thread indicator
        self._stopped.clear()
        self._writer_stopped.clear()

        # Start the handler
        self.handler.start()

        # Start the connection writer to establish the connection
        self.handler.spawn(proto_writer, self)
        return self._live

    def start(self, timeout=15):
        """Initiate connection to ZK

        :param timeout: Time in seconds to wait for connection to
                        succeed.
        :throws: :attr:`~kazoo.interfaces.IHandler.timeout_exception`
                 if the connection wasn't established within `timeout`
                 seconds.

        """
        event = self.start_async()
        event.wait(timeout=timeout)
        if not self.connected:
            # We time-out, ensure we are disconnected
            self.stop()
            raise self.handler.timeout_exception("Connection time-out")

    def stop(self):
        """Gracefully stop this Zookeeper session"""
        if self._stopped.is_set():
            return

        self._stopped.set()
        self._queue.put((Close(), None))
        self._safe_close()

    def restart(self):
        """Stop and restart the Zookeeper session."""
        self.stop()
        self.start()

    def add_auth_async(self, scheme, credential):
        """Asynchronously send credentials to server

        :param scheme: authentication scheme (default supported:
                       "digest")
        :param credential: the credential -- value depends on scheme
        :returns: AsyncResult object set on completion
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self.handler.async_result()

        self._safe_call(self.zookeeper.add_auth, async_result, scheme,
                        credential)

        # Compensate for io polling bug on auth by running an exists call
        # See https://issues.apache.org/jira/browse/ZOOKEEPER-770
        self.exists_async("/")
        return async_result

    def add_auth(self, scheme, credential):
        """Send credentials to server

        :param scheme: authentication scheme (default supported:
                       "digest")
        :param credential: the credential -- value depends on scheme

        """
        return self.add_auth_async(scheme, credential).get()

    def ensure_path(self, path, acl=None):
        """Recursively create a path if it doesn't exist
        """
        self._inner_ensure_path(path, acl)

    def _inner_ensure_path(self, path, acl):
        if self.exists(path):
            return

        if acl is None and self.default_acl:
            acl = self.default_acl

        parent, node = split(path)

        if node:
            self._inner_ensure_path(parent, acl)
        try:
            self.create_async(path, "", acl=acl).get()
        except NodeExistsError:
            # someone else created the node. how sweet!
            pass

    def unchroot(self, path):
        if not self.chroot:
            return path

        if path.startswith(self.chroot):
            return path[len(self.chroot):]
        else:
            return path

    def create_async(self, path, value, acl=None, ephemeral=False,
                     sequence=False):
        """Asynchronously create a ZNode

        :param path: path of node
        :param value: initial value of node
        :param acl: permissions for node
        :param ephemeral: boolean indicating whether node is ephemeral
                          (tied to this session)
        :param sequence: boolean indicating whether path is suffixed
                         with a unique index
        :returns: AsyncResult object set on completion with the real
                  path of the new node
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        if acl is None and self.default_acl:
            acl = self.default_acl

        flags = 0
        if ephemeral:
            flags |= 1
        if sequence:
            flags |= 2
        if acl is None:
            acl = OPEN_ACL_UNSAFE

        async_result = self.handler.async_result()
        self._call(Create(_prefix_root(self.chroot, path), value, acl, flags),
                   async_result)
        return async_result

    def create(self, path, value, acl=None, ephemeral=False, sequence=False,
               makepath=False):
        """Create a ZNode

        :param path: path of node
        :param value: initial value of node
        :param acl: permissions for node
        :param ephemeral: boolean indicating whether node is ephemeral
                          (tied to this session)
        :param sequence: boolean indicating whether path is suffixed
                         with a unique index
        :param makepath: Whether the path should be created if it
                         doesn't exist
        :returns: real path of the new node

        """
        try:
            realpath = self.create_async(path, value, acl=acl,
                ephemeral=ephemeral, sequence=sequence).get()

        except NoNodeError:
            # some or all of the parent path doesn't exist. if makepath is set
            # we will create it and retry. If it fails again, someone must be
            # actively deleting ZNodes and we'd best bail out.
            if not makepath:
                raise

            parent, _ = split(path)

            # using the inner call directly because path is already namespaced
            self._inner_ensure_path(parent, acl)

            # now retry
            realpath = self.create_async(path, value, acl=acl,
                ephemeral=ephemeral, sequence=sequence).get()

        return self.unchroot(realpath)

    def exists_async(self, path, watch=None):
        """Asynchronously check if a node exists

        :param path: path of node
        :param watch: optional watch callback to set for future changes
                      to this path
        :returns: stat of the node if it exists, else None
        :rtype: `dict` or `None`

        """
        async_result = self.handler.async_result()
        self._call(Exists(_prefix_root(self.chroot, path), watch),
                   async_result)
        return async_result

    def exists(self, path, watch=None):
        """Check if a node exists

        :param path: path of node
        :param watch: optional watch callback to set for future changes
                      to this path
        :returns: stat of the node if it exists, else None
        :rtype: `dict` or `None`

        """
        return self.exists_async(path, watch).get()

    def get_async(self, path, watch=None):
        """Asynchronously get the value of a node

        :param path: path of node
        :param watch: optional watch callback to set for future changes
                      to this path
        :returns: AsyncResult set with tuple (value, :class:`ZnodeStat`
                  ) of node on success
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self.handler.async_result()
        self._safe_call(self.zookeeper.aget, async_result, path)
        return async_result

    def get(self, path, watch=None):
        """Get the value of a node

        :param path: path of node
        :param watch: optional watch callback to set for future changes
                      to this path
        :returns: tuple (value, :class:`ZnodeStat`) of node

        """
        return self.get_async(path, watch).get()

    def get_children_async(self, path, watch=None):
        """Asynchronously get a list of child nodes of a path

        :param path: path of node to list
        :param watch: optional watch callback to set for future changes
                      to this path
        :returns: AsyncResult set with list of child node names on
                  success
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self.handler.async_result()
        self._call(GetChildren(_prefix_root(self.chroot, path),
                               None, watch), async_result)
        return async_result

    def get_children(self, path, watch=None):
        """Get a list of child nodes of a path

        :param path: path of node to list
        :param watch: optional watch callback to set for future changes
                      to this path
        :returns: list of child node names

        """
        return self.get_children_async(path, watch).get()

    def set_async(self, path, data, version=-1):
        """Set the value of a node

        If the version of the node being updated is newer than the
        supplied version (and the supplied version is not -1), a
        BadVersionException will be raised.

        :param path: path of node to set
        :param data: new data value
        :param version: version of node being updated, or -1
        :returns: AsyncResult set with new node :class:`ZnodeStat` on
                  success
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`

        """
        async_result = self.handler.async_result()
        callback = partial(_generic_callback, async_result)

        self._safe_call(self.zookeeper.aset, async_result, path, data, version,
                        callback)
        return async_result

    def set(self, path, data, version=-1):
        """Set the value of a node

        If the version of the node being updated is newer than the
        supplied version (and the supplied version is not -1), a
        BadVersionException will be raised.

        :param path: path of node to set
        :param data: new data value
        :param version: version of node being updated, or -1
        :returns: updated :class:`ZnodeStat` of the node

        """
        return self.set_async(path, data, version).get()

    def delete_async(self, path, version=-1):
        """Asynchronously delete a node

        :param path: path of node to delete
        :param version: version of node to delete, or -1 for any
        :returns: AyncResult set upon completion
        :rtype: :class:`~kazoo.interfaces.IAsyncResult`
        """
        async_result = self.handler.async_result()
        callback = partial(_generic_callback, async_result)

        self._safe_call(self.zookeeper.adelete, async_result, path, version,
                        callback)
        return async_result

    def delete(self, path, version=-1, recursive=False):
        """Delete a node

        :param path: path of node to delete
        :param version: version of node to delete, or -1 for any
        :param recursive: Recursively delete node and all its children,
            defaults to False.
        """
        if recursive:
            self._delete_recursive(path)
        else:
            self.delete_async(path, version).get()

    def _delete_recursive(self, path):
        try:
            children = self.get_children(path)
        except self.zookeeper.NoNodeError:
            return

        if children:
            for child in children:
                if path == "/":
                    child_path = path + child
                else:
                    child_path = path + "/" + child

                self._delete_recursive(child_path)
        try:
            self.delete(path)
        except self.zookeeper.NoNodeError:
            pass
