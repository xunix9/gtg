# -----------------------------------------------------------------------------
# Getting Things GNOME! - a personal organizer for the GNOME desktop
# Copyright (c) 2008-2013 - Lionel Dricot & Bertrand Rousseau
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------
"""
Contains the Datastore object, which is the manager of all the active backends
(both enabled and disabled ones)
"""

from collections import deque
import threading
import logging
import uuid

from GTG.backends.backend_signals import BackendSignals
from GTG.backends.generic_backend import GenericBackend
from GTG.core.config import CoreConfig
from GTG.core import requester
from GTG.core.search import parse_search_query, search_filter, InvalidQuery
from GTG.core.tag import Tag, SEARCH_TAG
from GTG.core.task import Task
from GTG.core.treefactory import TreeFactory
from GTG.core.borg import Borg


log = logging.getLogger(__name__)
TAG_XMLROOT = "tagstore"


class DataStore():
    """
    A wrapper around all backends that is responsible for keeping the backend
    instances. It can enable, disable, register and destroy backends, and acts
    as interface between the backends and GTG core.
    You should not interface yourself directly with the DataStore: use the
    Requester instead (which also sends signals as you issue commands).
    """

    def __init__(self, global_conf=CoreConfig()):
        """
        Initializes a DataStore object
        """
        # dictionary {backend_name_string: Backend instance}
        self.backends = {}
        self.treefactory = TreeFactory()
        self._tasks = self.treefactory.get_tasks_tree()
        self.requester = requester.Requester(self, global_conf)
        self.tagfile_loaded = False
        self._tagstore = self.treefactory.get_tags_tree(self.requester)
        self._backend_signals = BackendSignals()
        self.conf = global_conf
        self.tag_idmap = {}

        # Flag when turned to true, all pending operation should be
        # completed and then GTG should quit
        self.please_quit = False

        # The default backend must be loaded first. This flag turns to True
        # when the default backend loading has finished.
        self.is_default_backend_loaded = False
        self._backend_signals.connect('default-backend-loaded',
                                      self._activate_non_default_backends)
        self._backend_mutex = threading.Lock()

    # Accessor to embedded objects in DataStore ##############################
    def get_tagstore(self):
        """
        Return the Tagstore associated with this DataStore

        @return GTG.core.tagstore.TagStore: the tagstore object
        """
        return self._tagstore

    def get_requester(self):
        """
        Return the Requester associate with this DataStore

        @returns GTG.core.requester.Requester: the requester associated with
                                               this datastore
        """
        return self.requester

    def get_tasks_tree(self):
        """
        Return the Tree with all the tasks contained in this Datastore

        @returns GTG.core.tree.Tree: a task tree (the main one)
        """
        return self._tasks

    # Tags functions ##########################################################
    def _add_new_tag(self, name, tag, filter_func, parameters, parent_id=None):
        """ Add tag into a tree """
        if self._tagstore.has_node(name):
            raise IndexError(f'tag {name} was already in the datastore')

        self._tasks.add_filter(name, filter_func, parameters=parameters)
        self._tagstore.add_node(tag, parent_id=parent_id)
        tag.set_save_callback(self.save)

    def new_tag(self, name, attributes={}, tid=None):
        """
        Create a new tag

        @returns GTG.core.tag.Tag: the new tag
        """
        parameters = {'tag': name}
        tag = Tag(name, req=self.requester, attributes=attributes, tid=tid)
        self._add_new_tag(name, tag, self.treefactory.tag_filter, parameters)
        return tag

    def new_search_tag(self, name, query, attributes={}, tid=None, save=True):
        """
        Create a new search tag

        @returns GTG.core.tag.Tag: the new search tag/None for a invalid query
        """
        try:
            parameters = parse_search_query(query)
        except InvalidQuery as error:
            log.warning("Problem with parsing query %r (skipping): %s", query, error.message)
            return None

        # Create own copy of attributes and add special attributes label, query
        init_attr = dict(attributes)
        init_attr["label"] = name
        init_attr["query"] = query

        tag = Tag(name, req=self.requester, attributes=init_attr, tid=tid)
        self._add_new_tag(name, tag, search_filter, parameters,
                          parent_id=SEARCH_TAG)

        if save:
            self.save_tagtree()

        return tag

    def remove_tag(self, name):
        """ Removes a tag from the tagtree """
        if self._tagstore.has_node(name):
            self._tagstore.del_node(name)
            self.save_tagtree()
        else:
            raise IndexError(f"There is no tag {name}")

    def rename_tag(self, oldname, newname):
        """ Give a tag a new name

        This function is quite high-level method. Right now,
        only renaming search bookmarks are implemented by removing
        the old one and creating almost identical one with the new name.

        NOTE: Implementation for regular tasks must be much more robust.
        You have to replace all occurences of tag name in tasks descriptions,
        their parameters and backend settings (synchronize only certain tags).

        Have a fun with implementing it!
        """

        tag = self.get_tag(oldname)

        if not tag.is_search_tag():
            for task_id in tag.get_related_tasks():

                # Store old tag attributes
                color = tag.get_attribute("color")
                icon = tag.get_attribute("icon")
                tid = tag.tid

                my_task = self.get_task(task_id)
                my_task.rename_tag(oldname, newname)

                # Restore attributes on tag
                new_tag = self.get_tag(newname)
                new_tag.tid = tid

                if color:
                    new_tag.set_attribute("color", color)

                if icon:
                    new_tag.set_attribute("icon", icon)

            self.remove_tag(oldname)
            self.save_tagtree()

            return None

        query = tag.get_attribute("query")
        self.remove_tag(oldname)

        # Make sure the name is unique
        if newname.startswith('!'):
            newname = '_' + newname

        label, num = newname, 1
        while self._tagstore.has_node(label):
            num += 1
            label = newname + " " + str(num)

        self.new_search_tag(label, query, {}, tag.tid)

    def get_tag(self, tagname):
        """
        Returns tag object

        @return GTG.core.tag.Tag
        """
        if self._tagstore.has_node(tagname):
            return self._tagstore.get_node(tagname)
        else:
            return None

    def load_tag_tree(self, tag_tree):
        """
        Loads the tag tree from a xml file
        """

        for element in tag_tree.iter('tag'):
            tid = element.get('id')
            name = element.get('name')
            color = element.get('color')
            icon = element.get('icon')
            parent = element.get('parent')
            nonactionable = element.get('nonactionable')

            tag_attrs = {}

            if color:
                tag_attrs['color'] = '#' + color

            if icon:
                tag_attrs['icon'] = icon

            if nonactionable:
                tag_attrs['nonactionable'] = nonactionable

            tag = self.new_tag(name, tag_attrs, tid)

            if parent:
                tag.set_parent(parent)

            # Add to idmap for quick lookup based on ID
            self.tag_idmap[tid] = tag

        self.tagfile_loaded = True


    def load_search_tree(self, search_tree):
        """Load saved searches tree."""

        for element in search_tree.iter('savedSearch'):
            tid = element.get('id')
            name = element.get('name')
            color = element.get('color')
            icon = element.get('icon')
            query = element.get('query')

            tag_attrs = {}

            if color:
                tag_attrs['color'] = color

            if icon:
                tag_attrs['icon'] = icon

            self.new_search_tag(name, query, tag_attrs, tid, False)


    def get_tag_by_id(self, tid):
        """Get a tag by its ID"""

        try:
            return self.tag_idmap[tid]
        except KeyError:
            return

    def save_tagtree(self):
        """ Saves the tag tree to an XML file """

        if not self.tagfile_loaded:
            return

        tags = self._tagstore.get_main_view().get_all_nodes()

        for backend in self.backends.values():
            if backend.get_name() == 'backend_localfile':
                backend.save_tags(tags, self._tagstore)


    # Tasks functions #########################################################
    def get_all_tasks(self):
        """
        Returns list of all keys of active tasks

        @return a list of strings: a list of task ids
        """
        return self._tasks.get_main_view().get_all_nodes()

    def has_task(self, tid):
        """
        Returns true if the tid is among the active or closed tasks for
        this DataStore, False otherwise.

        @param tid: Task ID to search for
        @return bool: True if the task is present
        """
        return self._tasks.has_node(tid)

    def get_task(self, tid):
        """
        Returns the internal task object for the given tid, or None if the
        tid is not present in this DataStore.

        @param tid: Task ID to retrieve
        @returns GTG.core.task.Task or None:  whether the Task is present
        or not
        """
        if self.has_task(tid):
            return self._tasks.get_node(tid)
        else:
            # log.error("requested non-existent task %s", tid)
            # This is not an error: it is normal to request a task which
            # might not exist yet.
            return None

    def task_factory(self, tid, newtask=False):
        """
        Instantiates the given task id as a Task object.

        @param tid: a task id. Must be unique
        @param newtask: True if the task has never been seen before
        @return Task: a Task instance
        """
        return Task(tid, self.requester, newtask)

    def new_task(self):
        """
        Creates a blank new task in this DataStore.
        New task is created in all the backends that collect all tasks (among
        them, the default backend). The default backend uses the same task id
        in its own internal representation.

        @return: The task object that was created.
        """
        task = self.task_factory(str(uuid.uuid4()), True)
        self._tasks.add_node(task)
        return task

    def push_task(self, task):
        """
        Adds the given task object to the task tree. In other words, registers
        the given task in the GTG task set.
        This function is used in mutual exclusion: only a backend at a time is
        allowed to push tasks.

        @param task: A valid task object  (a GTG.core.task.Task)
        @return bool: True if the task has been accepted
        """

        def adding(task):
            self._tasks.add_node(task)
            task.set_loaded()
            if self.is_default_backend_loaded:
                task.sync()
        if self.has_task(task.get_id()):
            return False
        else:
            # Thread protection
            adding(task)
            return True

    ##########################################################################
    # Backends functions
    ##########################################################################
    def get_all_backends(self, disabled=False):
        """
        returns list of all registered backends for this DataStore.

        @param disabled: If disabled is True, attaches also the list of
                disabled backends
        @return list: a list of TaskSource objects
        """
        result = []
        for backend in self.backends.values():
            if backend.is_enabled() or disabled:
                result.append(backend)
        return result

    def get_backend(self, backend_id):
        """
        Returns a backend given its id.

        @param backend_id: a backend id
        @returns GTG.core.datastore.TaskSource or None: the requested backend,
                                                        or None
        """
        if backend_id in self.backends:
            return self.backends[backend_id]
        else:
            return None

    def register_backend(self, backend_dic):
        """
        Registers a TaskSource as a backend for this DataStore

        @param backend_dic: Dictionary object containing all the
                            parameters to initialize the backend
                            (filename...). It should also contain the
                            backend class (under "backend"), and its
                            unique id (under "pid")
        """
        if "backend" in backend_dic:
            if "pid" not in backend_dic:
                log.error("registering a backend without pid.")
                return None
            backend = backend_dic["backend"]
            first_run = backend_dic["first_run"]

            # Checking that is a new backend
            if backend.get_id() in self.backends:
                log.error("registering already registered backend")
                return None
            # creating the TaskSource which will wrap the backend,
            # filtering the tasks that should hit the backend.
            source = TaskSource(requester=self.requester,
                                backend=backend,
                                datastore=self)

            if first_run:
                backend.this_is_the_first_run(None)

            self.backends[backend.get_id()] = source
            # we notify that a new backend is present
            self._backend_signals.backend_added(backend.get_id())
            # saving the backend in the correct dictionary (backends for
            # enabled backends, disabled_backends for the disabled ones)
            # this is useful for retro-compatibility
            if GenericBackend.KEY_ENABLED not in backend_dic:
                source.set_parameter(GenericBackend.KEY_ENABLED, True)
            if GenericBackend.KEY_DEFAULT_BACKEND not in backend_dic:
                source.set_parameter(GenericBackend.KEY_DEFAULT_BACKEND, True)
            # if it's enabled, we initialize it
            if source.is_enabled() and \
                    (self.is_default_backend_loaded or source.is_default()):
                source.initialize(connect_signals=False)
                # Filling the backend
                # Doing this at start is more efficient than
                # after the GUI is launched
                source.start_get_tasks()
            return source
        else:
            log.error("Tried to register a backend without a pid")

    def _activate_non_default_backends(self, sender=None):
        """
        Non-default backends have to wait until the default loads before
        being  activated. This function is called after the first default
        backend has loaded all its tasks.

        @param sender: not used, just here for signal compatibility
        """
        if self.is_default_backend_loaded:
            log.debug("spurious call")
            return

        self.is_default_backend_loaded = True
        for backend in self.backends.values():
            if backend.is_enabled() and not backend.is_default():
                self._backend_startup(backend)

    def _backend_startup(self, backend):
        """
        Helper function to launch a thread that starts a backend.

        @param backend: the backend object
        """

        def __backend_startup(self, backend):
            """
            Helper function to start a backend

            @param backend: the backend object
            """
            backend.initialize()
            backend.start_get_tasks()
            self.flush_all_tasks(backend.get_id())

        thread = threading.Thread(target=__backend_startup,
                                  args=(self, backend))
        thread.setDaemon(True)
        thread.start()

    def set_backend_enabled(self, backend_id, state):
        """
        The backend corresponding to backend_id is enabled or disabled
        according to "state".
        Disable:
        Quits a backend and disables it (which means it won't be
        automatically loaded next time GTG is started)
        Enable:
        Reloads a disabled backend. Backend must be already known by the
        Datastore

        @param backend_id: a backend id
        @param state: True to enable, False to disable
        """
        if backend_id in self.backends:
            backend = self.backends[backend_id]
            current_state = backend.is_enabled()
            if current_state is True and state is False:
                # we disable the backend
                # FIXME!!!
                threading.Thread(target=backend.quit,
                                 kwargs={'disable': True}).start()
            elif current_state is False and state is True:
                if self.is_default_backend_loaded is True:
                    self._backend_startup(backend)
                else:
                    # will be activated afterwards
                    backend.set_parameter(GenericBackend.KEY_ENABLED,
                                          True)

    def remove_backend(self, backend_id):
        """
        Removes a backend, and forgets it ever existed.

        @param backend_id: a backend id
        """
        if backend_id in self.backends:
            backend = self.backends[backend_id]
            if backend.is_enabled():
                self.set_backend_enabled(backend_id, False)
            # FIXME: to keep things simple, backends are not notified that they
            #       are completely removed (they think they're just
            #       deactivated). We should add a "purge" call to backend to
            #       let them know that they're removed, so that they can
            #       remove all the various files they've created. (invernizzi)

            # we notify that the backend has been deleted
            self._backend_signals.backend_removed(backend.get_id())
            del self.backends[backend_id]

    def backend_change_attached_tags(self, backend_id, tag_names):
        """
        Changes the tags for which a backend should store a task

        @param backend_id: a backend_id
        @param tag_names: the new set of tags. This should not be a tag object,
                          just the tag name.
        """
        backend = self.backends[backend_id]
        backend.set_attached_tags(tag_names)

    def flush_all_tasks(self, backend_id):
        """
        This function will cause all tasks to be checked against the backend
        identified with backend_id. If tasks need to be added or removed, it
        will be done here.
        It has to be run after the creation of a new backend (or an alteration
        of its "attached tags"), so that the tasks which are already loaded in
        the Tree will be saved in the proper backends

        @param backend_id: a backend id
        """

        def _internal_flush_all_tasks():
            backend = self.backends[backend_id]
            for task_id in self.get_all_tasks():
                if self.please_quit:
                    break
                backend.queue_set_task(task_id)
        t = threading.Thread(target=_internal_flush_all_tasks)
        t.start()
        self.backends[backend_id].start_get_tasks()

    def save(self, quit=False):
        """
        Saves the backends parameters.

        @param quit: If quit is true, backends are shut down
        """

        try:
            self.start_get_tasks_thread.join()
        except Exception:
            pass

        # we ask all the backends to quit first.
        if quit:
            # we quit backends in parallel
            threads_dic = {}

            for b in self.get_all_backends():
                thread = threading.Thread(target=b.quit)
                threads_dic[b.get_id()] = thread
                thread.start()

            for backend_id, thread in threads_dic.items():
                # after 20 seconds, we give up
                thread.join(20)

                alive = thread.is_alive()

                if alive:
                    log.error("The %s backend stalled while quitting",
                              backend_id)

        # we save the parameters
        for b in self.get_all_backends(disabled=True):
            config = self.conf.get_backend_config(b.get_name())


            for key, value in b.get_parameters().items():
                if key in ["backend", "xmlobject"]:
                    # We don't want parameters, backend, xmlobject:
                    # we'll create them at next startup
                    continue

                param_type = b.get_parameter_type(key)
                value = b.cast_param_type_to_string(param_type, value)
                config.set(str(key), value)

        config.save()

        #  Saving the tagstore
        self.save_tagtree()

    def request_task_deletion(self, tid):
        """
        This is a proxy function to request a task deletion from a backend

        @param tid: the tid of the task to remove
        """
        self.requester.delete_task(tid)

    def get_backend_mutex(self):
        """
        Returns the mutex object used by backends to avoid modifying a task
        at the same time.

        @returns: threading.Lock
        """
        return self._backend_mutex


class TaskSource():
    """
    Transparent interface between the real backend and the DataStore.
    Is in charge of connecting and disconnecting to signals
    """

    def __init__(self, requester, backend, datastore):
        """
        Instantiates a TaskSource object.

        @param requester: a Requester
        @param backend:  the backend being wrapped
        @param datastore: a Datastore
        """
        self.backend = backend
        self.req = requester
        self.backend.register_datastore(datastore)
        self.tasktree = datastore.get_tasks_tree().get_main_view()
        self.to_set = deque()
        self.to_remove = deque()
        self.please_quit = False
        self.task_filter = self.get_task_filter_for_backend()
        if log.isEnabledFor(logging.DEBUG):
            self.timer_timestep = 5
        else:
            self.timer_timestep = 1
        self.add_task_handle = None
        self.set_task_handle = None
        self.remove_task_handle = None
        self.to_set_timer = None

    def start_get_tasks(self):
        """ Loads all task from the backend and connects its signals
        afterwards. """
        self.backend.start_get_tasks()
        self._connect_signals()
        if self.backend.is_default():
            BackendSignals().default_backend_loaded()

    def get_task_filter_for_backend(self):
        """
        Filter that checks if the task should be stored in this backend.

        @returns function: a function that accepts a task and returns
                 True/False whether the task should be stored or not
        """

        def backend_filter(req, task, parameters):
            """
            Filter that checks if two tags sets intersect. It is used to check
            if a task should be stored inside a backend
            @param task: a task object
            @param tags_to_match_set: a *set* of tag names
            """
            try:
                tags_to_match_set = parameters['tags']
            except KeyError:
                return []
            all_tasks_tag = req.get_alltag_tag().get_name()
            if all_tasks_tag in tags_to_match_set:
                return True
            task_tags = set(task.get_tags_name())
            return task_tags.intersection(tags_to_match_set)

        attached_tags = self.backend.get_attached_tags()
        return lambda task: backend_filter(self.requester, task,
                                           {"tags": set(attached_tags)})

    def should_task_id_be_stored(self, task_id):
        """
        Helper function:  Checks if a task should be stored in this backend

        @param task_id: a task id
        @returns bool: True if the task should be stored
        """
        # task = self.req.get_task(task_id)
        # FIXME: it will be a lot easier to add, instead,
        # a filter to a tree and check that this task is well in the tree
#        return self.task_filter(task)
        return True

    def queue_set_task(self, tid, path=None):
        """
        Updates the task in the DataStore.  Actually, it adds the task to a
        queue to be updated asynchronously.

        @param task: The Task object to be updated.
        @param path: its path in TreeView widget => not used there
        """
        if self.should_task_id_be_stored(tid):
            if tid not in self.to_set and tid not in self.to_remove:
                self.to_set.appendleft(tid)
                self.__try_launch_setting_thread()
        else:
            self.queue_remove_task(tid, path)

    def launch_setting_thread(self, bypass_please_quit=False):
        """
        Operates the threads to set and remove tasks.
        Releases the lock when it is done.

        @param bypass_please_quit: if True, the self.please_quit
                                   "quit condition" is ignored. Currently,
                                   it's turned to true after the quit
                                   condition has been issued, to execute
                                   eventual pending operations.
        """
        while not self.please_quit or bypass_please_quit:
            try:
                tid = self.to_set.pop()
            except IndexError:
                break
            # we check that the task is not already marked for deletion
            # and that it's still to be stored in this backend
            # NOTE: no need to lock, we're reading
            if tid not in self.to_remove and \
                    self.should_task_id_be_stored(tid) and \
                    self.req.has_task(tid):
                task = self.req.get_task(tid)
                self.backend.queue_set_task(task)
        while not self.please_quit or bypass_please_quit:
            try:
                tid = self.to_remove.pop()
            except IndexError:
                break
            self.backend.queue_remove_task(tid)
        # we release the weak lock
        self.to_set_timer = None

    def queue_remove_task(self, tid, path=None):
        """
        Queues task to be removed.

        @param sender: not used, any value will do
        @param tid: The Task ID of the task to be removed
        """
        if tid not in self.to_remove:
            self.to_remove.appendleft(tid)
            self.__try_launch_setting_thread()

    def __try_launch_setting_thread(self):
        """
        Helper function to launch the setting thread, if it's not running
        """
        if self.to_set_timer is None and not self.please_quit:
            self.to_set_timer = threading.Timer(self.timer_timestep,
                                                self.launch_setting_thread)
            self.to_set_timer.setDaemon(True)
            self.to_set_timer.start()

    def initialize(self, connect_signals=True):
        """
        Initializes the backend and starts looking for signals.

        @param connect_signals: if True, it starts listening for signals
        """
        self.backend.initialize()
        if connect_signals:
            self._connect_signals()

    def _connect_signals(self):
        """
        Helper function to connect signals
        """
        if not self.add_task_handle:
            self.add_task_handle = self.tasktree.register_cllbck(
                'node-added', self.queue_set_task)
        if not self.set_task_handle:
            self.set_task_handle = self.tasktree.register_cllbck(
                'node-modified', self.queue_set_task)
        if not self.remove_task_handle:
            self.remove_task_handle = self.tasktree.register_cllbck(
                'node-deleted', self.queue_remove_task)

    def _disconnect_signals(self):
        """
        Helper function to disconnect signals
        """
        if self.add_task_handle:
            self.tasktree.deregister_cllbck('node-added',
                                            self.set_task_handle)
            self.add_task_handle = None
        if self.set_task_handle:
            self.tasktree.deregister_cllbck('node-modified',
                                            self.set_task_handle)
            self.set_task_handle = None
        if self.remove_task_handle:
            self.tasktree.deregister_cllbck('node-deleted',
                                            self.remove_task_handle)
            self.remove_task_handle = None

    def sync(self):
        """
        Forces the TaskSource to sync all the pending tasks
        """
        try:
            self.to_set_timer.cancel()
        except Exception:
            pass
        try:
            self.to_set_timer.join(3)
        except Exception:
            pass
        try:
            self.start_get_tasks_thread.join(3)
        except Exception:
            pass
        self.launch_setting_thread(bypass_please_quit=True)

    def quit(self, disable=False):
        """
        Quits the backend and disconnect the signals

        @param disable: if True, the backend is disabled.
        """
        self._disconnect_signals()
        self.please_quit = True
        self.sync()
        self.backend.quit(disable)

    def __getattr__(self, attr):
        """
        Delegates all the functions not defined here to the real backend
        (standard python function)

        @param attr: attribute to get
        """
        if attr in self.__dict__:
            return self.__dict__[attr]
        else:
            return getattr(self.backend, attr)
