# This file is part of NDR.
#
# Copyright (C) 2017 - Secured By THEM
# Original Author: Michael Casadevall <michaelc@them.com>
#
# NDR is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# NDR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with NDR.  If not, see <http://www.gnu.org/licenses/>.

'''Holds the configuration information for NMAP'''

import tempfile
import subprocess
import os
import ipaddress

from enum import Enum

import ndr
import ndr_netcfg

class NmapConfig(object):
    '''Holds the configuration for NMAP scans'''
    def __init__(self, netcfg_file='/persistant/etc/ndr/network_config.yml'):
        self.scan_interfaces = []
        self.networks_to_scan = []
        self.blacklisted_hosts = []

        # Pull our interfaces from the NDR network configuration
        netcfg = ndr_netcfg.NetworkConfiguration(netcfg_file)
        interfaces = netcfg.get_all_managed_interfaces()

        # Loop through the interfaces we'll scan on
        for interface in interfaces:
            if 'lan' not in interface.name:
                continue # Interface we don't care about

            # Add this interface to networks we care about
            self.scan_interfaces.append(interface.name)

            # Append the networks we're configured for to the list
            for addr in interface.current_ip_addresses:
                self.networks_to_scan.append(
                    addr.ip_network()
                )

class NmapRunner(object):

    '''Runs NMAP scans on the network'''

    def __init__(self, config, nmap_config):
        self.config = config
        self.nmap_config = nmap_config

    def run_scan(self, scan_type, options, target):
        '''Does a IPv4 network scan'''

        xml_logfile = tempfile.mkstemp()

        # Invoke NMAP
        self.config.logger.debug("Scanning Target: %s", target)
        self.config.logger.debug("Options: %s", options)

        # Build the full nmap command line
        nmap_cmd = ["nmap"]
        nmap_cmd += (options.split(" "))

        # Build in XML output
        nmap_cmd += ["-oX", xml_logfile[1]]

        if target is not None:
            nmap_cmd += [target.compressed]

        self.config.logger.debug("NMap Command: %s", ' '.join(nmap_cmd))

        nmap_proc = subprocess.run(
            args=nmap_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

        if nmap_proc.returncode != 0:
            raise ndr.NmapFailure(nmap_proc.returncode,
                                  str(nmap_proc.stderr, 'utf-8'))

        xml_output = None
        with open(xml_logfile[1], 'r') as nmap_file:
            xml_output = nmap_file.read()
        os.remove(xml_logfile[1])

        # Build a scan object and return it
        nmap_scan = ndr.NmapScan(config=self.config)
        nmap_scan.parse_nmap_xml(xml_output)
        nmap_scan.scan_type = scan_type

        # Scan targets MUST be in CIDR form, so convert the target to a ipnetwork
        if target is not None:
            target_net = ipaddress.ip_network(target.compressed)
            nmap_scan.scan_target = target_net.compressed
        return nmap_scan

    def arp_host_discovery_scan(self, network):
        '''Performs a ARP surface scan on the network'''
        return self.run_scan(NmapScanTypes.ARP_DISCOVERY, "-sn -R -PR", network)

    def nd_host_discovery_scan(self, network):
        '''Performs a ND surface scan on the network'''
        return self.run_scan(NmapScanTypes.ND_DISCOVERY, "-6 -R -sn -PR", network)


    def v6_link_local_scan(self, interface):
        '''Performs a link-local scan'''
        return self.run_scan(NmapScanTypes.IPV6_LINK_LOCAL_DISCOVERY,
                             "-6 -R -sn -e %s --script=targets-ipv6-multicast-* --script-args=newtargets"
                             % (interface), None)

    def basic_host_scan(self, network):
        '''Does a basic port scan by hosts'''

        # Several bits of magic are required here
        # 1. If we're v6 address or range, we need -6
        # 2. If we're link-local, we need to specify the interface

        return self.run_scan(NmapScanTypes.PORT_SCAN, "-sS", network)

    def protocol_scan(self, network):
        '''Scans the network to determine what, if any IP protocols are supported'''
        nmap_flags = "-sO"
        if network.version == 6:
            nmap_flags = "-6" + nmap_flags

        return self.run_scan(NmapScanTypes.IP_PROTOCOL_DETECTION, nmap_flags, network)

    def indepth_host_scan(self, address, interface=None):
        '''Does a full discovery scan'''

        base_nmap_options = "-sS -A -T4"
        ipaddr = ipaddress.ip_address(address)

        options = base_nmap_options
        if ipaddr.version == 6:
            options = "-6 " + options
        if ipaddr.is_link_local:
            options = "-e " + interface + " " + options
        return self.run_scan(NmapScanTypes.SERVICE_DISCOVERY, options, address)

    def run_network_scans(self):
        '''Runs a scan of a network and builds an iterative map of the network'''

        def process_and_send_scan(scan, interface=None):
            '''Appends a list of IP addresses to scan further down the line'''
            hosts_in_scan = []
            for found_ip in scan.full_ip_list():
                logger.debug("Discovered host %s", found_ip)
                hosts_in_scan.append((found_ip, interface))

            logger.debug("Discovered %d hosts in total this scan", len(hosts_in_scan))
            scan.sign_report()
            scan.load_into_queue()

            return hosts_in_scan


        # During NMAP scanning, we will run this in multiple stages to determine what, if anything
        # if on the network, and then do additional scans beyond that point based on the data we
        # detect and determine. By default, we only scan the L2 segment we're on.

        logger = self.config.logger

        scan_interfaces = self.nmap_config.scan_interfaces
        networks_to_scan = self.nmap_config.networks_to_scan

        # First we need to generate a list of everything we can detect link local
        logger.info("== Running NMap Network Scan ==")
        logger.info("Phase 1: Link-Local Discovery")

        discovered_hosts = []

        for interface in scan_interfaces:
            logger.info("Scaning on %s", interface)

            logger.info("Performing IPv6 link-local discovery scan")
            ipv6_ll_scan = self.v6_link_local_scan(interface)

            discovered_hosts += process_and_send_scan(ipv6_ll_scan, interface=interface)


        logger.info("Phase 2: Network Discover")

        # Now we need to do host discovery on each network we have we have to scan
        for network in networks_to_scan:
            if network.version == 4:
                logger.info("Performing ARP host discovery on %s", network)
                arp_discovery = self.arp_host_discovery_scan(network)
                discovered_hosts += process_and_send_scan(arp_discovery)
            else:
                # IPv6
                logger.info("Performing ND host discovery on %s", network)
                nd_discovery = self.nd_host_discovery_scan(network)
                discovered_hosts += process_and_send_scan(nd_discovery)


        # Now we need to figure out what protocols each host supports
        logger.info("Phase 3: Protocol Discovery")
        for network in networks_to_scan:
            # FIXME: We should use protocol discovery here and refine our scans based on it, but
            # at the moment, that requires a fair bit of additional code to be written, so we'll
            # address it later

            # For now, we'll simply do a protocol scan so we can get an idea of what exists
            # out there in the wild
            logger.debug("Running protocol scan on %s", network)
            protocol_scan = self.protocol_scan(network)
            process_and_send_scan(protocol_scan)

        # Now begin in-depth scanning of things. If a host is blacklisted,
        # then it's noted and skipped at this point

        logger.info("Phase 4: Host Scanning")
        for host_tuple in discovered_hosts:
            logger.info("In-depth scanning %s", host_tuple[0])
            host_scan = self.indepth_host_scan(host_tuple[0], host_tuple[1])
            process_and_send_scan(host_scan)

class NmapScanTypes(Enum):

    '''Types of scans we do with NMAP'''
    ARP_DISCOVERY = "arp-discovery"
    IPV6_LINK_LOCAL_DISCOVERY = 'ipv6-link-local-discovery'
    IP_PROTOCOL_DETECTION = "ip-protocol-detection"
    PORT_SCAN = "port-scan"
    SERVICE_DISCOVERY = "service-discovery"
    ND_DISCOVERY = "nd-discovery"
