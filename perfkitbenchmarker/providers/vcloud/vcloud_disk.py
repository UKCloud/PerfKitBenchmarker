# Copyright 2016 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module containing classes related to vCloud disks.
"""

from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import providers


BOOT = 'boot'
LOCAL = 'local'

DISK_TYPE = {
    disk.STANDARD: BOOT,
    disk.LOCAL: LOCAL
}

disk.RegisterDiskTypeMap(providers.VCLOUD, DISK_TYPE)


class vCloudDiskSpec(disk.BaseDiskSpec):
  """Object containing the information needed to create a
  vCloudDisk.

  Attributes:
  """
  CLOUD = providers.VCLOUD

  @classmethod
  def _ApplyFlags(cls, config_values, flag_values):
    super(vCloudDiskSpec, cls)._ApplyFlags(config_values, flag_values)

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    result = super(vCloudDiskSpec, cls)._GetOptionDecoderConstructions()
    return result


class vCloudDisk(disk.BaseDisk):
  """Base class for vCloud disks."""

  def __init__(self, disk_spec, name):
    super(vCloudDisk, self).__init__(disk_spec)
    self.name = name
    self.attached_vm_id = None

  def _Create(self):
    """Creates the disk."""
    raise NotImplementedError()

  def _Delete(self):
    """Deletes the disk."""
    raise NotImplementedError()

  def Attach(self, vm):
    """Attaches disk, if needed, to the VM."""
    self.attached_vm_id = vm.id

  def Detach(self):
    """Detaches disk, if needed, from the VM."""
    self.attached_vm_id = None


class vCloudLocalDisk(vCloudDisk):
  """vCloudLocalDisk is a disk object to represent an ephemeral storage disk
  locally attached to an instance.
  """

  def __init__(self, disk_spec, name, device_path):
    super(vCloudLocalDisk, self).__init__(disk_spec, name)
    self.exists = False
    self.device_path = device_path
    self.name = name

  def _Create(self):
    self.exists = True

  def _Delete(self):
    self.exists = False

  def _Exists(self):
    return self.exists


class vCloudBootDisk(vCloudLocalDisk):
  """vCloudBootDisk is a disk object to represent the root (boot) disk of an
  instance. Boot disk provides a directory path as a scratch disk space for a
  benchmark, but does not allow its backing block device to be formatted, or
  its mount point to be changed.
  """

  def __init__(self, disk_spec, device_path):
    super(vCloudBootDisk, self).__init__(disk_spec, 'boot-disk', device_path)
    self.mount_point = disk_spec.mount_point

