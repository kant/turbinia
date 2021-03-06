# Copyright 2015 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Turbinia task."""

from __future__ import unicode_literals

from datetime import datetime
import errno
import json
import logging
import os
import platform
import time
import traceback
import uuid

from turbinia import config
from turbinia import output_writers
from turbinia import TurbiniaException


class TurbiniaTaskResult(object):
  """Object to store task results to be returned by a TurbiniaTask.

  Attributes:
        base_output_dir: Base path for local output
        output_dir: Full path for local output
        error: Dict of error data ('error' and 'traceback' are some valid keys)
        evidence: List of newly created Evidence objects.
        input_evidence: The evidence this task processed.
        run_time: Length of time the task ran for.
        start_time: Datetime object of when the task was started
        status: A one line descriptive task status.
        successful: Bool indicating success status.
        task_id: Task ID of the parent task.
        task_name: Name of parent task.
        worker_name: Name of worker task executed on.
        _log: A list of log messages
        _output_writers: A list of output writer objects
  """

  # The list of attributes that we will persist into storage
  STORED_ATTRIBUTES = ['status', 'successful']

  def __init__(
      self,
      evidence=None,
      input_evidence=None,
      task_id=None,
      task_name=None,
      base_output_dir=None):
    """Initialize the TurbiniaTaskResult object."""

    self.evidence = evidence if evidence else []
    self.input_evidence = input_evidence if input_evidence else []
    self.task_id = task_id
    self.task_name = task_name
    self.base_output_dir = base_output_dir

    self.start_time = datetime.now()
    self.run_time = None
    self.successful = None
    self.status = None
    self.error = {}
    self.worker_name = platform.node()
    # TODO(aarontp): Create mechanism to grab actual python logging data.
    self._log = []
    self._output_writers = output_writers.GetOutputWriters(self)
    self.output_dir = self.get_local_output_dir()

  def close(self, success, status=None):
    """Handles closing of this result and writing logs.

    Args:
      success: Bool indicating task success
      status: One line descriptive task status.
    """
    # TODO(aarontp): Running this close() method is now mandatory to make sure
    # output is copied and that the results object can be serialized properly.
    # We should move this to be part of a closure or decorator on the main
    # TurbiniaTask.run function.
    self.successful = success
    self.run_time = datetime.now() - self.start_time
    if not status:
      status = u'Completed successfully in {0:s} on {1:s}'.format(
          str(self.run_time), self.worker_name)
    self.log(status)

    # Write result log info to file
    logfile = os.path.join(self.output_dir, u'worker-log.txt')
    if self.output_dir and os.path.exists(self.output_dir):
      with open(logfile, 'w') as f:
        f.write('\n'.join(self._log))
        f.write('\n')
    self.save_local_file(logfile)

    [self.save_local_file(e.local_path) for e in self.evidence if e.local_path]

    self.input_evidence.postprocess()
    # Unset the writers during the close because they don't serialize
    self._output_writers = None
    self.status = status

  def get_local_output_dir(self):
    """Gets the local output dir from the local output writer.

    Returns:
      String to locally created output directory.

    Raises:
      TurbiniaException: If no local output writer with output_dir is found.
    """
    if not self._output_writers:
      raise TurbiniaException('No output writers found.')

    # Get the local writer
    writer = [w for w in self._output_writers if w.name == 'LocalWriter'][0]
    if not hasattr(writer, 'output_dir'):
      raise TurbiniaException(
          'Local output writer does not have output_dir attribute.')

    if not writer.output_dir:
      raise TurbiniaException(
          'Local output writer attribute output_dir is not set')

    return writer.output_dir


  def log(self, log_msg):
    """Add a log message to the result object.

    Args:
      log_msg: A log message string.
    """
    logging.info(log_msg)
    self._log.append(log_msg)

  def add_evidence(self, evidence):
    """Populate the results list.

    Args:
        evidence: Evidence object
    """
    self.evidence.append(evidence)

  def save_local_file(self, file_):
    """Saves local file by writing to all non-local output writers.

    Args:
      file_ (string): Path to file to save.
    """
    for writer in self._output_writers:
      if writer.name != 'LocalOutputWriter':
        writer.write(file_)

  def set_error(self, error, traceback_):
    """Add error and traceback.

    Args:
        error: Short string describing the error.
        traceback: Traceback of the error.
    """
    self.error['error'] = error
    self.error['traceback'] = traceback_


class TurbiniaTask(object):
  """Base class for Turbinia tasks.

  Attributes:
      id: Unique Id of task (string of hex)
      name: Name of task
      base_output_dir: The base directory that output will go into.  Per-task
                       directories will be created under this.
      output_dir: The directory output will go into (including per-task folder).
      result: A TurbiniaTaskResult object.
      state_key: A key used to manage task state
      stub: The task manager implementation specific task stub that exists
            server side to keep a reference to the remote task objects.  For PSQ
            this is a task result object, but other implementations have their
            own stub objects.
  """

  # The list of attributes that we will persist into storage
  STORED_ATTRIBUTES = ['id', 'name']

  def __init__(self, name=None, base_output_dir=None):
    """Initialization for TurbiniaTask."""
    self.id = uuid.uuid4().hex
    self.name = name if name else self.__class__.__name__
    self.base_output_dir = base_output_dir
    self.output_dir = None
    self.result = None
    self.state_key = None
    self.stub = None

  def setup(self, evidence):
    """Perform common setup operations and runtime environment.

    Even though TurbiniaTasks are initially instantiated by the Jobs under the
    Task Manager, this setup method needs to be run from the task on the worker
    because it handles setting up the task runtime environment.

    Args:
      evidence: An Evidence object to process.

    Returns:
      A TurbiniaTaskResult object.

    Raises:
      TurbiniaException: If the evidence can not be found.
    """
    self.result = TurbiniaTaskResult(
        task_id=self.id,
        task_name=self.name,
        input_evidence=evidence,
        base_output_dir=self.base_output_dir)
    self.output_dir = self.result.output_dir
    if evidence.local_path and not os.path.exists(evidence.local_path):
      raise TurbiniaException(
          'Evidence local path {0:s} does not exist'.format(
              evidence.local_path))
    evidence.preprocess()
    return self.result

  def run_wrapper(self, evidence):
    """Wrapper to manage TurbiniaTaskResults and exception handling.

    This wrapper should be called to invoke the run() methods so it can handle
    the management of TurbiniaTaskResults and the exception handling.  Otherwise
    details from exceptions in the worker cannot be propogated back to the
    Turbinia TaskManager.

    Args:
      evidence: Evidence object

    Returns:
      A TurbiniaTaskResult object
    """
    result = self.setup(evidence)
    try:
      result = self.run(evidence, result)
    # pylint: disable=broad-except
    except Exception as e:
      msg = 'Task failed with exception: [{0!s}]'.format(e)
      result.close(success=False, status=msg)
      result.set_error(e.message, traceback.format_exc())

    return result

  def run(self, evidence, result):
    """Entry point to execute the task.

    Args:
      evidence: Evidence object.
      result: A TurbiniaTaskResult object to place task results into.

    Returns:
        TurbiniaTaskResult object.
    """
    raise NotImplementedError
