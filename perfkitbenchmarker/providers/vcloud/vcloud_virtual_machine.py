# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
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
"""Class to represent a vCloud Virtual Machine object.

All VM specifics are self-contained and the class provides methods to
operate on the VM: boot, shutdown, etc.
"""
from collections import OrderedDict
import json
import logging
import re
import tempfile

from perfkitbenchmarker import errors
from perfkitbenchmarker import flags
from perfkitbenchmarker import linux_virtual_machine
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker import providers
from perfkitbenchmarker.configs import option_decoders
from perfkitbenchmarker.providers.vcloud import vcloud_disk
from perfkitbenchmarker.providers.vcloud import util

FLAGS = flags.FLAGS

CLOUD_CONFIG_TEMPLATE = '''#!/bin/bash
/usr/sbin/useradd -m -s /bin/bash -G wheel {0}
mkdir ~{0}/.ssh
echo '{1}' > ~{0}/.ssh/authorized_keys
chown {0}:{0} ~{0}/.ssh/authorized_keys
chmod 500 ~{0}/.ssh/authorized_keys
echo '{0} ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers
service firewalld stop
yum clean all
'''

LSBLK_REGEX = (r'NAME="(.*)"\s+MODEL="(.*)"\s+SIZE="(.*)"'
               r'\s+TYPE="(.*)"\s+MOUNTPOINT="(.*)"\s+LABEL="(.*)"')
LSBLK_PATTERN = re.compile(LSBLK_REGEX)

UBUNTU_IMAGE = 'Ubuntu'
RHEL_IMAGE = 'Redhat'

INSTANCE_EXISTS_STATUSES = frozenset(
    ['Powered on', 'Suspended', 'PAUSED', 'SHUTOFF', 'ERROR'])

class vCloudVmSpec(virtual_machine.BaseVmSpec):
  """Object containing the information needed to create a vCloudVirtualMachine.

  Attributes:
    vcloud_org: string. Organisation ID
    vcloud_vdc: string. vDC ID
  """

  CLOUD = providers.VCLOUD

  @classmethod
  def _ApplyFlags(cls, config_values, flag_values):
    """Modifies config options based on runtime flag values.

    Args:
      config_values: dict mapping config option names to provided values. May
          be modified by this function.
      flag_values: flags.FlagValues. Runtime flags that may override the
          provided config values.
    """
    super(vCloudVmSpec, cls)._ApplyFlags(config_values, flag_values)
    if flag_values['vcloud_vdc'].present:
      config_values['vcloud_vdc'] = flag_values.vcloud_vdc
    if flag_values['vcloud_network'].present:
      config_values['vcloud_network'] = flag_values.vcloud_network
    if flag_values['vcloud_catalog'].present:
      config_values['vcloud_catalog'] = flag_values.vcloud_catalog

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    """Gets decoder classes and constructor args for each configurable option.

    Returns:
      dict. Maps option name string to a (ConfigOptionDecoder class, dict) pair.
          The pair specifies a decoder class and its __init__() keyword
          arguments to construct in order to decode the named option.
    """
    result = super(vCloudVmSpec, cls)._GetOptionDecoderConstructions()
    result.update({
        'vcloud_catalog': (option_decoders.StringDecoder, {'default': None}),
        'vcloud_media': (option_decoders.StringDecoder, {'default': None}),
        'vcloud_network': (option_decoders.StringDecoder, {'default': None}),
        'vcloud_vdc': (option_decoders.StringDecoder, {'default': None})})
    return result

class vCloudVirtualMachine(virtual_machine.BaseVirtualMachine):
  """Object representing a vCloud Virtual Machine."""

  CLOUD = providers.VCLOUD
  DEFAULT_IMAGE = None

  def __init__(self, vm_spec):
    """Initialize a vCloud Virtual Machine

    Args:
      vm_spec: virtual_machine.BaseVirtualMachineSpec object of the VM.
    """
    super(vCloudVirtualMachine, self).__init__(vm_spec)
    self.id = None
    self.image = self.image or self.DEFAULT_IMAGE
    self.vcloud_catalog = vm_spec.vcloud_catalog
    self.vcloud_vdc = vm_spec.vcloud_vdc
    self.vcloud_network = vm_spec.vcloud_network
    self.allocated_disks = set()

  def _CreateDependencies(self):
    """Create dependencies prior creating the VM."""
    # TODO(meteorfox) Create security group (if applies)

  def _Create(self):
    """Creates a vCloud VM instance and waits until it's ACTIVE."""
    self._CreateInstance()
    self._CustomizeInstance()
    self._WaitForInstanceUntilActive()

  @vm_util.Retry()
  def _PostCreate(self):
    """Gets the VM's information."""
    get_cmd = util.vCloudCLICommand(self, 'vm', 'list')
    get_cmd.flags['vapp'] = self.name
    stdout, _, _ = get_cmd.Issue()
    resp = json.loads(stdout)
    self.internal_ip = resp['vms'][0]['IPs']
    self.ip_address = resp['vms'][0]['IPs']

  def _Exists(self):
    """Returns true if the VM exists otherwise returns false."""
    if self.id is None:
      return False
    get_cmd = util.vCloudCLICommand(self, 'vm', 'list')
    get_cmd.flags['vapp'] = self.id
    stdout, _, _ = get_cmd.Issue(suppress_warning=True)
    try:
      resp = json.loads(stdout)
    except ValueError:
      return False
    try:
      status = resp['vms'][0]['Status']
      return status in INSTANCE_EXISTS_STATUSES
    except:
      return False
    

  def _Delete(self):
    """Deletes a vCloud VM instance and waits until API returns 404."""
    if self.id is None:
      return
    self._DeleteInstance()
    self._WaitForInstanceUntilDeleted()

  def _DeleteDependencies(self):
    """Deletes dependencies that were need for the VM after the VM has been
    deleted."""
    # TODO(meteorfox) Delete security group (if applies)

  def _CreateInstance(self):
    """Generates and execute command for creating a vCloud VM."""
    create_cmd = self._GetCreateCommand()
    stdout, stderr, _ = create_cmd.Issue()
    if stderr:
      resp = json.loads(stderr)
      raise errors.Error(''.join(
          ('Non-recoverable error has occurred: %s\n' % str(resp),
           'Following command caused the error: %s' % repr(create_cmd),)))
    self.id = self.name

  def _GetCreateCommand(self):
    """Generates vCloud command for creating a vCloud VM.

    Args:
      tf: file object containing cloud-config script.

    Returns:
      vCloudCLICommand containing vcd-cli arguments to build a vCloud VM.
    """
    create_cmd = util.vCloudCLICommand(self, 'vapp', 'create')
    create_cmd.flags['vapp'] = self.name
    create_cmd.flags['catalog'] = self.vcloud_catalog
    create_cmd.flags['template'] = self.image
    create_cmd.flags['network'] = self.vcloud_network
    create_cmd.flags['vm'] = self.name
    create_cmd.flags['mode'] = 'pool'
    metadata = ['owner=%s' % FLAGS.owner]
    return create_cmd


  def _CustomizeInstance(self):
    """Powers the instance on"""
    if self.id is None:
      return False
    with tempfile.NamedTemporaryFile(dir=vm_util.GetTempDir(),
                                     prefix='user-data') as tf:
      with open(self.ssh_public_key) as f:
        public_key = f.read().rstrip('\n')
      tf.write(CLOUD_CONFIG_TEMPLATE.format(self.user_name, public_key))
      tf.flush()
      get_cmd = util.vCloudCLICommand(self, 'vapp', 'customize')
      get_cmd.flags['vapp'] = self.id
      get_cmd.flags['vm'] = self.id
      get_cmd.flags['file'] = tf.name
      stdout, stderr, _ = get_cmd.Issue(suppress_warning=True)
    try:
      resp = json.loads(stdout)
    except ValueError:
      return False
    #TODO(DBW)
    #status = resp['Status']
    #return status in INSTANCE_EXISTS_STATUSES


  def _PowerOnInstance(self):
    """Powers the instance on"""
    get_cmd = util.vCloudCLICommand(self, 'vapp', 'power-on')
    get_cmd.flags['vapp'] = self.id
    get_cmd.flags['file'] = tf.name
    stdout, _, _ = get_cmd.Issue(suppress_warning=True)
    try:
      resp = json.loads(stdout)
    except ValueError:
      return False
    #TODO(DBW)
    #status = resp['Status']
    #return status in INSTANCE_EXISTS_STATUSES


  @vm_util.Retry(poll_interval=5, max_retries=720, log_errors=False,
                 retryable_exceptions=(errors.Resource.RetryableCreationError,))
  def _WaitForInstanceUntilActive(self):
    """Waits until instance achieves non-transient state."""
    get_cmd = util.vCloudCLICommand(self, 'vapp', 'info')
    get_cmd.flags['vapp'] = self.name
    stdout, stderr, _ = get_cmd.Issue()
    if stdout:
      instance = json.loads(stdout)
      if instance['vapp'][4]['Value'] == 'Powered on':
        logging.info('VM: %s is up and running.' % self.name)
        return
      elif instance['vapp'][4]['Value'] == 'ERROR':
        logging.error('VM: %s failed to boot.' % self.name)
        raise errors.VirtualMachine.VmStateError()
    raise errors.Resource.RetryableCreationError(
        'VM: %s is not running. Retrying to check status.' % self.name)

  def _DeleteInstance(self):
    """Executes delete command for removing a vCloud VM."""
    cmd = util.vCloudCLICommand(self, 'vapp', 'delete')
    cmd.flags['vapp'] = self.name
    stdout, _, _ = cmd.Issue(suppress_warning=True)
    #resp = json.loads(stdout)
    # TODO - need to check for Task.@Status = success
    #if 'result' not in resp or 'Deleting' not in resp['result']:
    #  raise errors.Resource.RetryableDeletionError()

  @vm_util.Retry(poll_interval=5, max_retries=-1, timeout=300,
                 log_errors=False,
                 retryable_exceptions=(errors.Resource.RetryableDeletionError,))
  def _WaitForInstanceUntilDeleted(self):
    """Waits until instance has been fully removed, or deleted."""
    get_cmd = util.vCloudCLICommand(self, 'vapp', 'info')
    get_cmd.flags['vapp'] = self.name
    stdout, stderr, _ = get_cmd.Issue()
    if "not found" in stderr:
      logging.info('VM: %s has been successfully deleted.' % self.name)
      return

    #instance = json.loads(stdout)
    #if instance['Status'] == 'ERROR':
    #  logging.error('VM: %s failed to delete.' % self.name)
    #  raise errors.VirtualMachine.VmStateError()

    #if instance['Status'] == 'DELETED':
    #    logging.info('VM: %s has been successfully deleted.' % self.name)
    #else:
    #  raise errors.Resource.RetryableDeletionError(
    #      'VM: %s has not been deleted. Retrying to check status.' % self.name)

  def OnStartup(self):
    """Executes commands on the VM immediately after it has booted."""
    super(vCloudVirtualMachine, self).OnStartup()
    self.boot_device = self._GetBootDevice()

  def CreateScratchDisk(self, disk_spec):
      """Create a VM's scratch disk.

      Args:
        disk_spec: virtual_machine.BaseDiskSpec object of the disk.
      """
      if disk_spec.disk_type == vcloud_disk.BOOT:  # Ignore num_striped_disks
        self._AllocateBootDisk(disk_spec)
      elif disk_spec.disk_type == vcloud_disk.LOCAL:
        self._AllocateLocalDisk(disk_spec)
      else:
        raise errors.Error('Unsupported data disk type: %s' % disk_spec.disk_type)

  def _AllocateBootDisk(self, disk_spec):
    """Allocate the VM's boot, or system, disk as the scratch disk.

    Boot disk can only be allocated once. If multiple data disks are required
    it will raise an error.

    Args:
      disk_spec: virtual_machine.BaseDiskSpec object of the disk.

    Raises:
      errors.Error when boot disk has already been allocated as a data disk.
    """
    #if self.boot_disk_allocated:
    #  raise errors.Error('Only one boot disk can be created per VM')
    #device_path = '/dev/%s' % self.boot_device['name']
    scratch_disk = vcloud_disk.vCloudBootDisk(
        disk_spec, 'boot-disk')
    #self.boot_disk_allocated = True
    self.scratch_disks.append(scratch_disk)
    #scratch_disk.Create()
    path = disk_spec.mount_point
    mk_cmd = 'sudo mkdir -p {0}; sudo chown -R $USER:$USER {0};'.format(path)
    self.RemoteCommand(mk_cmd)


  def _AllocateLocalDisk(self, disk_spec):
    """Allocate the VM's local disks (included with the VM), as a data disk(s).

    A local disk can only be allocated once per data disk.

    Args:
      disk_spec: virtual_machine.BaseDiskSpec object of the disk.
    """
    block_devices = self._GetBlockDevices()
    free_blk_devices = self._GetFreeBlockDevices(block_devices, disk_spec)
    disks = []
    for i in range(disk_spec.num_striped_disks):
      local_device = free_blk_devices[i]
      disk_name = '%s-local-disk-%d' % (self.name, i)
      device_path = '/dev/%s' % local_device['name']
      local_disk = vcloud_disk.vCloudLocalDisk(
          disk_spec, disk_name, device_path)
      self.allocated_disks.add(local_disk)
      disks.append(local_disk)
    self._CreateScratchDiskFromDisks(disk_spec, disks)

  def _GetFreeBlockDevices(self, block_devices, disk_spec):
    """Returns available block devices that are not in used as data disk or as
    a boot disk.

    Args:
      block_devices: list of dict containing information about all block devices
          in the VM.
      disk_spec: virtual_machine.BaseDiskSpec of the disk.

    Returns:
      list of dicts of only block devices that are not being used.

    Raises:
      errors.Error Whenever there are no available block devices.
    """
    free_blk_devices = []
    for dev in block_devices:
      if self._IsDiskAvailable(dev):
        free_blk_devices.append(dev)
    if not free_blk_devices:
      raise errors.Error(
          ''.join(('Machine type %s does not include' % self.machine_type,
                   ' local disks. Please use a different disk_type,',
                   ' or a machine_type that provides local disks.')))
    elif len(free_blk_devices) < disk_spec.num_striped_disks:
      raise errors.Error('Not enough local data disks. '
                         'Requesting %d disk(s) but only %d available.'
                         % (disk_spec.num_striped_disks, len(free_blk_devices)))
    return free_blk_devices

  def _GetBlockDevices(self):
    """Execute command on VM to gather all block devices in the VM.

    Returns:
      list of dicts block devices in the VM.
    """
    stdout, _ = self.RemoteCommand(
        'sudo lsblk -o NAME,MODEL,SIZE,TYPE,MOUNTPOINT,LABEL -n -b -P')
    lines = stdout.splitlines()
    groups = [LSBLK_PATTERN.match(line) for line in lines]
    tuples = [g.groups() for g in groups if g]
    colnames = ('name', 'model', 'size_bytes', 'type', 'mountpoint', 'label',)
    blk_devices = [dict(zip(colnames, t)) for t in tuples]
    for d in blk_devices:
      d['model'] = d['model'].rstrip()
      d['label'] = d['label'].rstrip()
      d['size_bytes'] = int(d['size_bytes'])
    return blk_devices

  def _GetBootDevice(self):
    """Returns backing block device where '/' is mounted on.

    Returns:
      dict blk device data

    Raises:
      errors.Error indicates that could not find block device with '/'.
    """
    blk_devices = self._GetBlockDevices()
    boot_blk_device = None
    for dev in blk_devices:
      if dev['mountpoint'] == '/':
        boot_blk_device = dev
        break
    if boot_blk_device is None:  # Unlikely
      raise errors.Error('Could not find disk with "/" root mount point.')
    if boot_blk_device['type'] == 'lvm':   # If LVM disk, then find the /boot mount point.
      boot_blk_device = None
      for dev in blk_devices:
        if dev['mountpoint'] == '/boot':
          boot_blk_device = dev
          break
      if boot_blk_device is None:
        raise errors.Error('root mount point is LVM, and could not find "/boot".')
    if boot_blk_device['type'] != 'part':
      return boot_blk_device
    return self._FindBootBlockDevice(blk_devices, boot_blk_device)

  def _FindBootBlockDevice(self, blk_devices, boot_blk_device):
    """Helper method to search for backing block device of a partition."""
    blk_device_name = boot_blk_device['name'].rstrip('0123456789')
    for dev in blk_devices:
      if dev['type'] == 'disk' and dev['name'] == blk_device_name:
        boot_blk_device = dev
        return boot_blk_device

  def _IsDiskAvailable(self, blk_device):
    """Returns True if a block device is available.

    An available disk, is a disk that has not been allocated previously as
    a data disk, or is not being used as boot disk.
    """
    return (blk_device['type'] != 'part' and
            blk_device['type'] != 'lvm' and
            blk_device['name'] != self.boot_device['name'] and
            blk_device['name'] != 'fd0' and
            'config' not in blk_device['label'] and
            blk_device['name'] not in self.allocated_disks)


class DebianBasedvCloudVirtualMachine(vCloudVirtualMachine,
                                         linux_virtual_machine.DebianMixin):
  DEFAULT_IMAGE = UBUNTU_IMAGE


class RhelBasedvCloudVirtualMachine(vCloudVirtualMachine,
                                       linux_virtual_machine.RhelMixin):
  DEFAULT_IMAGE = RHEL_IMAGE

