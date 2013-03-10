# -*- coding: utf-8 -
#
# This file is part of gaffer. See the NOTICE for more information.

from collections import deque
import sqlite3

from ..events import EventEmitter
from .util import load_backend


class KeyNotFound(Exception):
    """ exception raised when the key isn't found """


class KeyConflict(Exception):
    """ exception when you try to create a key that already exists """


class InvalidKey(Exception):
    """ exception raised when the key is invalid """


class UnknownPermission(Exception):
    """ raised when the permission is not found """


class Key(object):
    """ instance representing a key """

    def __init__(self, api_key, label="", permissions={}):
        self.api_key = api_key
        self.label = label
        self.permissions = permissions

        # parse permissions
        self.manage = permissions.get('manage', None) or {}
        self.write = permissions.get('write', None) or {}
        self.read = permissions.get('write', None) or {}
        self.can_manage_all = '*' in self.manage
        self.can_write_all = '*' in self.write
        self.can_read_all = '*' in self.read

    def __str__(self):
        return "Key: %s" % self.api_key

    @classmethod
    def load(cls, obj):
        if not "key" in obj:
            raise InvalidKey()

        key = obj['key']
        label = obj.get('label', "")
        permissions = obj.get("permissions", {})
        return cls(key, label, permissions)

    def dump(self):
        return {"key": self.api_key, "label": self.label, "permissions":
                self.permissions}

    def is_superuser(self):
        """ does this key has all rights? """
        return self.permissions.get("superuser", False)

    def can_create_key(self):
        """ can we create new keys with this key?

        Note only a user key can create keys able to create other keys. Sub
        keys can't create keys.
        """
        return self.permissions.get("create_key", False)

    def can_create_user(self):
        """ can we create users with this key ? """
        return self.permissions.get("create_user", False)

    def can_manage(self, job_or_session):
        """ test if a user can manage a job or a session

        managing a session means:
        - load/unload/update job in this session
        - start/stop processes and jobs in a session
        - list
        """
        return self.can('manage', job_or_session)

    def can_write(self, job_or_session):
        """ test if a user can write to a process for this job or all the jobs
        of the session """
        if self.can_manage(job_or_session):
            return True

        return self.can('write', job_or_session)

    def can_read(self, job_or_session):
        """ test if a user can read from a process for this job or all the jobs
        of the session """

        if self.can_write(job_or_session):
            return True

        return self.can('read', job_or_session)

    def can(self, permission, what):
        """ test the permission for a job or a session """
        if not hasattr(self, permission):
            raise UnknownPermission("%r does not exist")

        # do this key has all the permissions
        if getattr(self, "%s_all" % permission, False):
            return True

        # get all permissions
        permissions = getattr(self, permission, {})

        if "." in what:
            # we are testing job possibilities. The try first to know if we
            # have the permissions on the session
            session = what.split(".")[0]
            if session in permissions:
                return True

        # test the job permission
        if what in getattr(self, permission, {}):
            return True

        return False


class DummyKey(Key):

    def __init__(self):
        super(DummyKey, self).__init__("dummy")

    def can_create_key(self):
        return False

    def is_superuser(self):
        return False

    def can_create_user(self):
        return False

    def can(self, permissions, what):
        return True


class KeyManager(object):

    def __init__(self, loop, cfg):
        self.loop = loop
        self.cfg = cfg
        self._cache = {}
        self._entries = deque()
        self._lock = Lock()

        # initialize the db backend
        if not cfg.keys_backend or cfg.keys_backend == "default":
            self._backend = SqliteKeyBackend(loop, cfg)
        else:
            self._backend = load_backend(backend)

        # initialize the events listenr
        self._emitter = EventEmitter()

    def subscribe(self, event, listener):
        self._emitter.subscribe(event, listener)

    def unsubscribe(self, event, listener):
        self._emitter.unsubscribe(event, listener)

    def open(self):
        self._backend.open()
        self._emitter.publlish("open", self)

    def close(self):
        self._emitter.publlish("close", self)
        self._backend.close()
        self._emitter.close()

        # empty the cache
        self._entries.clear()
        self._cache = {}

    def all_keys(self):
        return self._backend.all()

    def set_key(self, key, data, parent=None):
        self._backend.set_key(key, data, parent=parent)
        self._emitter.publlish("set", self, key)

    def get_key(self, key):
        if key in self._cache:
            return self._cache[key]

        okey = self._backend.get_key(key)

        # do we need to clean the cache?
        # we only keep last 1000 acceded keys in RAM
        if len(self._cache) >= 1000:
            last = self._entries.popleft()
            self._cache.pop(key)

        # enter last entry in the cache
        self._cache[key] = okey
        self._entries.append(key)
        return okey

    def delete_key(self, key):
        # remove the key and all sub keys from the cache if needed
        self._delete_entry(key)
        with self.conn:
            cur = self.conn.cursor()
            rows = cur.execute("SELECT key FROM keys where parent=?", [key])
            [self._delete_entry(row[0]) for row in rows]

        # then delete the
        self._backend.delete_key(key)
        self._emitter.publlish("delete", self, key)

    def has_key(self, key):
        return self._backend.has_key(key)

    def _delete_entry(self, key):
        if key in self._cache:
            self._entries.remove(key)
            self._cache.pop(key)


class KeyBackend(object):

    def __init__(self, loop, cfg, dbname=None):
        self.loop = patch_loop(loop)
        self.cfg = cfg
        self.dbname = dbname

    def open(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError

    def all_keys(self):
        raise NotImplementedError

    def set_key(self, key, data):
        raise NotImplementedError

    def get_key(self, key):
        raise NotImplementedError

    def delete_key(self, key):
        raise NotImplementedError

    def has_key(self, key):
        raise NotImplementedError

    def all_subkeys(self, key):
        raise NotImplementedError


class SqliteKeyBackend(KeyBackend):
    """ sqlite backend to store API keys in gaffer """


    def __init__(self, loop, cfg, dbname=None):
        # set dbname
        dbname = dbname or "keys.db"
        if dbname != ":memory:":
            dbname = os.path.join(cfg.config_dir, dbname)

        super(SqliteAuthHandler, self).__init__(loop, cfg, dbname)

        # intitialize conn
        self.conn = None

    def open(self):
        self.conn = sqlite3.connect(self.dbname)
        if self.dbname != ":memory:" and os.path.isfile(self.dbname):
            return

        with self.conn:
            sql = """CREATE TABLE keys (key text primary key, data text,
            parent text)"""
            self.conn.execute(sql)

    def close(self):
        self.conn.commit()
        self.conn.close()

    def all_keys(self):
        with self.conn:
            cur = self.conn.cursor()
            rows = cur.execute("SELECT * FROM keys", [])
            return [self._make_key(row) for row in rows]

    def set_key(self, key, data, parent=None):
        assert self.conn is not None
        if isinstance(data, dict):
            data = json.dumps(data)

        with self.conn:
            cur = self.conn.cursor()
            try:
                res = cur.execute("INSERT INTO keys VALUES (?, ?, ?)", [key,
                    data, parent])
            except sqlite3.IntegrityError:
                raise UserConflict()

    def get_key(self, key, subkeys=True):
        assert self.conn is not None

        with self.conn:
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM keys WHERE key=?", [key])
            row = cur.fetchone()

        if not row:
            raise KeyNotFound()
        return self._make_key(row)

    def delete_key(self, key):
        assert self.conn is not None
        with self.conn:
            self.conn.execute("DELETE FROM keys WHERE key=?", [key])

    def has_key(self, key):
        try:
            self.get_key(key)
        except KeyNotFound:
            return False
        return True

    def all_subkeys(self, key):
        with self.conn:
            cur = self.conn.cursor()
            rows = cur.execute("SELECT * FROM keys WHERE parent=?", [key])
            return [self._make_key(row) for row in rows]

    def _make_key(self, row):
        obj = json.loads(row[1])
        obj.update({ "key": row[0] })
        return obj
