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

from perfkitbenchmarker import flags

flags.DEFINE_string('vcloud_path',
                    default='vcd',
                    help='The path for the rack CLI binary.')

flags.DEFINE_string('vcloud_vdc', default=None,
                    help='A string indicating which virtual datacenter to use.')

flags.DEFINE_string('vcloud_network', None,
                    help='A string indicating which vCloud network to use.')

flags.DEFINE_string('vcloud_catalog', None,
                    help='A string indicating which vCloud catalog to use.')

flags.DEFINE_string('vcloud_gateway', None,
                    help='The vCloud Gateway that the VMs site behind.')

flags.DEFINE_string('vcloud_publicip', None,
                    help='A public IP address to NAT access to VMs behind.')
