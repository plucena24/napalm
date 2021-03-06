# Copyright 2015 Spotify AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

from pyEOS import EOS
from pyEOS.exceptions import CommandError, ConfigReplaceError

from base import NetworkDriver

from exceptions import MergeConfigException, ReplaceConfigException

from datetime import datetime
import time

from utils.string_parsers import colon_separated_string_to_dict, hyphen_range


class EOSDriver(NetworkDriver):

    def __init__(self, hostname, username, password):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.device = EOS(hostname, username, password, use_ssl=True)
        self.config_replace = False
        self.candidate_configuration = list()
        self.config_session = None

    def open(self):
        self.device.open()

    def close(self):
        self.device.close()

    def _load_and_test_config(self, filename, config, overwrite):
        if filename is None:
            self.candidate_configuration = config
        else:
            with open(filename) as f:
                self.candidate_configuration = f.read()

        self.candidate_configuration = self.candidate_configuration.split('\n')

        # If you send empty commands the whole thing breaks so we have to remove them
        clean_candidate = list()
        for line in self.candidate_configuration:
            if not line.strip() == '':
                clean_candidate.append(line)

        self.candidate_configuration = list(clean_candidate)
        test_config = list(clean_candidate)

        if 'end' in test_config:
            test_config.remove('end')

        if overwrite:
            test_config.insert(0, 'configure session test')
            test_config.append('abort')
        else:
            self.config_session = 'napalm_commit_%s' % datetime.now().strftime('%Y%m%d-%H%m%s')
            test_config.insert(0, 'configure session %s' % self.config_session)
            test_config.append('end')

        output = self.device.run_commands(test_config)

    def load_replace_candidate(self, filename=None, config=None):
        self.config_replace = True
        self.device.load_candidate_config(filename=filename, config=config)

    def load_merge_candidate(self, filename=None, config=None):
        try:
            self._load_and_test_config(filename=filename, config=config, overwrite=False)
            self.config_replace = False
        except CommandError as e:
            self.discard_config()
            raise MergeConfigException(e.message)

    def compare_config(self):
        if self.config_replace:
            return self.device.compare_config()
        else:
            commands = ['show session-config named %s diffs' % self.config_session]
            return self.device.run_commands(commands, format='text')[1]['output']

    def _commit_replace(self):
        try:
            self.device.replace_config()
        except ConfigReplaceError as e:
            raise ReplaceConfigException(e.message)

    def _commit_merge(self):
        self.candidate_configuration.insert(0, 'copy startup-config flash:rollback-0')
        self.candidate_configuration.insert(0, 'configure session %s' % self.config_session)
        if 'end' in self.candidate_configuration:
            self.candidate_configuration.remove('end')
        self.candidate_configuration.append('commit')
        self.device.run_commands(self.candidate_configuration)

    def commit_config(self):
        if self.config_replace:
            self._commit_replace()
        else:
            self._commit_merge()
        self.device.run_commands(['write memory'])

    def discard_config(self):
        if self.config_session is not None:
            commands = ['configure session %s' % self.config_session, 'abort']
            self.device.run_commands(commands)
            self.config_session = None
        self.device.load_candidate_config(config=self.device.get_config(format='text'))

    def rollback(self):
        self.device.run_commands(['configure replace flash:rollback-0'])
        self.device.run_commands(['write memory'])
        self.device.load_candidate_config(config=self.device.get_config(format='text'))

    def get_facts(self):
        output = self.device.show_version()
        uptime = time.time() - output['bootupTimestamp']

        interfaces = self.device.show_interfaces_status()['interfaceStatuses'].keys()

        return {
            'vendor': u'Arista',
            'model': output['modelName'],
            'serial_number': output['serialNumber'],
            'os_version': output['internalVersion'],
            'uptime': uptime,
            'interface_list': interfaces
        }

    def get_interfaces(self):
        def _process_counters():
            interfaces[interface]['counters'] = dict()
            if counters is None:
                interfaces[interface]['counters']['tx_packets'] = -1
                interfaces[interface]['counters']['rx_packets'] = -1
                interfaces[interface]['counters']['tx_errors'] = -1
                interfaces[interface]['counters']['rx_errors'] = -1
                interfaces[interface]['counters']['tx_discards'] = -1
                interfaces[interface]['counters']['rx_discards'] = -1
            else:
                interfaces[interface]['counters']['tx_packets'] = counters['outUcastPkts'] + \
                                                      counters['outMulticastPkts'] + \
                                                      counters['outBroadcastPkts']
                interfaces[interface]['counters']['rx_packets'] = counters['inUcastPkts'] + \
                                                      counters['inMulticastPkts'] + \
                                                      counters['inBroadcastPkts']

                interfaces[interface]['counters']['tx_errors'] = counters['totalOutErrors']
                interfaces[interface]['counters']['rx_errors'] = counters['totalInErrors']

                interfaces[interface]['counters']['tx_discards'] = counters['outDiscards']
                interfaces[interface]['counters']['rx_discards'] = counters['inDiscards']

        def _process_routed_interface():
            interface_json = values.pop("interfaceAddress", [])
            interfaces[interface]['ip_address_v4'] = list()

            if len(interface_json) > 0:
                interface_json = interface_json[0]
                interfaces[interface]['ip_address_v4'].append('{}/{}'.format(
                    interface_json['primaryIp']['address'], interface_json['primaryIp']['maskLen'])
                )

                for sec_ip, sec_values in interface_json['secondaryIps'].iteritems():
                    interfaces[interface]['ip_address_v4'].append('{}/{}'.format(sec_ip, sec_values['maskLen']))

        def _process_switched_interface():
            data = colon_separated_string_to_dict(switchport_data['output'])

            if data[u'Operational Mode'] == u'static access':
                interfaces[interface]['switchport_mode'] = 'access'
                interfaces[interface]['access_vlan'] = int(data[u'Access Mode VLAN'].split()[0])
            elif data[u'Operational Mode'] == u'trunk':
                interfaces[interface]['switchport_mode'] = 'trunk'
                interfaces[interface]['native_vlan'] = int(data[u'Trunking Native Mode VLAN'].split()[0])

                if data[u'Trunking VLANs Enabled'] == u'ALL':
                    interfaces[interface]['trunk_vlans'] = range(1,4095)
                else:
                    interfaces[interface]['trunk_vlans'] = hyphen_range(data[u'Trunking VLANs Enabled'])

        output = self.device.show_interfaces()

        interfaces = dict()

        for interface, values in output['interfaces'].iteritems():
            interfaces[interface] = dict()

            interfaces[interface]['description'] = values['description']

            status = values['lineProtocolStatus']

            if status == 'up':
                interfaces[interface]['status'] = 'up'
            else:
                interfaces[interface]['status'] = 'down'

            interfaces[interface]['last_flapped'] = values.pop('lastStatusChangeTimestamp', -1)

            counters = values.pop('interfaceCounters', None)
            _process_counters()

            interfaces[interface]['mode'] = values['forwardingModel']

            if interfaces[interface]['mode'] == u'routed':
                _process_routed_interface()
            if interfaces[interface]['mode'] == u'bridged':
                switchport_data = eval('self.device.show_interfaces_{}_switchport(format="text")'.format(interface))
                _process_switched_interface()

        return interfaces

    def get_bgp_neighbors(self):
        bgp_neighbors = dict()

        for vrf, vrf_data in self.device.show_ip_bgp_summary_vrf_all()['vrfs'].iteritems():
            bgp_neighbors[vrf] = dict()
            bgp_neighbors[vrf]['router_id'] = vrf_data['routerId']
            bgp_neighbors[vrf]['local_as'] = vrf_data['asn']
            bgp_neighbors[vrf]['peers'] = dict()

            for n, n_data in vrf_data['peers'].iteritems():
                bgp_neighbors[vrf]['peers'][n] = dict()

                if n_data['peerState'] == 'Established':
                    bgp_neighbors[vrf]['peers'][n]['status'] = 'up'
                else:
                    bgp_neighbors[vrf]['peers'][n]['status'] = 'down'

                bgp_neighbors[vrf]['peers'][n]['remote_as'] = n_data['asn']
                bgp_neighbors[vrf]['peers'][n]['uptime'] = n_data['upDownTime']

                raw_data = eval(
                    'self.device.show_ip_bgp_neighbors_vrf_{}(format="text", pipe="section {}")'.format(vrf, n)
                )['output']

                n_data_full =  colon_separated_string_to_dict(raw_data)
                sent, rcvd = n_data_full['IPv4 Unicast'].split()
                bgp_neighbors[vrf]['peers'][n]['rcvd_prefixes'] = int(rcvd)
                bgp_neighbors[vrf]['peers'][n]['sent_prefixes'] = int(sent)

        return bgp_neighbors

    def get_lldp_neighbors(self):
        lldp = dict()

        for n in self.device.show_lldp_neighbors()['lldpNeighbors']:
            if n['port'] not in lldp.keys():
                lldp[n['port']] = list()

            lldp[n['port']].append(
                {
                    'hostname': n['neighborDevice'],
                    'port': n['neighborPort'],
                    'ttl': n['ttl']
                }
            )

        return lldp