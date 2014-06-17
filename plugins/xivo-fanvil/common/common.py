# -*- coding: utf-8 -*-

# Copyright (C) 2010-2014 Avencall
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>

import errno
import logging
import re
import os.path
from operator import itemgetter
from provd import tzinform
from provd import synchronize
from provd.devices.config import RawConfigError
from provd.plugins import StandardPlugin, FetchfwPluginHelper, \
    TemplatePluginHelper
from provd.devices.pgasso import IMPROBABLE_SUPPORT, COMPLETE_SUPPORT, \
    FULL_SUPPORT, BasePgAssociator, UNKNOWN_SUPPORT
from provd.servers.http import HTTPNoListingFileService
from provd.util import norm_mac, format_mac
from twisted.internet import defer, threads

logger = logging.getLogger('plugin.xivo-fanvil')

TZ_NAME = { 'Europe/Paris': 'CET-1CEST-2,M3.5.0/02:00:00,M10.5.0/03:00:00' }
LOCALE = {
        u'de_DE': 'de',
        u'es_ES': 'es',
        u'fr_FR': 'fr',
        u'fr_CA': 'fr',
        u'it_IT': 'it',
        u'nl_NL': 'nl',
        u'en_US': 'en'
    }

FUNCKEY_TYPES = {
        u'speeddial': 0,
        u'blf': 1,
        u'park': 9
    }


class BaseFanvilHTTPDeviceInfoExtractor(object):
    _PATH_REGEX = re.compile(r'(\b00a859\w{6})\.cfg$')
    
    def extract(self, request, request_type):
        return defer.succeed(self._do_extract(request))

    def _do_extract(self, request):
        return self._extract_from_path(request)

    def _extract_from_path(self, request):
        m = self._PATH_REGEX.search(request.path)
        if 'f0C00620000.cfg' in request.path:
            return {u'vendor': u'Fanvil',
                    u'model' : u'C62'}
        if m:
            raw_mac = m.group(1)
            mac = norm_mac(raw_mac.decode('ascii'))
            return {u'mac': mac}

class BaseFanvilPgAssociator(BasePgAssociator):
    def __init__(self, models, version):
        BasePgAssociator.__init__(self)
        self._models = models
        self._version = version

    def _do_associate(self, vendor, model, version):
        if vendor == u'Fanvil':
            if model in self._models:
                if version == self._version:
                    return FULL_SUPPORT
                return COMPLETE_SUPPORT
            return UNKNOWN_SUPPORT
        return IMPROBABLE_SUPPORT


class BaseFanvilPlugin(StandardPlugin):
    _ENCODING = 'UTF-8'

    def __init__(self, app, plugin_dir, gen_cfg, spec_cfg):
        StandardPlugin.__init__(self, app, plugin_dir, gen_cfg, spec_cfg)
        # update to use the non-standard tftpboot directory
        self._base_tftpboot_dir = self._tftpboot_dir
        self._tftpboot_dir = os.path.join(self._tftpboot_dir, 'Fanvil')

        self._tpl_helper = TemplatePluginHelper(plugin_dir)

        downloaders = FetchfwPluginHelper.new_downloaders(gen_cfg.get('proxies'))
        fetchfw_helper = FetchfwPluginHelper(plugin_dir, downloaders)
        # update to use the non-standard tftpboot directory
        fetchfw_helper.root_dir = self._tftpboot_dir

        self.services = fetchfw_helper.services()
        self.http_service = HTTPNoListingFileService(self._base_tftpboot_dir)

    http_dev_info_extractor = BaseFanvilHTTPDeviceInfoExtractor()

    def _dev_specific_filename(self, device):
        # Return the device specific filename (not pathname) of device
        fmted_mac = format_mac(device[u'mac'], separator='', uppercase=False)
        return fmted_mac + '.cfg'

    def _check_config(self, raw_config):
        if u'http_port' not in raw_config:
            raise RawConfigError('only support configuration via HTTP')

    def _check_device(self, device):
        if u'mac' not in device:
            raise Exception('MAC address needed for device configuration')

    def configure(self, device, raw_config):
        self._check_config(raw_config)
        self._check_device(device)
        self._check_lines_password(raw_config)
        self._add_timezone(raw_config)
        self._add_locale(raw_config)
        self._add_fkeys(raw_config)
        filename = self._dev_specific_filename(device)
        tpl = self._tpl_helper.get_dev_template(filename, device)

        path = os.path.join(self._tftpboot_dir, filename)
        self._tpl_helper.dump(tpl, raw_config, path, self._ENCODING)

    def deconfigure(self, device):
        self._remove_configuration_file(device)

    def configure_common(self, raw_config):
        for filename, fw_filename, tpl_filename in self._COMMON_FILES:
            tpl = self._tpl_helper.get_template('common/%s' % tpl_filename)
            dst = os.path.join(self._tftpboot_dir, filename)
            raw_config[u'XX_fw_filename'] = fw_filename
            self._tpl_helper.dump(tpl, raw_config, dst, self._ENCODING)

    def _remove_configuration_file(self, device):
        path = os.path.join(self._tftpboot_dir, self._dev_specific_filename(device))
        try:
            os.remove(path)
        except OSError as e:
            logger.info('error while removing configuration file: %s', e)

    def synchronize(self, device, raw_config):
        try:
            ip = device[u'ip'].encode('ascii')
        except KeyError:
            return defer.fail(Exception('IP address needed for device synchronization'))
        else:
            sync_service = synchronize.get_sync_service()
            if sync_service is None or sync_service.TYPE != 'AsteriskAMI':
                return defer.fail(Exception('Incompatible sync service: %s' % sync_service))
            else:
                return threads.deferToThread(sync_service.sip_notify, ip, 'check-sync')

    def _check_lines_password(self, raw_config):
        for line in raw_config[u'sip_lines'].itervalues():
            if line[u'password'] == u'autoprov':
                line[u'password'] = u''

    def _add_timezone(self, raw_config):
        if u'timezone' in raw_config and raw_config[u'timezone'] in TZ_NAME:
            raw_config[u'XX_timezone'] = TZ_NAME[raw_config[u'timezone']]
        else:
            raw_config['timezone'] = TZ_NAME['Europe/Paris']

    def _add_locale(self, raw_config):
       locale = raw_config.get(u'locale')
       if locale in LOCALE:
            raw_config[u'XX_locale'] = LOCALE[locale]

    def _add_fkeys(self, raw_config):
        lines = []
        for funckey_no, funckey_dict in raw_config[u'funckeys'].iteritems():
            i_funckey_no = int(funckey_no)
            funckey_type = funckey_dict[u'type']
            if funckey_type not in FUNCKEY_TYPES:
                logger.info('Unsupported funckey type: %s', funckey_type)
                continue
            type_code = u'P32%s' % (i_funckey_no + 2)
            lines.append(self._format_line(type_code, FUNCKEY_TYPES[funckey_type]))
            line_code = self._format_code(3*i_funckey_no - 2)
            lines.append(self._format_line(line_code, int(funckey_dict[u'line']) - 1))
            if u'label' in funckey_dict : 
                label_code = self._format_code(3*i_funckey_no - 1)
                lines.append(self._format_line(label_code, funckey_dict[u'label']))
            value_code = self._format_code(3*i_funckey_no)
            lines.append(self._format_line(value_code, funckey_dict[u'value']))
        raw_config[u'XX_fkeys'] = u'\n'.join(lines)

    def _format_line(self, code, value):
        return u'    <%s>%s</%s>' % (code, value, code)

    def _format_code(self, code):
        if code >= 10:
            str_code = str(code)
        else:
            str_code = u'0%s' % code
        return u'P3%s' % str_code
