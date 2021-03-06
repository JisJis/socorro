# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import time
import unittest

from nose.tools import ok_

from  socorro.lib.threaded_task_manager import ThreadedTaskManager, \
      ThreadedTaskManagerWithConfigSetup, \
      default_task_func
from socorro.lib.util import DotDict, SilentFakeLogger


class TestFileSystemRawCrashStorage(unittest.TestCase):

    def setUp(self):
        self.logger = SilentFakeLogger()

    def tearDown(self):
        pass

    def test_constuctor1(self):
        config = DotDict()
        config.logger = self.logger
        config.number_of_threads = 1
        config.maximum_queue_size = 1
        ttm = ThreadedTaskManager(config)
        try:
            ok_(ttm.config == config)
            ok_(ttm.logger == self.logger)
            ok_(ttm.task_func == default_task_func)
            ok_(ttm.quit == False)
        finally:
            # we got threads to join
            ttm._kill_worker_threads()

    def test_start1(self):
        config = DotDict()
        config.logger = self.logger
        config.number_of_threads = 1
        config.maximum_queue_size = 1
        ttm = ThreadedTaskManager(config)
        try:
            ttm.start()
            time.sleep(0.2)
            ok_(ttm.queuing_thread.isAlive(),
                            "the queing thread is not running")
            ok_(len(ttm.thread_list) == 1,
                            "where's the worker thread?")
            ok_(ttm.thread_list[0].isAlive(),
                            "the worker thread is stillborn")
            ttm.stop()
            ok_(ttm.queuing_thread.isAlive() == False,
                            "the queuing thread did not stop")
        except Exception:
            # we got threads to join
            ttm.wait_for_completion()

    def test_doing_work_with_one_worker(self):
        config = DotDict()
        config.logger = self.logger
        config.number_of_threads = 1
        config.maximum_queue_size = 1
        my_list = []

        def insert_into_list(anItem):
            my_list.append(anItem)

        ttm = ThreadedTaskManager(config,
                                  task_func=insert_into_list
                                 )
        try:
            ttm.start()
            time.sleep(0.2)
            ok_(len(my_list) == 10,
                            'expected to do 10 inserts, '
                               'but %d were done instead' % len(my_list))
            ok_(my_list == range(10),
                            'expected %s, but got %s' % (range(10), my_list))
            ttm.stop()
        except Exception:
            # we got threads to join
            ttm.wait_for_completion()
            raise

    def test_doing_work_with_two_workers_and_generator(self):
        config = DotDict()
        config.logger = self.logger
        config.number_of_threads = 2
        config.maximum_queue_size = 2
        my_list = []

        def insert_into_list(anItem):
            my_list.append(anItem)

        ttm = ThreadedTaskManager(config,
                                  task_func=insert_into_list,
                                  job_source_iterator=(((x,), {}) for x in
                                                       xrange(10))
                                 )
        try:
            ttm.start()
            time.sleep(0.2)
            ok_(len(ttm.thread_list) == 2,
                            "expected 2 threads, but found %d"
                              % len(ttm.thread_list))
            ok_(len(my_list) == 10,
                            'expected to do 10 inserts, '
                              'but %d were done instead' % len(my_list))
            ok_(sorted(my_list) == range(10),
                            'expected %s, but got %s' % (range(10),
                                                         sorted(my_list)))
        except Exception:
            # we got threads to join
            ttm.wait_for_completion()
            raise

    def test_doing_work_with_two_workers_and_config_setup(self):
        def new_iter():
            for x in xrange(5):
                yield ((x,), {})

        my_list = []

        def insert_into_list(anItem):
            my_list.append(anItem)

        config = DotDict()
        config.logger = self.logger
        config.number_of_threads = 2
        config.maximum_queue_size = 2
        config.job_source_iterator = new_iter
        config.task_func = insert_into_list
        ttm = ThreadedTaskManagerWithConfigSetup(config)
        try:
            ttm.start()
            time.sleep(0.2)
            ok_(len(ttm.thread_list) == 2,
                            "expected 2 threads, but found %d"
                              % len(ttm.thread_list))
            ok_(len(my_list) == 5,
                            'expected to do 5 inserts, '
                              'but %d were done instead' % len(my_list))
            ok_(sorted(my_list) == range(5),
                            'expected %s, but got %s' % (range(5),
                                                         sorted(my_list)))
        except Exception:
            # we got threads to join
            ttm.wait_for_completion()
            raise

    # failure tests

    count = 0

    def test_task_raises_unexpected_exception(self):
        global count
        count = 0

        def new_iter():
            for x in xrange(10):
                yield (x,)

        my_list = []

        def insert_into_list(anItem):
            global count
            count += 1
            if count == 4:
                raise Exception('Unexpected')
            my_list.append(anItem)

        config = DotDict()
        config.logger = self.logger
        config.number_of_threads = 1
        config.maximum_queue_size = 1
        config.job_source_iterator = new_iter
        config.task_func = insert_into_list
        ttm = ThreadedTaskManagerWithConfigSetup(config)
        try:
            ttm.start()
            time.sleep(0.2)
            ok_(len(ttm.thread_list) == 1,
                            "expected 1 threads, but found %d"
                              % len(ttm.thread_list))
            ok_(sorted(my_list) == [0, 1, 2, 4, 5, 6, 7, 8, 9],
                            'expected %s, but got %s'
                              % ([0, 1, 2, 5, 6, 7, 8, 9], sorted(my_list)))
            ok_(len(my_list) == 9,
                            'expected to do 9 inserts, '
                              'but %d were done instead' % len(my_list))
        except Exception:
            # we got threads to join
            ttm.wait_for_completion()
            raise
