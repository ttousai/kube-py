#!/usr/bin/env python

'''
Kubernetes external inventory script
=================================

Generates inventory that Ansible can understand by calling kubectl.

NOTE: This script assumes Ansible is being executed where kubectl is already
installed and has a valid config at ~/.kube/config.

For more details, see: https://kubernetes.io/docs/tasks/tools/install-kubectl/

NOTE: By default, this script also assumes that the kubernetes nodes all have
labels that correspond to hostnames that are in your resolver search path.
Your resolver search path resides in /etc/hosts.
Optionally, if you would like to use the hosts public or private IP instead of 
it's label use the following setting in kube.ini:

    use_public_ip = true
    use_private_ip = true

When run against a specific host, this script returns the following variables:

    - annotations :dict
    - labels :dict
    - addresses :list
    - allocatable :dict
    - capacity :dict
    - taints :list
    - podCIDR (optional)
    - externalID
    - nodeinfo :dict
    - public_ip (The first public IP found)
    - private_ip (The first private IP found, or empty string if none)

Peter Sankauskas did most of the legwork here with his linode plugin; Dan Slimmon
adapted that for Linode and I adapted Dan Slimmon's work for kubernetes.
'''

# (c) 2018, Abubakr-Sadik Nii Nai Davis
# (c) 2013, Dan Slimmon
#
# This file is part of Ansible,
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

######################################################################

# Standard imports
import os
import subprocess
import re
import sys
import argparse
from time import time

try:
    import json
except ImportError:
    import simplejson as json

# Imports for ansible
import ConfigParser

class K8sInventory(object):
    def _empty_inventory(self):
        return {"_meta": {"hostvars": {}}}

    def __init__(self):
        """Main execution path."""
        # Inventory grouped by display group
        self.inventory = self._empty_inventory()

        # # Index of label to kubernetes nodes
        self.index = {}
        # # Local cache of Datacenter objects populated by populate_datacenter_cache()
        # self._datacenter_cache = None

        # Initialize empty node labels
        self.node_labels = {}

        # # Read settings and parse CLI arguments
        self.read_settings()
        self.parse_cli_args()

        # Cache
        if self.args.refresh_cache:
            self.do_api_calls_update_cache()
        elif not self.is_cache_valid():
            self.do_api_calls_update_cache()

        # Data to print
        if self.args.host:
            data_to_print = self.get_host_info()
        elif self.args.list:
            # Display list of nodes for inventory
            if len(self.inventory) == 1:
                data_to_print = self.get_inventory_from_cache()
            else:
                data_to_print = self.json_format_dict(self.inventory, True)

        print(data_to_print)

    def is_cache_valid(self):
        """Determines if the cache file has expired, or if it is still valid."""
        if os.path.isfile(self.cache_path_cache):
            mod_time = os.path.getmtime(self.cache_path_cache)
            current_time = time()
            if (mod_time + self.cache_max_age) > current_time:
                if os.path.isfile(self.cache_path_index):
                    return True
        return False

    def read_settings(self):
        """Reads the settings from the .ini file."""
        config = ConfigParser.SafeConfigParser()
        config.read(os.path.dirname(os.path.realpath(__file__)) + '/kube.ini')

        # Cache related
        cache_path = config.get('kubernetes', 'cache_path')
        self.cache_path_cache = cache_path + "/ansible-kube.cache"
        self.cache_path_index = cache_path + "/ansible-kube.index"
        self.cache_max_age = config.getint('kubernetes', 'cache_max_age')
        self.use_public_ip = config.getboolean('kubernetes', 'use_public_ip')
        self.use_private_ip = config.getboolean('kubernetes', 'use_private_ip')

    def parse_cli_args(self):
        """Command line argument processing"""
        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on Kubernetes')
        parser.add_argument('--list', action='store_true', default=True,
                            help='List nodes (default: True)')
        parser.add_argument('--host', action='store',
                            help='Get all the variables about a specific node')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                            help='Force refresh of cache by making API requests to Kubernetes (default: False - use cache files)')
        self.args = parser.parse_args()

    def do_api_calls_update_cache(self):
        """Do API calls, and save data in cache files."""
        self.get_nodes()
        self.write_to_cache(self.inventory, self.cache_path_cache)
        self.write_to_cache(self.index, self.cache_path_index)

    def get_nodes(self):
        """Calls kubectl to get the list of nodes."""
        cmd = [ "bash", "-c", "kubectl get nodes -ojson | jq '.items'" ]

        try:
            nodes = json.loads(subprocess.check_output(cmd))

            # load node labels hash
            for node in nodes:
                self.add_node(node)
                for label in node["metadata"]["labels"]:
                    self.node_labels[label] = 1
        except subprocess.CalledProcessError as e:
            sys.exit("Looks like kubectl is broken:\n %s" % e)

    def get_node(self, id):
        """Gets details about a specific node."""
        cmd = ['bash', '-c', 'kubectl get node %s -ojson' % id]
        try:
            node = json.loads(subprocess.check_output(cmd))
            return node
        except subprocess.CalledProcessError as err:
            sys.exit("kubectl error\n%s" % e)

    def get_node_label(self, node, label):
        if label in node["metadata"]["labels"]:
            return node["metadata"]["labels"][label]
        else:
            return ""

    def get_node_name(self, node):
        return node["metadata"]["name"]

    def add_node(self, node):
        """Adds an node to the inventory and index."""
        if self.use_public_ip:
            dest = self.get_node_public_ip(node)
        elif self.use_private_ip:
            dest = self.get_node_private_ip(node)
        else:
            dest = self.get_node_name(node)

        # Add to index
        self.index[dest] = self.get_node_name(node)

        self.push(self.inventory, 'all', dest)

        # Inventory: Group by all node labels
        for label in self.node_labels:
            self.push(self.inventory, self.get_node_label(node, label), dest)

        # Inventory: Group by display group
        # self.push(self.inventory, node.display_group, dest)

        # Add host info to hostvars
        self.inventory["_meta"]["hostvars"][dest] = self._get_host_info(node)

    def get_node_public_ip(self, node):
        """Returns a the public IP address of the node"""
        for addr in node['status']['addresses']:
            if addr['type'] == 'ExternalIP':
                return addr['address']

    def get_node_private_ip(self, node):
        """Returns a the private IP address of the node"""
        for addr in node['status']['addresses']:
            if addr['type'] == 'InternalIP':
                return addr['address']

    def get_host_info(self):
        """Get variables about a specific host."""

        if len(self.index) == 0:
            # Need to load index from cache
            self.load_index_from_cache()

        if self.args.host not in self.index:
            # try updating the cache
            self.do_api_calls_update_cache()
            if self.args.host not in self.index:
                # host might not exist anymore
                return self.json_format_dict({}, True)

        node_id = self.index[self.args.host]
        node = self.get_node(node_id)

        return self.json_format_dict(self._get_host_info(node), True)

    def _get_host_info(self, node):
        node_vars = {
            'annotations': node['metadata']['annotations'],
            'labels': node['metadata']['labels'],
            'addresses': node['status']['addresses'],
            'allocatable': node['status']['allocatable'],
            'capacity': node['status']['capacity'],
            # 'nodeinfo': node['status']['nodeinfo'],
        }

        if 'taints' in node['spec']:
            node_vars['taints'] = node['spec']['taints']

        if 'podCIDR' in node['spec']:
            node_vars['podCIDR'] = node['spec']['podCIDR']

        if 'providerID' in node['spec']:
            node_vars['providerID'] = node['spec']['providerID']

        if 'externalID' in node['spec']:
            node_vars['externalID'] = node['spec']['externalID']

        node_vars["public_ip"] = self.get_node_public_ip(node)
        node_vars["private_ip"] = self.get_node_private_ip(node)

        # Set the SSH host information, so these inventory items can be used if
        # their labels aren't FQDNs
        ssh_ip = ""
        if self.use_public_ip:
            ssh_ip = node_vars['public_ip']

        elif self.use_public_ip:
            ssh_ip = node_vars['private_ip']

        if ssh_ip == '':
            ssh_ip = node_vars['private_ip']

        node_vars['ansible_ssh_host'] = ssh_ip
        node_vars['ansible_host'] = ssh_ip

        return node_vars

    def push(self, my_dict, key, element):
        """Pushed an element onto an array that may not have been defined in the dict."""
        if key in my_dict:
            my_dict[key].append(element)
        else:
            my_dict[key] = [element]

    def get_inventory_from_cache(self):
        """Reads the inventory from the cache file and returns it as a JSON object."""
        cache = open(self.cache_path_cache, 'r')
        json_inventory = cache.read()
        return json_inventory

    def load_index_from_cache(self):
        """Reads the index from the cache file and sets self.index."""
        cache = open(self.cache_path_index, 'r')
        json_index = cache.read()
        self.index = json.loads(json_index)

    def write_to_cache(self, data, filename):
        """Writes data in JSON format to a file."""
        json_data = self.json_format_dict(data, True)
        cache = open(filename, 'w')
        cache.write(json_data)
        cache.close()

    def to_safe(self, word):
        """Escapes any characters that would be invalid in an ansible group name."""
        return re.sub("[^A-Za-z0-9\-]", "_", word)

    def json_format_dict(self, data, pretty=False):
        """Converts a dict to a JSON object and dumps it as a formatted string."""
        if pretty:
            return json.dumps(data, sort_keys=True, indent=2)
        else:
            return json.dumps(data)

K8sInventory()
