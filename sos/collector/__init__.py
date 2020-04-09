# Copyright Red Hat 2020, Jake Hunsaker <jhunsake@redhat.com>

# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

import fnmatch
import inspect
import json
import logging
import os
import random
import re
import string
import tarfile
import tempfile
import socket
import shutil
import subprocess
import sys

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from getpass import getpass
from pipes import quote
from textwrap import fill
from sos.collector.sosnode import SosNode
from sos.collector.exceptions import ControlPersistUnsupportedException
from sos.component import SoSComponent
from sos import __version__

COLLECTOR_LIB_DIR = '/var/lib/sos-collector'


class SoSCollector(SoSComponent):
    """Collect an sos report from multiple nodes simultaneously
    """

    arg_defaults = {
        'alloptions': False,
        'all_logs': False,
        'become_root': False,
        'batch': False,
        'case_id': False,
        'cluster_type': None,
        'cluster_options': [],
        'chroot': 'auto',
        'enable_plugins': [],
        'group': None,
        'save_group': '',
        'image': '',
        'ssh_key': '',
        'insecure_sudo': False,
        'plugin_options': [],
        'list_options': False,
        'label': '',
        'log_size': 0,
        'skip_plugins': [],
        'nodes': [],
        'no_pkg_check': False,
        'no_local': False,
        'master': '',
        'only_plugins': [],
        'ssh_port': 22,
        'password': False,
        'password_per_node': False,
        'preset': '',
        'sos_opt_line': '',
        'ssh_user': 'root',
        'timeout': 600,
        'verify': False,
        'compression': 'auto'
    }

    def __init__(self, parser, parsed_args, cmdline_args):
        super(SoSCollector, self).__init__(parser, parsed_args, cmdline_args)
        os.umask(0o77)
        self.client_list = []
        self.node_list = []
        self.master = False
        self.retrieved = 0
        self.need_local_sudo = False
        # get the local hostname and addresses to filter from results later
        self.hostname = socket.gethostname()
        try:
            self.ip_addrs = list(set([
                i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)
            ]))
        except Exception:
            # this is almost always a DNS issue with reverse resolution
            # set a safe fallback and log the issue
            self.log_error(
                "Could not get a list of IP addresses from this hostnamne. "
                "This may indicate a DNS issue in your environment"
            )
            self.ip_addrs = ['127.0.0.1']
        if not self.opts.list_options:
            try:
                self.parse_node_strings()
                self.parse_cluster_options()
                self._parse_options()
                self._check_for_control_persist()
                self.clusters = self.load_clusters()
                self.log_debug('Executing %s' % ' '.join(s for s in sys.argv))
                self.log_debug("Found cluster profiles: %s"
                               % self.clusters.keys())
                self.log_debug("Found supported host types: %s"
                               % self.host_types.keys())
                self.verify_cluster_options()
                self.prep()
            except KeyboardInterrupt:
                self._exit('Exiting on user cancel', 130)
            except Exception:
                raise        

    def load_clusters(self):
        """Loads all cluster types supported by the local installation for
        future comparison and/or use
        """
        import sos.collector.clusters
        package = sos.collector.clusters
        supported_clusters = {}
        clusters = self._load_modules(package, 'clusters')
        for cluster in clusters:
            supported_clusters[cluster[0]] = cluster[1](self.commons)
        return supported_clusters

    def load_host_types(self):
        """Loads all host types supported by the local installation"""
        import sos.collector.hosts
        package = sos.collector.hosts
        supported_hosts = {}
        hosts = self._load_modules(package, 'hosts')
        for host in hosts:
            supported_hosts[host[0]] = host[1]
        return supported_hosts

    def _load_modules(self, package, submod):
        """Helper to import cluster and host types"""
        modules = []
        for path in package.__path__:
            if os.path.isdir(path):
                modules.extend(self._find_modules_in_path(path, submod))
        return modules

    def _find_modules_in_path(self, path, modulename):
        """Given a path and a module name, find everything that can be imported
        and then import it

            path - the filesystem path of the package
            modulename - the name of the module in the package

        E.G. a path of 'clusters', and a modulename of 'ovirt' equates to
        importing sos.collector.clusters.ovirt
        """
        modules = []
        if os.path.exists(path):
            for pyfile in sorted(os.listdir(path)):
                if not pyfile.endswith('.py'):
                    continue
                if '__' in pyfile:
                    continue
                fname, ext = os.path.splitext(pyfile)
                modname = 'sos.collector.%s.%s' % (modulename, fname)
                modules.extend(self._import_modules(modname))
        return modules

    def _import_modules(self, modname):
        """Import and return all found classes in a module"""
        mod_short_name = modname.split('.')[2]
        module = __import__(modname, globals(), locals(), [mod_short_name])
        modules = inspect.getmembers(module, inspect.isclass)
        for mod in modules:
            if mod[0] in ('SosHost', 'Cluster'):
                modules.remove(mod)
        return modules

    def parse_node_strings(self):
        """Parses the given --nodes option(s) to properly format the regex
        list that we use. We cannot blindly split on ',' chars since it is a
        valid regex character, so we need to scan along the given strings and
        check at each comma if we should use the preceeding string by itself
        or not, based on if there is a valid regex at that index.
        """
        if not self.opts.nodes:
            return
        nodes = []
        if not isinstance(self.opts.nodes, list):
            self.opts.nodes = [self.opts.nodes]
        for node in self.opts.nodes:
            idxs = [i for i, m in enumerate(node) if m == ',']
            idxs.append(len(node))
            start = 0
            pos = 0
            for idx in idxs:
                try:
                    pos = idx
                    reg = node[start:idx]
                    re.compile(re.escape(reg))
                    # make sure we aren't splitting a regex value
                    if '[' in reg and ']' not in reg:
                        continue
                    nodes.append(reg.lstrip(','))
                    start = idx
                except re.error:
                    continue
            if pos != len(node):
                nodes.append(node[pos+1:])
        self.opts.nodes = nodes

    @classmethod
    def add_parser_options(cls, parser):
        parser.add_argument('-a', '--alloptions', action='store_true',
                            help='Enable all sos options')
        parser.add_argument('--all-logs', action='store_true',
                            help='Collect logs regardless of size')
        parser.add_argument('-b', '--become', action='store_true',
                            dest='become_root',
                            help='Become root on the remote nodes')
        parser.add_argument('--batch', action='store_true',
                            help='Do not prompt interactively (except passwords)')
        parser.add_argument('--case-id', help='Specify case number')
        parser.add_argument('--cluster-type',
                            help='Specify a type of cluster profile')
        parser.add_argument('-c', '--cluster-option', dest='cluster_options',
                            action='append',
                            help=('Specify a cluster options used by a profile'
                                  ' and takes the form of cluster.option=value'
                                  )
                            )
        parser.add_argument('--chroot', default='',
                            choices=['auto', 'always', 'never'],
                            help="chroot executed commands to SYSROOT")
        parser.add_argument('-e', '--enable-plugins', action="append",
                            help='Enable specific plugins for sosreport')
        parser.add_argument('--group', default=None,
                            help='Use a predefined group JSON file')
        parser.add_argument('--save-group', default='',
                            help='Save the resulting node list to a group')
        parser.add_argument('--image',
                            help=('Specify the container image to use for '
                                  'containerized hosts. Defaults to the '
                                  'rhel7/support-tools image'))
        parser.add_argument('-i', '--ssh-key', help='Specify an ssh key to use')
        parser.add_argument('--insecure-sudo', action='store_true',
                            help='Use when passwordless sudo is configured')
        parser.add_argument('-k', '--plugin-options', action="append",
                            help='Plugin option as plugname.option=value')
        parser.add_argument('-l', '--list-options', action="store_true",
                            help='List options available for profiles')
        parser.add_argument('--label', help='Assign a label to the archives')
        parser.add_argument('--log-size', default=0, type=int,
                            help='Limit the size of individual logs (in MiB)')
        parser.add_argument('-n', '--skip-plugins', action="append",
                            help='Skip these plugins')
        parser.add_argument('--nodes', action="append",
                            help='Provide a comma delimited list of nodes, or a '
                                 'regex to match against')
        parser.add_argument('--no-pkg-check', action='store_true',
                            help=('Do not run package checks. Use this '
                                  'with --cluster-type if there are rpm '
                                  'or apt issues on node'
                                  )
                            )
        parser.add_argument('--no-local', action='store_true',
                            help='Do not collect a sosreport from localhost')
        parser.add_argument('--master', help='Specify a remote master node')
        parser.add_argument('-o', '--only-plugins', action="append",
                            help='Run these plugins only')
        parser.add_argument('-p', '--ssh-port', type=int,
                            help='Specify SSH port for all nodes')
        parser.add_argument('--password', action='store_true', default=False,
                            help='Prompt for user password for nodes')
        parser.add_argument('--password-per-node', action='store_true',
                            default=False,
                            help='Prompt for password separately for each node')
        parser.add_argument('--preset', default='', required=False,
                            help='Specify a sos preset to use')
        parser.add_argument('--sos-cmd', dest='sos_opt_line',
                            help=("Manually specify the commandline options for "
                                  "sosreport on remote nodes")
                            )
        parser.add_argument('--ssh-user',
                            help='Specify an SSH user. Default root')
        parser.add_argument('--timeout', type=int, required=False,
                            help='Timeout for sosreport on each node. Default 300.'
                            )
        parser.add_argument('--verify', action="store_true",
                            help="perform data verification during collection")
        parser.add_argument('-z', '--compression-type', dest="compression",
                            choices=['auto', 'gzip', 'bzip2', 'xz'],
                            help="compression technology to use")

    def _check_for_control_persist(self):
        '''Checks to see if the local system supported SSH ControlPersist.

        ControlPersist allows OpenSSH to keep a single open connection to a
        remote host rather than building a new session each time. This is the
        same feature that Ansible uses in place of paramiko, which we have a
        need to drop in sos-collector.

        This check relies on feedback from the ssh binary. The command being
        run should always generate stderr output, but depending on what that
        output reads we can determine if ControlPersist is supported or not.

        For our purposes, a host that does not support ControlPersist is not
        able to run sos-collector.

        Returns
            True if ControlPersist is supported, else raise Exception.
        '''
        ssh_cmd = ['ssh', '-o', 'ControlPersist']
        cmd = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        out, err = cmd.communicate()
        err = err.decode('utf-8')
        if 'Bad configuration option' in err or 'Usage:' in err:
            raise ControlPersistUnsupportedException
        return True

    def _exit(self, msg, error=1):
        '''Used to safely terminate if sos-collector encounters an error'''
        self.log_error(msg)
        try:
            self.close_all_connections()
        except Exception:
            pass
        sys.exit(error)

    def _parse_options(self):
        """From commandline options, defaults, etc... build a set of commons
        to hand to other collector mechanisms
        """
        self.host_types = self.load_host_types()
        self.commons = {
            'need_sudo': True if self.opts.ssh_user != 'root' else False,
            'host_types': self.host_types,
            'opts': self.opts,
            'tmpdir': self.tmpdir,
            'hostlen': len(self.opts.master) or len(self.hostname)
        }


    def parse_cluster_options(self):
        opts = []
        if not isinstance(self.opts.cluster_options, list):
            self.opts.cluster_options = [self.opts.cluster_options]
        if self.opts.cluster_options:
            for option in self.opts.cluster_options:
                cluster = option.split('.')[0]
                name = option.split('.')[1].split('=')[0]
                try:
                    # there are no instances currently where any cluster option
                    # should contain a legitimate space.
                    value = option.split('=')[1].split()[0]
                except IndexError:
                    # conversion to boolean is handled during validation
                    value = 'True'

                opts.append(
                    ClusterOption(name, value, value.__class__, cluster)
                )
        self.opts.cluster_options = opts


    def verify_cluster_options(self):
        """Verify that requested cluster options exist"""
        if self.opts.cluster_options:
            for opt in self.opts.cluster_options:
                match = False
                for clust in self.clusters:
                    for option in self.clusters[clust].options:
                        if opt.name == option.name:
                            match = True
                            break
            if not match:
                self._exit('Unknown cluster option provided: %s.%s'
                           % (opt.cluster, opt.name))

    def _validate_option(self, default, cli):
        '''Checks to make sure that the option given on the CLI is valid.
        Valid in this sense means that the type of value given matches what a
        cluster profile expects (str for str, bool for bool, etc).

        For bool options, this will also convert the string equivalent to an
        actual boolean value
        '''
        if not default.opt_type == bool:
            if not default.opt_type == cli.opt_type:
                msg = "Invalid option type for %s. Expected %s got %s"
                self._exit(msg % (cli.name, default.opt_type, cli.opt_type))
            return cli.value
        else:
            val = cli.value.lower()
            if val not in ['true', 'on', 'false', 'off']:
                msg = ("Invalid value for %s. Accepted values are: 'true', "
                       "'false', 'on', 'off'")
                self._exit(msg % cli.name)
            else:
                if val in ['true', 'on']:
                    return True
                else:
                    return False

    def log_info(self, msg):
        '''Log info messages to both console and log file'''
        self.soslog.info(msg)

    def log_warn(self, msg):
        '''Log warn messages to both console and log file'''
        self.soslog.warn(msg)

    def log_error(self, msg):
        '''Log error messages to both console and log file'''
        self.soslog.error(msg)

    def log_debug(self, msg):
        '''Log debug message to both console and log file'''
        caller = inspect.stack()[1][3]
        msg = '[sos_collector:%s] %s' % (caller, msg)
        self.soslog.debug(msg)

    def list_options(self):
        '''Display options for available clusters'''

        sys.stdout.write('\nThe following clusters are supported by this '
                         'installation\n')
        sys.stdout.write('Use the short name with --cluster-type or cluster '
                         'options (-c)\n\n')
        for cluster in sorted(self.clusters):
            sys.stdout.write(" {:<15} {:30}\n".format(
                                cluster,
                                self.clusters[cluster].cluster_name))

        _opts = {}
        for _cluster in self.clusters:
            for opt in self.clusters[_cluster].options:
                if opt.name not in _opts.keys():
                    _opts[opt.name] = opt
                else:
                    for clust in opt.cluster:
                        if clust not in _opts[opt.name].cluster:
                            _opts[opt.name].cluster.append(clust)

        sys.stdout.write('\nThe following cluster options are available:\n\n')
        sys.stdout.write(' {:25} {:15} {:<10} {:10} {:<}\n'.format(
            'Cluster',
            'Option Name',
            'Type',
            'Default',
            'Description'
        ))

        for _opt in sorted(_opts, key=lambda x: _opts[x].cluster):
            opt = _opts[_opt]
            optln = ' {:25} {:15} {:<10} {:<10} {:<10}\n'.format(
                ', '.join(c for c in sorted(opt.cluster)),
                opt.name,
                opt.opt_type.__name__,
                str(opt.value),
                opt.description)
            sys.stdout.write(optln)
        sys.stdout.write('\nOptions take the form of cluster.name=value'
                         '\nE.G. "ovirt.no-database=True" or '
                         '"pacemaker.offline=False"\n')

    def delete_tmp_dir(self):
        '''Removes the temp directory and all collected sosreports'''
        shutil.rmtree(self.tmpdir)

    def _get_archive_name(self):
        '''Generates a name for the tarball archive'''
        nstr = 'sos-collector'
        if self.opts.label:
            nstr += '-%s' % self.opts.label
        if self.opts.case_id:
            nstr += '-%s' % self.opts.case_id
        dt = datetime.strftime(datetime.now(), '%Y-%m-%d')

        try:
            string.lowercase = string.ascii_lowercase
        except NameError:
            pass

        rand = ''.join(random.choice(string.lowercase) for x in range(5))
        return '%s-%s-%s' % (nstr, dt, rand)

    def _get_archive_path(self):
        '''Returns the path, including filename, of the tarball we build
        that contains the collected sosreports
        '''
        self.arc_name = self._get_archive_name()
        compr = 'gz'
        return self.tmpdir + self.arc_name + '.tar.' + compr

    def _fmt_msg(self, msg):
        width = 80
        _fmt = ''
        for line in msg.splitlines():
            _fmt = _fmt + fill(line, width, replace_whitespace=False) + '\n'
        return _fmt

    def _load_group_config(self):
        '''
        Attempts to load the host group specified on the command line.
        Host groups are defined via JSON files, typically saved under
        /var/lib/sos-collector/, although users can specify a full filepath
        on the commandline to point to one existing anywhere on the system

        Host groups define a list of nodes and/or regexes and optionally the
        master and cluster-type options.
        '''
        if os.path.exists(self.opts.group):
            fname = self.opts.group
        elif os.path.exists(
                os.path.join(COLLECTOR_LIB_DIR, self.opts.group)
             ):
            fname = os.path.join(COLLECTOR_LIB_DIR, self.opts.group)
        else:
            raise OSError('Group not found')

        self.log_debug("Loading host group %s" % fname)

        with open(fname, 'r') as hf:
            _group = json.load(hf)
            for key in ['master', 'cluster_type']:
                if _group[key]:
                    self.log_debug("Setting option '%s' to '%s' per host group"
                                   % (key, _group[key]))
                    setattr(self.opts, key, _group[key])
            if _group['nodes']:
                self.log_debug("Adding %s to node list" % _group['nodes'])
                self.opts.nodes.extend(_group['nodes'])

    def write_host_group(self):
        '''
        Saves the results of this run of sos-collector to a host group file
        on the system so it can be used later on.

        The host group will save the options master, cluster_type, and nodes
        as determined by sos-collector prior to execution of sosreports.
        '''
        cfg = {
            'name': self.opts.save_group,
            'master': self.opts.master,
            'cluster_type': self.cluster_type,
            'nodes': [n for n in self.node_list]
        }
        if not os.path.isdir(COLLECTOR_LIB_DIR):
            raise OSError("%s no such directory" % COLLECTOR_LIB_DIR)
        fname = COLLECTOR_LIB_DIR + '/' + cfg['name']
        with open(fname, 'w') as hf:
            json.dump(cfg, hf)
        os.chmod(fname, 0o644)
        return fname

    def prep(self):
        disclaimer = ("""\
This utility is used to collect sosreports from multiple \
nodes simultaneously. It uses OpenSSH's ControlPersist feature \
to connect to nodes and run commands remotely. If your system \
installation of OpenSSH is older than 5.6, please upgrade.

An archive of sosreport tarballs collected from the nodes will be \
generated in %s and may be provided to an appropriate support representative.

The generated archive may contain data considered sensitive \
and its content should be reviewed by the originating \
organization before being passed to any third party.

No configuration changes will be made to the system running \
this utility or remote systems that it connects to.
""")
        self.ui_log.info("\nsos-collector (version %s)\n" % __version__)
        intro_msg = self._fmt_msg(disclaimer % self.opts.tmp_dir)
        self.ui_log.info(intro_msg)
        prompt = "\nPress ENTER to continue, or CTRL-C to quit\n"
        if not self.opts.batch:
            input(prompt)

        if (not self.opts.password and not
                self.opts.password_per_node):
            self.log_debug('password not specified, assuming SSH keys')
            msg = ('sos-collector ASSUMES that SSH keys are installed on all '
                   'nodes unless the --password option is provided.\n')
            self.ui_log.info(self._fmt_msg(msg))

        if self.opts.password or self.opts.password_per_node:
            self.log_debug('password specified, not using SSH keys')
            msg = ('Provide the SSH password for user %s: '
                   % self.opts.ssh_user)
            self.opts.password = getpass(prompt=msg)

        if self.commons['need_sudo'] and not self.opts.insecure_sudo:
            if not self.opts.password:
                self.log_debug('non-root user specified, will request '
                               'sudo password')
                msg = ('A non-root user has been provided. Provide sudo '
                       'password for %s on remote nodes: '
                       % self.opts.ssh_user)
                self.opts.sudo_pw = getpass(prompt=msg)
            else:
                if not self.opts.insecure_sudo:
                    self.opts.sudo_pw = self.opts.password

        if self.opts.become_root:
            if not self.opts.ssh_user == 'root':
                self.log_debug('non-root user asking to become root remotely')
                msg = ('User %s will attempt to become root. '
                       'Provide root password: ' % self.opts.ssh_user)
                self.opts.root_password = getpass(prompt=msg)
                self.commons['need_sudo'] = False
            else:
                self.log_info('Option to become root but ssh user is root.'
                              ' Ignoring request to change user on node')
                self.opts.become_root = False

        if self.opts.group:
            try:
                self._load_group_config()
            except Exception as err:
                self.log_error("Could not load specified group %s: %s"
                               % (self.opts.group, err))

        if self.opts.master:
            self.connect_to_master()
            self.opts.no_local = True
        else:
            try:
                self.master = SosNode('localhost', self.commons)
            except Exception as err:
                print(err)
                self.log_debug("Unable to determine local installation: %s" %
                               err)
                self._exit('Unable to determine local installation. Use the '
                           '--no-local option if localhost should not be '
                           'included.\nAborting...\n', 1)

        if self.opts.cluster_type:
            if self.opts.cluster_type == 'none':
                self.cluster = self.clusters['jbon']
            else:
                self.cluster = self.clusters[self.opts.cluster_type
                                        ]
            self.cluster.master = self.master

        else:
            self.determine_cluster()
        if self.cluster is None and not self.opts.nodes:
            msg = ('Cluster type could not be determined and no nodes provided'
                   '\nAborting...')
            self._exit(msg, 1)
        if self.cluster:
            self.master.cluster = self.cluster
            self.cluster.setup()
            self.cluster.modify_sos_cmd()
        self.get_nodes()
        if self.opts.save_group:
            gname = self.opts.save_group
            try:
                fname = self.write_host_group()
                self.log_info("Wrote group '%s' to %s" % (gname, fname))
            except Exception as err:
                self.log_error("Could not save group %s: %s" % (gname, err))
        self.intro()
        self.configure_sos_cmd()

    def intro(self):
        '''Prints initial messages and collects user and case if not
        provided already.
        '''
        self.ui_log.info('')

        if not self.node_list and not self.master.connected:
            self._exit('No nodes were detected, or nodes do not have sos '
                       'installed.\nAborting...')

        self.ui_log.info('The following is a list of nodes to collect from:')
        if self.master.connected:
            self.ui_log.info('\t%-*s' % (self.commons['hostlen'], self.opts.master))

        for node in sorted(self.node_list):
            self.ui_log.info("\t%-*s" % (self.commons['hostlen'], node))

        self.ui_log.info('')

        if not self.opts.case_id and not self.opts.batch:
            msg = 'Please enter the case id you are collecting reports for: '
            self.opts.case_id = input(msg)

    def configure_sos_cmd(self):
        '''Configures the sosreport command that is run on the nodes'''
        self.sos_cmd = 'sosreport --batch'
        if self.opts.sos_opt_line:
            filt = ['&', '|', '>', '<', ';']
            if any(f in self.opts.sos_opt_line for f in filt):
                self.log_warn('Possible shell script found in provided sos '
                              'command. Ignoring --sos-opt-line entirely.')
                self.opts.sos_opt_line = None
            else:
                self.sos_cmd = '%s %s' % (
                    self.sos_cmd, quote(self.opts.sos_opt_line))
                self.log_debug("User specified manual sosreport command. "
                               "Command set to %s" % self.sos_cmd)
                return True
        if self.opts.case_id:
            self.sos_cmd += ' --case-id=%s' % (
                quote(self.opts.case_id))
        if self.opts.alloptions:
            self.sos_cmd += ' --alloptions'
        if self.opts.all_logs:
            self.sos_cmd += ' --all-logs'
        if self.opts.verify:
            self.sos_cmd += ' --verify'
        if self.opts.log_size:
            self.sos_cmd += (' --log-size=%s' % quote(self.opts.log_size))
        if self.opts.sysroot:
            self.sos_cmd += ' -s %s' % quote(self.opts.sysroot)
        if self.opts.chroot:
            self.sos_cmd += ' -c %s' % quote(self.opts.chroot)
        if self.opts.compression:
            self.sos_cmd += ' -z %s' % (quote(self.opts.compression))
        self.log_debug('Initial sos cmd set to %s' % self.sos_cmd)

    def connect_to_master(self):
        '''If run with --master, we will run cluster checks again that
        instead of the localhost.
        '''
        try:
            self.master = SosNode(self.opts.master, self.commons)
            self.ui_log.info('Connected to %s, determining cluster type...'
                             % self.opts.master)
        except Exception as e:
            self.log_debug('Failed to connect to master: %s' % e)
            self._exit('Could not connect to master node. Aborting...', 1)

    def determine_cluster(self):
        '''This sets the cluster type and loads that cluster's cluster.

        If no cluster type is matched and no list of nodes is provided by
        the user, then we abort.

        If a list of nodes is given, this is not run, however the cluster
        can still be run if the user sets a --cluster-type manually
        '''
        self.cluster = None
        checks = list(self.clusters.values())
        for cluster in self.clusters.values():
            checks.remove(cluster)
            cluster.master = self.master
            if cluster.check_enabled():
                cname = cluster.__class__.__name__
                self.log_debug("Installation matches %s, checking for layered "
                               "profiles" % cname)
                for remaining in checks:
                    if issubclass(remaining.__class__, cluster.__class__):
                        rname = remaining.__class__.__name__
                        self.log_debug("Layered profile %s found. "
                                       "Checking installation"
                                       % rname)
                        remaining.master = self.master
                        if remaining.check_enabled():
                            self.log_debug("Installation matches both layered "
                                           "profile %s and base profile %s, "
                                           "setting cluster type to layered "
                                           "profile" % (rname, cname))
                            cluster = remaining
                            break
                self.cluster = cluster
                self.cluster_type = cluster.name()
                self.ui_log.info(
                    'Cluster type set to %s' % self.cluster_type)
                break

    def get_nodes_from_cluster(self):
        '''Collects the list of nodes from the determined cluster cluster'''
        if self.cluster_type:
            nodes = self.cluster._get_nodes()
            self.log_debug('Node list: %s' % nodes)
            return nodes

    def reduce_node_list(self):
        '''Reduce duplicate entries of the localhost and/or master node
        if applicable'''
        if (self.hostname in self.node_list and self.opts.no_local):
            self.node_list.remove(self.hostname)
        for i in self.ip_addrs:
            if i in self.node_list:
                self.node_list.remove(i)
        # remove the master node from the list, since we already have
        # an open session to it.
        if self.master:
            for n in self.node_list:
                if n == self.master.hostname or n == self.opts.master:
                    self.node_list.remove(n)
        self.node_list = list(set(n for n in self.node_list if n))
        self.log_debug('Node list reduced to %s' % self.node_list)

    def compare_node_to_regex(self, node):
        '''Compares a discovered node name to a provided list of nodes from
        the user. If there is not a match, the node is removed from the list'''
        for regex in self.opts.nodes:
            try:
                regex = fnmatch.translate(regex)
                if re.match(regex, node):
                    return True
            except re.error as err:
                msg = 'Error comparing %s to provided node regex %s: %s'
                self.log_debug(msg % (node, regex, err))
        return False

    def get_nodes(self):
        ''' Sets the list of nodes to collect sosreports from '''
        if not self.master and not self.cluster:
            msg = ('Could not determine a cluster type and no list of '
                   'nodes or master node was provided.\nAborting...'
                   )
            self._exit(msg)

        try:
            nodes = self.get_nodes_from_cluster()
            if self.opts.nodes:
                for node in nodes:
                    if self.compare_node_to_regex(node):
                        self.node_list.append(node)
            else:
                self.node_list = nodes
        except Exception as e:
            self.log_debug("Error parsing node list: %s" % e)
            self.log_debug('Setting node list to --nodes option')
            self.node_list = self.opts.nodes
            for node in self.node_list:
                if any(i in node for i in ('*', '\\', '?', '(', ')', '/')):
                    self.node_list.remove(node)

        # force add any non-regex node strings from nodes option
        if self.opts.nodes:
            for node in self.opts.nodes:
                if any(i in node for i in '*\\?()/[]'):
                    continue
                if node not in self.node_list:
                    self.log_debug("Force adding %s to node list" % node)
                    self.node_list.append(node)

        if not self.master:
            host = self.hostname.split('.')[0]
            # trust the local hostname before the node report from cluster
            for node in self.node_list:
                if host == node.split('.')[0]:
                    self.node_list.remove(node)
            self.node_list.append(self.hostname)
        self.reduce_node_list()
        try:
            self.commons['hostlen'] = len(max(self.node_list, key=len))
        except (TypeError, ValueError):
            self.commons['hostlen'] = len(self.opts.master)

    def _connect_to_node(self, node):
        '''Try to connect to the node, and if we can add to the client list to
        run sosreport on

        Positional arguments
            node - a tuple specifying (address, password). If no password, set
                   to None
        '''
        try:
            client = SosNode(node[0], self.commons, password=node[1])
            if client.connected:
                self.client_list.append(client)
            else:
                client.close_ssh_session()
        except Exception:
            pass

    def collect(self):
        ''' For each node, start a collection thread and then tar all
        collected sosreports '''
        if self.master.connected:
            self.client_list.append(self.master)

        self.ui_log.info("\nConnecting to nodes...")
        filters = [self.master.address, self.master.hostname]
        nodes = [(n, None) for n in self.node_list if n not in filters]

        if self.opts.password_per_node:
            _nodes = []
            for node in nodes:
                msg = ("Please enter the password for %s@%s: "
                       % (self.opts.ssh_user, node[0]))
                node_pwd = getpass(msg)
                _nodes.append((node[0], node_pwd))
            nodes = _nodes

        try:
            pool = ThreadPoolExecutor(self.opts.threads)
            pool.map(self._connect_to_node, nodes, chunksize=1)
            pool.shutdown(wait=True)

            self.report_num = len(self.client_list)
            if self.opts.no_local and self.master.address == 'localhost':
                self.report_num -= 1

            self.ui_log.info("\nBeginning collection of sosreports from %s "
                              "nodes, collecting a maximum of %s "
                              "concurrently\n"
                              % (self.report_num, self.opts.threads)
                              )

            pool = ThreadPoolExecutor(self.opts.threads)
            pool.map(self._collect, self.client_list, chunksize=1)
            pool.shutdown(wait=True)
        except KeyboardInterrupt:
            self.log_error('Exiting on user cancel\n')
            os._exit(130)
        except Exception as err:
            self.log_error('Could not connect to nodes: %s' % err)
            os._exit(1)

        if hasattr(self.cluster, 'run_extra_cmd'):
            self.ui_log.info('Collecting additional data from master node...')
            files = self.cluster._run_extra_cmd()
            if files:
                self.master.collect_extra_cmd(files)
        msg = '\nSuccessfully captured %s of %s sosreports'
        self.log_info(msg % (self.retrieved, self.report_num))
        self.close_all_connections()
        if self.retrieved > 0:
            self.create_cluster_archive()
        else:
            msg = 'No sosreports were collected, nothing to archive...'
            self._exit(msg, 1)

    def _collect(self, client):
        '''Runs sosreport on each node'''
        try:
            if not client.local:
                client.sosreport()
            else:
                if not self.opts.no_local:
                    client.sosreport()
            if client.retrieved:
                self.retrieved += 1
        except Exception as err:
            self.log_error("Error running sosreport: %s" % err)

    def close_all_connections(self):
        '''Close all ssh sessions for nodes'''
        for client in self.client_list:
            self.log_debug('Closing SSH connection to %s' % client.address)
            client.close_ssh_session()

    def create_cluster_archive(self):
        '''Calls for creation of tar archive then cleans up the temporary
        files created by sos-collector'''
        self.log_info('Creating archive of sosreports...')
        self.create_sos_archive()
        if self.archive:
            self.soslog.info('Archive created as %s' % self.archive)
            self.cleanup()
            self.ui_log.info('\nThe following archive has been created. '
                              'Please provide it to your support team.')
            self.ui_log.info('    %s' % self.archive)

    def create_sos_archive(self):
        '''Creates a tar archive containing all collected sosreports'''
        try:
            self.archive = self._get_archive_path()
            with tarfile.open(self.archive, "w:gz") as tar:
                for host in self.client_list:
                    for fname in host.file_list:
                        try:
                            if '.md5' in fname:
                                arc_name = (self.arc_name + '/md5/' +
                                            fname.split('/')[-1])
                            else:
                                arc_name = (self.arc_name + '/' +
                                            fname.split('/')[-1])
                            tar.add(
                                os.path.join(self.tmpdir, fname),
                                arcname=arc_name
                            )
                        except Exception as err:
                            self.log_error("Could not add %s to archive: %s"
                                           % (arc_name, err))
                tar.add(
                    self.sos_log_file,
                    arcname=self.arc_name + '/logs/sos-collector.log'
                )
                tar.add(
                    self.sos_ui_log_file,
                    arcname=self.arc_name + '/logs/ui.log'
                )
                tar.close()
        except Exception as e:
            msg = 'Could not create archive: %s' % e
            self._exit(msg, 2)

    def cleanup(self):
        ''' Removes the tmp dir and all sosarchives therein.

            If tmp dir was supplied by user, only the sos archives within
            that dir are removed.
        '''
        if self.tmpdir_created:
            self.delete_tmp_dir()
        else:
            for f in os.listdir(self.tmpdir):
                if re.search('sosreport-*tar*', f):
                    os.remove(os.path.join(self.tmpdir, f))
