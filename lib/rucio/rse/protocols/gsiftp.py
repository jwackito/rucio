# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2014
# - Wen Guan, <wen.guan@cern.ch>, 2015
# - Tomas Javurek, Cedric Serfon, 2016

import json
import os
import requests

from rucio.common import exception
from rucio.rse.protocols import protocol


class Default(protocol.RSEProtocol):
    """ Implementing access to RSEs using gsiftp."""

    def __init__(self, protocol_attr, rse_settings):
        """ Initializes the object with information about the referred RSE.

            :param props Properties derived from the RSE Repository
        """
        super(Default, self).__init__(protocol_attr, rse_settings)

    def connect(self):
        """
        Establishes the actual connection to the referred RSE.
        If we decide to use gfal, init should be done here.

        :raises RSEAccessDenied
        """
        pass

    def close(self):
        """
        Closes the connection to RSE.
        """
        pass

    def get_space_usage(self):
        """
        Get RSE space usage information.

        :returns: a list with dict containing 'totalsize' and 'unusedsize'

        :raises ServiceUnavailable: if some generic error occured in the library.
        """
        # original
        rse_name = self.rse['rse']
        endpoint_path = ''.join([self.attributes['scheme'], '://', self.attributes['hostname'], ':', str(self.attributes['port']), '/atlas/dq2/site-size'])
        # dest = '/tmp/rucio-gsiftp-site-size_' + rse_name
        dest = '/var/log/rucio/tmp/rucio-gsiftp-site-size_' + rse_name

        space_usage_url = ''
        # url of space usage json, woud be nicer to have it in rse_settings
        agis = requests.get('http://atlas-agis-api.cern.ch/request/ddmendpoint/query/list/?json').json()
        agis_token = ''
        for res in agis:
            if rse_name == res['name']:
                agis_token = res['token']
                space_usage_url = res['space_usage_url']

        import gfal2
        try:
            if os.path.exists(dest):
                os.remove(dest)
            ctx = gfal2.creat_context()
            ctx.set_opt_string_list("SRM PLUGIN", "TURL_PROTOCOLS", ["gsiftp", "rfio", "gsidcap", "dcap", "kdcap"])
            params = ctx.transfer_parameters()
            params.timeout = 3600
            ret = ctx.filecopy(params, str(space_usage_url), str('file://' + dest))

            if ret == 0:
                data_file = open(dest)
                data = json.load(data_file)
                data_file.close()
                if data.keys()[0] != agis_token:
                    print 'ERROR: space usage json has different token as key'
                else:
                    totalsize = data[agis_token]['total_space']
                    used = data[agis_token]['used_space']
                    unusedsize = totalsize - used
                    return totalsize, unusedsize
        except Exception as e:
            print e
            raise exception.ServiceUnavailable(e)

        try:
            if os.path.exists(dest):
                os.remove(dest)
            ctx = gfal2.creat_context()
            params = ctx.transfer_parameters()
            params.timeout = 60
            ret = ctx.filecopy(params, str(endpoint_path), str('file://' + dest))
            if ret == 0:
                data_file = open(dest)
                data = json.load(data_file)
                data_file.close()
                totalsize = data['sizes']['total']
                availablesize = data['sizes']['available']
                # unusedsize = totalsize - availablesize # tjavurek responsible for correction
            return totalsize, availablesize
        except Exception as e:
            print e
            raise exception.ServiceUnavailable(e)
