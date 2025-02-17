# -*- coding: utf-8 -*-
#
# This file is part of SENAITE.JSONAPI.
#
# SENAITE.JSONAPI is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright 2017-2020 by it's authors.
# Some rights reserved, see README and LICENSE.

import pkgutil

from senaite.jsonapi import logger
from senaite.jsonapi.v1 import routes
from senaite.jsonapi import add_route as add_senaite_route

__version__ = "2.0.1"
__date__ = "2021-07-27"

BASE_URL = "/senaite/v1"


def add_route(route, endpoint=None, **kw):
    """Add a new JSON API route
    """

    # ensure correct amout of slashes
    def apiurl(route):
        return '/'.join(s.strip('/') for s in ["", BASE_URL, route])

    return add_senaite_route(apiurl(route), endpoint, **kw)


prefix = routes.__name__ + "."
for importer, modname, ispkg in pkgutil.iter_modules(
        routes.__path__, prefix):
    module = __import__(modname, fromlist="dummy")
    logger.info("INITIALIZED SENAITE JSONAPI V1 ROUTE ---> %s" % module.__name__)
