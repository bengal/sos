# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

from sos.report.plugins import Plugin, UbuntuPlugin


class Landscape(Plugin, UbuntuPlugin):

    short_desc = 'Ubuntu Landscape client'

    plugin_name = 'landscape'
    profiles = ('sysmgmt',)
    files = ('/etc/landscape/client.conf', '/etc/landscape/service.conf')
    packages = ('landscape-client', 'landscape-server')

    def setup(self):
        self.add_copy_spec([
            "/etc/default/landscape-client",
            "/etc/default/landscape-server",
            "/etc/landscape/client.conf",
            "/etc/landscape/service.conf",
            "/etc/landscape/service.conf.old",
            "/var/lib/landscape/landscape-oops/*/OOPS-*"
        ])

        if not self.get_option("all_logs"):
            self.add_copy_spec("/var/log/landscape/*.log")
        else:
            self.add_copy_spec("/var/log/landscape")

        self.add_cmd_output([
            "gpg --verify /etc/landscape/license.txt",
            "head -n 5 /etc/landscape/license.txt",
            "lsctl status"
        ])

    def postproc(self):
        self.do_file_sub(
            "/etc/landscape/client.conf",
            r"registration_password(.*)",
            r"registration_password[********]"
        )
        keys = [
            "password",
            "store_password",
            "secret-token",
            "oidc-client-secret",
            "oidc-client-id",
        ]
        self.do_path_regex_sub(
            "/etc/landscape/service.conf*",
            r"(%s) = (.*)" % "|".join(keys),
            r"\1 = [********]"
        )

# vim: set et ts=4 sw=4 :
