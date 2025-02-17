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

import copy
import datetime
import json

from AccessControl import Unauthorized
from Acquisition import ImplicitAcquisitionWrapper
from senaite.jsonapi.interfaces import IUpdate

from bika.lims import api
from bika.lims.utils.analysisrequest import create_analysisrequest as create_ar
from DateTime import DateTime
from plone import api as ploneapi
from plone.behavior.interfaces import IBehaviorAssignable
from plone.jsonapi.core import router
from Products.ATContentTypes.utils import DT2dt
from Products.CMFPlone.controlpanel import browser as cp
from Products.CMFPlone.PloneBatch import Batch
from Products.ZCatalog.Lazy import LazyMap
from senaite.jsonapi import logger
from senaite.jsonapi import request as req
from senaite.jsonapi import underscore as u
from senaite.jsonapi.exceptions import APIError
from senaite.jsonapi.interfaces import IBatch
from senaite.jsonapi.interfaces import ICatalog
from senaite.jsonapi.interfaces import ICatalogQuery
from senaite.jsonapi.interfaces import IDataManager
from senaite.jsonapi.interfaces import IFieldManager
from senaite.jsonapi.interfaces import IInfo
from senaite.jsonapi.interfaces import ICreate
from zope.component import getAdapter
from zope.schema import getFieldNames
from zope.schema import getFields
from zope.component import queryAdapter

_marker = object()

DEFAULT_ENDPOINT = "senaite.jsonapi.v1.get"

CONTROLPANEL_INTERFACE_MAPPING = {
    'mail': [cp.mail.IMailSchema],
    'language': [cp.language.ILanguageSchema],
    'dateandtime': [cp.dateandtime.IDateAndTimeSchema],
    'usergroups': [cp.usergroups.IUserGroupsSettingsSchema,
                   cp.usergroups.ISecuritySchema],
    'maintenance': [cp.maintenance.IMaintenanceSchema],
}

SKIP_UPDATE_FIELDS = ["id", ]


# -----------------------------------------------------------------------------
#   JSON API (CRUD) Functions (called by the route providers)
# -----------------------------------------------------------------------------

# GET RECORD
def get_record(uid=None):
    """Get a single record
    """
    obj = None
    if uid is not None:
        obj = get_object_by_uid(uid)
    else:
        obj = get_object_by_request()
    if obj is None:
        fail(404, "No object found")
    complete = req.get_complete(default=_marker)
    if complete is _marker:
        complete = True
    items = make_items_for([obj], complete=complete)
    return u.first(items)


# GET BATCHED
def get_batched(portal_type=None, uid=None, endpoint=None, **kw):
    """Get batched results
    """

    # fetch the catalog results
    results = get_search_results(portal_type=portal_type, uid=uid, **kw)

    # fetch the batch params from the request
    size = req.get_batch_size()
    start = req.get_batch_start()

    # check for existing complete flag
    complete = req.get_complete(default=_marker)
    if complete is _marker:
        # if the uid is given, get the complete information set
        complete = uid and True or False

    # return a batched record
    return get_batch(results, size, start, endpoint=endpoint,
                     complete=complete)


# CREATE
def create_items(portal_type=None, uid=None, endpoint=None, **kw):
    """ create items

    1. If the uid is given, get the object and create the content in there
       (assumed that it is folderish)
    2. If the uid is 0, the target folder is assumed the portal.
    3. If there is no uid given, the payload is checked for either a key
        - `parent_uid`  specifies the *uid* of the target folder
        - `parent_path` specifies the *physical path* of the target folder
    """
    # disable CSRF
    req.disable_csrf_protection()

    # destination where to create the content
    container = uid and get_object_by_uid(uid) or None

    # extract the data from the request
    records = req.get_request_data()

    results = []
    for record in records:

        # get the portal_type
        if portal_type is None:
            # try to fetch the portal type out of the request data
            portal_type = record.pop("portal_type", None)

        if container is None:
            # find the container for content creation
            container = find_target_container(record)

        # Check if we have a container and a portal_type
        if not all([container, portal_type]):
            fail(400, "Please provide a container path/uid and portal_type")

        # check if it is allowed to create the portal_type
        if not is_creation_allowed(portal_type, container):
            fail(401, "Creation of '{}' in '{}' is not allowed".format(
                portal_type, api.get_path(container)))

        # create the object and pass in the record data
        obj = create_object(container, portal_type, **record)
        results.append(obj)

    if not results:
        fail(400, "No Objects could be created")

    return make_items_for(results, endpoint=endpoint)


# PATCH (alias for update_items)
def patch_items(portal_type=None, uid=None, endpoint=None, **kw):
    return update_items(portal_type=portal_type, uid=uid, endpoint=endpoint, **kw)


# PUT (alias for update_items)
def put_items(portal_type=None, uid=None, endpoint=None, **kw):
    return update_items(portal_type=portal_type, uid=uid, endpoint=endpoint, **kw)


# UPDATE
def update_items(portal_type=None, uid=None, endpoint=None, **kw):
    """ update items

    1. If the uid is given, the user wants to update the object with the data
       given in request body
    2. If no uid is given, the user wants to update a bunch of objects.
       -> each record contains either an UID, path or parent_path + id
    """

    # disable CSRF
    req.disable_csrf_protection()

    # the data to update
    records = req.get_request_data()

    # we have an uid -> try to get an object for it
    obj = get_object_by_uid(uid)
    if obj:
        record = records[0]  # ignore other records if we got an uid

        # Can this object be updated?
        if not is_update_allowed(obj):
            fail(401, "Update of {} is not allowed".format(api.get_path(obj)))

        obj = update_object_with_data(obj, record)
        return make_items_for([obj], endpoint=endpoint)

    # no uid -> go through the record items
    results = []
    for record in records:
        obj = get_object_by_record(record)

        # no object found for this record
        if obj is None:
            continue

        # Can this object be updated?
        if not is_update_allowed(obj):
            fail(401, "Update of {} is not allowed".format(api.get_path(obj)))

        # update the object with the given record data
        obj = update_object_with_data(obj, record)
        results.append(obj)

    if not results:
        fail(400, "No Objects could be updated")

    return make_items_for(results, endpoint=endpoint)


# DELETE
def delete_items(portal_type=None, uid=None, endpoint=None, **kw):
    """ delete items

    1. If the uid is given, we can ignore the request body and delete the
       object with the given uid (if the uid was valid).
    2. If no uid is given, the user wants to delete more than one item.
       => go through each item and extract the uid. Delete it afterwards.
       // we should do this kind of transaction base. So if we can not get an
       // object for an uid, no item will be deleted.
    3. we could check if the portal_type matches, just to be sure the user
       wants to delete the right content.
    """

    # disable CSRF
    req.disable_csrf_protection()

    # try to find the requested objects
    objects = find_objects(uid=uid)

    # We don't want to delete the portal object
    if filter(lambda o: is_root(o), objects):
        fail(400, "Can not delete the portal object")

    results = []
    for obj in objects:
        # We deactivate only!
        deactivate_object(obj)
        info = IInfo(obj)()
        results.append(info)

    if not results:
        fail(404, "No Objects could be found")

    return results


def make_items_for(brains_or_objects, endpoint=None, complete=False):
    """Generate API compatible data items for the given list of brains/objects

    :param brains_or_objects: List of objects or brains
    :type brains_or_objects: list/Products.ZCatalog.Lazy.LazyMap
    :param endpoint: The named URL endpoint for the root of the items
    :type endpoint: str/unicode
    :param complete: Flag to wake up the object and fetch all data
    :type complete: bool
    :returns: A list of extracted data items
    :rtype: list
    """

    # check if the user wants to include children
    include_children = req.get_children(False)

    def extract_data(brain_or_object):
        info = get_info(brain_or_object, endpoint=endpoint, complete=complete)
        if include_children and is_folderish(brain_or_object):
            info.update(get_children_info(brain_or_object, complete=complete))
        return info

    return map(extract_data, brains_or_objects)


# -----------------------------------------------------------------------------
#   Info Functions (JSON compatible data representation)
# -----------------------------------------------------------------------------

def get_info(brain_or_object, endpoint=None, complete=False):
    """Extract the data from the catalog brain or object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :param endpoint: The named URL endpoint for the root of the items
    :type endpoint: str/unicode
    :param complete: Flag to wake up the object and fetch all data
    :type complete: bool
    :returns: Data mapping for the object/catalog brain
    :rtype: dict
    """

    # also extract the brain data for objects
    if not is_brain(brain_or_object):
        brain_or_object = get_brain(brain_or_object)
        if brain_or_object is None:
            logger.warn("Couldn't find/fetch brain of {}".format(brain_or_object))
            return {}
        complete = True

    # When querying uid catalog we have to be sure that we skip the objects
    # used to relate two or more objects
    if is_relationship_object(brain_or_object):
        logger.warn("Skipping relationship object {}".format(repr(brain_or_object)))
        return {}

    # extract the data from the initial object with the proper adapter
    info = IInfo(brain_or_object).to_dict()

    # update with url info (always included)
    url_info = get_url_info(brain_or_object, endpoint)
    info.update(url_info)

    # include the parent url info
    parent = get_parent_info(brain_or_object)
    info.update(parent)

    # add the complete data of the object if requested
    # -> requires to wake up the object if it is a catalog brain
    if complete:
        # ensure we have a full content object
        obj = api.get_object(brain_or_object)
        # get the compatible adapter
        adapter = IInfo(obj)
        # update the data set with the complete information
        info.update(adapter.to_dict())

        # update the data set with the workflow information
        # -> only possible if `?complete=yes&workflow=yes`
        if req.get_workflow(False):
            info.update(get_workflow_info(obj))

        # # add sharing data if the user requested it
        # # -> only possible if `?complete=yes`
        # if req.get_sharing(False):
        #     sharing = get_sharing_info(obj)
        #     info.update({"sharing": sharing})

    return info


def get_url_info(brain_or_object, endpoint=None):
    """Generate url information for the content object/catalog brain

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :param endpoint: The named URL endpoint for the root of the items
    :type endpoint: str/unicode
    :returns: URL information mapping
    :rtype: dict
    """

    # If no endpoint was given, guess the endpoint by portal type
    if endpoint is None:
        endpoint = get_endpoint(brain_or_object)

    uid = get_uid(brain_or_object)
    portal_type = get_portal_type(brain_or_object)
    resource = portal_type_to_resource(portal_type)

    return {
        "uid": uid,
        "url": get_url(brain_or_object),
        "api_url": url_for(endpoint, resource=resource, uid=uid),
    }


def get_parent_info(brain_or_object, endpoint=None):
    """Generate url information for the parent object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :param endpoint: The named URL endpoint for the root of the items
    :type endpoint: str/unicode
    :returns: URL information mapping
    :rtype: dict
    """

    # special case for the portal object
    if is_root(brain_or_object):
        return {}

    # get the parent object
    parent = get_parent(brain_or_object)
    portal_type = get_portal_type(parent)
    resource = portal_type_to_resource(portal_type)

    # fall back if no endpoint specified
    if endpoint is None:
        endpoint = get_endpoint(parent)

    return {
        "parent_id": get_id(parent),
        "parent_uid": get_uid(parent),
        "parent_url": url_for(endpoint, resource=resource, uid=get_uid(parent))
    }


def get_children_info(brain_or_object, complete=False):
    """Generate data items of the contained contents

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :param complete: Flag to wake up the object and fetch all data
    :type complete: bool
    :returns: info mapping of contained content items
    :rtype: list
    """

    # fetch the contents (if folderish)
    children = get_contents(brain_or_object)

    def extract_data(brain_or_object):
        return get_info(brain_or_object, complete=complete)
    items = map(extract_data, children)

    return {
        "children_count": len(items),
        "children": items
    }


def get_file_info(obj, fieldname, default=None):
    """Extract file data from a file field

    :param obj: Content object
    :type obj: ATContentType/DexterityContentType
    :param fieldname: Schema name of the field
    :type fieldname: str/unicode
    :returns: File data mapping
    :rtype: dict
    """

    # extract the file field from the object if omitted
    field = get_field(obj, fieldname)

    # get the value with the fieldmanager
    fm = IFieldManager(field)

    # return None if we have no file data
    if fm.get_size(obj) == 0:
        return None

    out = {
        "content_type": fm.get_content_type(obj),
        "filename": fm.get_filename(obj),
        "download": fm.get_download_url(obj),
    }

    # only return file data only if requested (?filedata=yes)
    if req.get_filedata(False):
        data = fm.get_data(obj)
        out["data"] = data.encode("base64")

    return out


def get_workflow_info(brain_or_object, endpoint=None):
    """Generate workflow information of the assigned workflows

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :param endpoint: The named URL endpoint for the root of the items
    :type endpoint: str/unicode
    :returns: Workflows info
    :rtype: dict
    """

    # ensure we have a full content object
    obj = get_object(brain_or_object)

    # get the portal workflow tool
    wf_tool = get_tool("portal_workflow")

    # the assigned workflows of this object
    workflows = wf_tool.getWorkflowsFor(obj)

    # no worfkflows assigned -> return
    if not workflows:
        return []

    def to_transition_info(transition):
        """ return the transition information
        """
        return {
            "title": transition["title"],
            "value": transition["id"],
            "display": transition["description"],
            "url": transition["url"],
        }

    def to_review_history_info(review_history):
        """ return the transition information
        """
        converted = DT2dt(review_history.get('time')).\
            strftime("%Y-%m-%d %H:%M:%S")
        review_history['time'] = converted
        return review_history

    out = []

    for workflow in workflows:

        # get the status info of the current state (dictionary)
        info = wf_tool.getStatusOf(workflow.getId(), obj)
        if info is None:
            continue

        # get the current review_status
        review_state = info.get("review_state", None)
        inactive_state = info.get("inactive_state", None)
        cancellation_state = info.get("cancellation_state", None)
        worksheetanalysis_review_state = info.get("worksheetanalysis_review_state", None)

        state = review_state or \
            inactive_state or \
            cancellation_state or \
            worksheetanalysis_review_state

        if state is None:
            logger.warn("No state variable found for {} -> {}".format(
                repr(obj), info))
            continue

        # get the wf status object
        status_info = workflow.states[state]

        # get the title of the current status
        status = status_info.title

        # get the transition informations
        transitions = map(to_transition_info, wf_tool.getTransitionsFor(obj))

        # get the review history
        rh = map(to_review_history_info,
                 workflow.getInfoFor(obj, 'review_history', ''))

        out.append({
            "workflow": workflow.getId(),
            "status": status,
            "review_state": state,
            "transitions": transitions,
            "review_history": rh,
        })

    return {"workflow_info": out}


# -----------------------------------------------------------------------------
#   API
# -----------------------------------------------------------------------------

def fail(status, msg):
    """API Error
    """
    if msg is None:
        msg = "Reason not given."
    raise APIError(status, "{}".format(msg))


def search(**kw):
    """Search the catalog adapter

    :returns: Catalog search results
    :rtype: iterable
    """
    portal = get_portal()
    catalog = ICatalog(portal)
    catalog_query = ICatalogQuery(catalog)
    query = catalog_query.make_query(**kw)
    return catalog(query)


def get_search_results(portal_type=None, uid=None, **kw):
    """Search the catalog and return the results

    :returns: Catalog search results
    :rtype: iterable
    """

    # If we have an UID, return the object immediately
    if uid is not None:
        logger.info("UID '%s' found, returning the object immediately" % uid)
        return u.to_list(get_object_by_uid(uid))

    # allow to search search for the Plone Site with portal_type
    include_portal = False
    if u.to_string(portal_type) == "Plone Site":
        include_portal = True

    # The request may contain a list of portal_types, e.g.
    # `?portal_type=Document&portal_type=Plone Site`
    if "Plone Site" in u.to_list(req.get("portal_type")):
        include_portal = True

    # Build and execute a catalog query
    results = search(portal_type=portal_type, uid=uid, **kw)

    if include_portal:
        results = list(results) + u.to_list(get_portal())

    return results


def get_portal():
    """Proxy to senaite.api.get_portal
    """
    return api.get_portal()


def get_tool(name, default=_marker):
    """Proxy to senaite.api.get_tool
    """
    return api.get_tool(name, default=default)


def get_object(brain_or_object):
    """Proxy to senaite.api.get_object
    """
    return api.get_object(brain_or_object)


def get_brain(brain_or_object):
    """Return a ZCatalog brain for the object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: True if the object is a catalog brain
    :rtype: bool
    """
    if is_brain(brain_or_object):
        return brain_or_object
    if is_root(brain_or_object):
        return brain_or_object

    # fetch the brain by UID
    uid = get_uid(brain_or_object)
    uc = get_tool("uid_catalog")
    results = uc({"UID": uid}) or search(query={'UID': uid})
    if len(results) == 0:
        return None
    if len(results) > 1:
        fail(500, "More than one object with UID={} found in portal_catalog".format(uid))
    return results[0]


def is_brain(brain_or_object):
    """Proxy to senaite.api.is_brain
    """
    return api.is_brain(brain_or_object)


def is_at_content(brain_or_object):
    """Proxy to senaite.api.is_at_content
    """
    return api.is_at_content(brain_or_object)


def is_dexterity_content(brain_or_object):
    """Proxy to senaite.api.is_dexterity_content
    """
    return api.is_dexterity_content(brain_or_object)


def get_schema(brain_or_object):
    """Get the schema of the content

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: Schema object
    """
    obj = get_object(brain_or_object)
    if is_root(obj):
        return None
    if is_dexterity_content(obj):
        pt = get_tool("portal_types")
        fti = pt.getTypeInfo(obj.portal_type)
        return fti.lookupSchema()
    if is_at_content(obj):
        return obj.Schema()
    fail(400, "{} has no Schema.".format(repr(brain_or_object)))


def get_fields(brain_or_object):
    """Get the list of fields from the object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: List of fields
    :rtype: list
    """
    obj = get_object(brain_or_object)
    # The portal object has no schema
    if is_root(obj):
        return {}
    schema = get_schema(obj)
    if is_dexterity_content(obj):
        names = schema.names()
        fields = map(lambda name: schema.get(name), names)
        schema_fields = dict(zip(names, fields))
        # update with behavior fields
        schema_fields.update(get_behaviors(obj))
        return schema_fields
    return dict(zip(schema.keys(), schema.fields()))


def get_field(brain_or_object, name, default=None):
    """Return the named field
    """
    fields = get_fields(brain_or_object)
    return fields.get(name, default)


def get_behaviors(brain_or_object):
    """Iterate over all behaviors that are assigned to the object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: Behaviors
    :rtype: list
    """
    obj = get_object(brain_or_object)
    if not is_dexterity_content(obj):
        fail(400, "Only Dexterity contents can have assigned behaviors")
    assignable = IBehaviorAssignable(obj, None)
    if not assignable:
        return {}
    out = {}
    for behavior in assignable.enumerateBehaviors():
        for name, field in getFields(behavior.interface).items():
            out[name] = field
    return out


def is_root(brain_or_object):
    """Proxy to senaite.api.is_portal
    """
    return api.is_portal(brain_or_object)


def is_folderish(brain_or_object):
    """Proxy to senaite.api.is_folderish
    """
    return api.is_folderish(brain_or_object)


def is_uid(uid):
    """Checks if the passed in uid is a valid UID

    :param uid: The uid to check
    :type uid: string
    :return: True if the uid is a valid 32 alphanumeric uid or '0'
    :rtype: bool
    """
    if not isinstance(uid, basestring):
        return False
    if uid != "0" and len(uid) != 32:
        return False
    return True


def is_path(path):
    """Checks if the passed in path is a valid Path within the portal

    :param path: The path to check
    :type uid: string
    :return: True if the path is a valid path within the portal
    :rtype: bool
    """
    if not isinstance(path, basestring):
        return False
    portal_path = get_path(get_portal())
    if not path.startswith(portal_path):
        return False
    obj = get_object_by_path(path)
    if obj is None:
        return False
    return True

def calculate_delta_date(literal):
    """Calculate the date in the past from the given literal
    :param literal: A date literal, e.g. "today"
    :type literal: str
    :returns: Date between the literal and today
    :rtype: DateTime
    """
    mapping = {
        "today": 0,
        "yesterday": 1,
        "this-week": 7,
        "this-month": 30,
        "this-year": 365,
    }
    today = DateTime(DateTime().Date())  # current date without the time
    return today - mapping.get(literal, 0)


def is_json_serializable(thing):
    """Checks if the given thing can be serialized to JSON

    :param thing: The object to check if it can be serialized
    :type thing: arbitrary object
    :returns: True if it can be JSON serialized
    :rtype: bool
    """
    try:
        json.dumps(thing)
        return True
    except TypeError:
        return False


def to_json_value(obj, fieldname, value=_marker, default=None):
    """JSON save value encoding

    :param obj: Content object
    :type obj: ATContentType/DexterityContentType
    :param fieldname: Schema name of the field
    :type fieldname: str/unicode
    :param value: The field value
    :type value: depends on the field type
    :returns: JSON encoded field value
    :rtype: field dependent
    """

    # This function bridges the value of the field to a probably more complex
    # JSON structure to return to the client.

    # extract the value from the object if omitted
    if value is _marker:
        value = IDataManager(obj).json_data(fieldname)

    # convert objects
    if isinstance(value, ImplicitAcquisitionWrapper):
        return get_url_info(value)

    # check if the value is callable
    if callable(value):
        value = value()

    # convert dates
    if is_date(value):
        return to_iso_date(value)

    # check if the value is JSON serializable
    if not is_json_serializable(value):
        logger.warn("Output {} is not JSON serializable".format(repr(value)))
        return default

    return value


def is_date(thing):
    """Checks if the given thing represents a date

    :param thing: The object to check if it is a date
    :type thing: arbitrary object
    :returns: True if we have a date object
    :rtype: bool
    """
    # known date types
    date_types = (datetime.datetime,
                  datetime.date,
                  DateTime)
    return isinstance(thing, date_types)


def is_lazy_map(thing):
    """Checks if the passed in thing is a LazyMap

    :param thing: The thing to test
    :type thing: any
    :returns: True if the thing is a richtext value
    :rtype: bool
    """
    return isinstance(thing, LazyMap)


def to_iso_date(date, default=None):
    """ISO representation for the date object

    :param date: A date object
    :type field: datetime/DateTime
    :returns: The ISO format of the date
    :rtype: str
    """

    # not a date
    if not is_date(date):
        return default

    # handle Zope DateTime objects
    if isinstance(date, (DateTime)):
        return date.ISO8601()

    # handle python datetime objects
    return date.isoformat()


def get_contents(brain_or_object):
    """Lookup folder contents for this object.

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: List of contained contents
    :rtype: list/Products.ZCatalog.Lazy.LazyMap
    """

    # Nothing to do if the object is contentish
    if not is_folderish(brain_or_object):
        return []

    # Returning objects (not brains) to make sure we do not miss any child.
    # It may happen when children belong to different catalogs and not
    # found on 'portal_catalog'.
    ret = filter(lambda obj: api.is_object(obj),
                 api.get_object(brain_or_object).objectValues())
    return ret


def get_parent(brain_or_object):
    """Locate the parent object of the content/catalog brain

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: parent object
    :rtype: Parent content
    """

    if is_root(brain_or_object):
        return get_portal()

    if is_brain(brain_or_object):
        parent_path = get_parent_path(brain_or_object)
        return get_object_by_path(parent_path)

    return brain_or_object.aq_parent


def get_object_by_uid(uid, default=None):
    """Proxy to senaite.api.get_object_by_uid
    """
    return api.get_object_by_uid(uid, default)


def get_path(brain_or_object):
    """Proxy to senaite.api.get_path
    """
    return api.get_path(brain_or_object)


def get_parent_path(brain_or_object):
    """Proxy to senaite.api.get_parent_path
    """
    return api.get_parent_path(brain_or_object)


def get_id(brain_or_object):
    """Proxy to senaite.api.get_id
    """
    return api.get_id(brain_or_object)


def get_uid(brain_or_object):
    """Proxy to senaite.api.get_uid
    """
    return api.get_uid(brain_or_object)


def get_url(brain_or_object):
    """Proxy to senaite.api.get_url
    """
    return api.get_url(brain_or_object)


def get_portal_type(brain_or_object):
    """Proxy to senaite.api.get_portal_type
    """
    return api.get_portal_type(brain_or_object)


def do_transition_for(brain_or_object, transition):
    """Proxy to senaite.api.do_transition_for
    """
    return api.do_transition_for(brain_or_object, transition)


def get_portal_types():
    """Get a list of all portal types

    :retruns: List of portal type names
    :rtype: list
    """
    types_tool = get_tool("portal_types")
    return types_tool.listContentTypes()


def get_resource_mapping():
    """Map resources used in the routes to portal types

    :returns: Mapping of resource->portal_type
    :rtype: dict
    """
    portal_types = get_portal_types()
    resources = map(portal_type_to_resource, portal_types)
    return dict(zip(resources, portal_types))


def portal_type_to_resource(portal_type):
    """Converts a portal type name to a resource name

    :param portal_type: Portal type name
    :type name: string
    :returns: Resource name as it is used in the content route
    :rtype: string
    """
    resource = portal_type.lower()
    resource = resource.replace(" ", "")
    return resource


def resource_to_portal_type(resource):
    """Converts a resource to a portal type

    :param resource: Resource name as it is used in the content route
    :type name: string
    :returns: Portal type name
    :rtype: string
    """
    if resource is None:
        return None

    resource_mapping = get_resource_mapping()
    portal_type = resource_mapping.get(resource.lower())

    if portal_type is None:
        logger.warn("Could not map the resource '{}' "
                    "to any known portal type".format(resource))

    return portal_type


def is_creation_allowed(portal_type, container):
    """Checks if it is allowed to create the portal type

    :param portal_type: The portal type requested
    :type portal_type: string
    :container container: The parent of the object to be created
    :returns: True if it is allowed to create this object
    :rtype: bool
    """
    # Do not allow the creation of objects directly inside portal root
    if container == api.get_portal():
        return False

    # Do not allow the creation of objects directly inside setup folder
    if container == api.get_setup():
        return False

    # Check if the portal_type is allowed in the container
    container_info = container.getTypeInfo()
    if container_info.filter_content_types:
        if portal_type not in container_info.allowed_content_types:
            return False

    # Look for a create-specific adapter for this portal type and container
    adapter = queryAdapter(container, ICreate, name=portal_type)
    if adapter:
        return adapter.is_creation_allowed()

    return True


def is_update_allowed(obj):
    """Returns whether the update of the object passed in is supported

    :param obj: The object to be updated
    :type obj: ATContentType/DexterityContentType
    :returns: True if it is allowed to update this object
    :rtype: bool
    """
    # Do not allow to update the site itself
    if api.is_portal(obj):
        return False

    # Do not allow the update of objects that belong to site root folder
    parent = api.get_parent(obj)
    if api.is_portal(parent):
        return False

    # Do not allow the update of objects that belong to setup folder
    if parent == api.get_setup():
        return False

    # Look for an update-specific adapter for this object
    adapter = queryAdapter(obj, IUpdate)
    if adapter:
        return adapter.is_update_allowed()

    return True


def url_for(endpoint, default=DEFAULT_ENDPOINT, **values):
    """Looks up the API URL for the given endpoint

    :param endpoint: The name of the registered route (aka endpoint)
    :type endpoint: string
    :returns: External URL for this endpoint
    :rtype: string/None
    """

    try:
        return router.url_for(endpoint, force_external=True, values=values)
    except Exception:
        logger.warn("Could not build API URL for endpoint '%s'. "
                    "No route provider registered?" % endpoint)
        # build generic API URL
        return router.url_for(default, force_external=True, values=values)


def get_endpoint(brain_or_object, default=DEFAULT_ENDPOINT):
    """Calculate the endpoint for this object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: Endpoint for this object
    :rtype: string
    """
    portal_type = get_portal_type(brain_or_object)
    resource = portal_type_to_resource(portal_type)

    # Try to get the right namespaced endpoint
    endpoints = router.DefaultRouter.view_functions.keys()
    if resource in endpoints:
        return resource  # exact match
    endpoint_candidates = filter(lambda e: e.endswith(resource), endpoints)
    if len(endpoint_candidates) == 1:
        # only return the namespaced endpoint, if we have an exact match
        return endpoint_candidates[0]

    return default


def get_catalog():
    """Get catalog adapter

    :returns: ICatalog adapter for the Portal
    :rtype: CatalogTool
    """
    portal = get_portal()
    return ICatalog(portal)


def get_object_by_request():
    """Find an object by request parameters

    Inspects request parameters to locate an object

    :returns: Found Object or None
    :rtype: object
    """
    data = req.get_form() or req.get_query_string()
    return get_object_by_record(data)


def get_object_by_record(record):
    """Find an object by a given record

    Inspects request the record to locate an object

    :param record: A dictionary representation of an object
    :type record: dict
    :returns: Found Object or None
    :rtype: object
    """

    # nothing to do here
    if not record:
        return None

    if record.get("uid"):
        return get_object_by_uid(record["uid"])
    if record.get("path"):
        return get_object_by_path(record["path"])
    if record.get("parent_path") and record.get("id"):
        path = "/".join([record["parent_path"], record["id"]])
        return get_object_by_path(path)

    logger.warn("get_object_by_record::No object found! record='%r'" % record)
    return None


def get_object_by_path(path):
    """Find an object by a given physical path

    :param path: The physical path of the object to find
    :type path: string
    :returns: Found Object or None
    :rtype: object
    """

    # nothing to do here
    if not isinstance(path, basestring):
        return None

    # path must be a string
    path = str(path)

    portal = get_portal()
    portal_path = get_path(portal)

    if path == portal_path:
        return portal

    if path.startswith(portal_path):
        segments = path.split("/")
        path = "/".join(segments[2:])

    try:
        return portal.restrictedTraverse(str(path))
    except (KeyError, AttributeError):
        fail(404, "No object could be found at {}".format(str(path)))


def is_anonymous():
    """Check if the current user is authenticated or not

    :returns: True if the current user is authenticated
    :rtype: bool
    """
    return ploneapi.user.is_anonymous()


def get_current_user():
    """Get the current logged in user

    :returns: Member
    :rtype: object
    """
    return ploneapi.user.get_current()


def get_member_ids():
    """Return all member ids of the portal.
    """
    pm = get_tool("portal_membership")
    member_ids = pm.listMemberIds()
    # How can it be possible to get member ids with None?
    return filter(lambda x: x, member_ids)


def get_user(user_or_username=None):
    """Return Plone User

    :param user_or_username: Plone user or user id
    :type groupname:  PloneUser/MemberData/str
    :returns: Plone MemberData
    :rtype: object
    """
    if user_or_username is None:
        return None
    if hasattr(user_or_username, "getUserId"):
        return ploneapi.user.get(user_or_username.getUserId())
    return ploneapi.user.get(userid=u.to_string(user_or_username))


def get_user_properties(user_or_username):
    """Return User Properties

    :param user_or_username: Plone group identifier
    :type groupname:  PloneUser/MemberData/str
    :returns: Plone MemberData
    :rtype: object
    """
    user = get_user(user_or_username)
    if user is None:
        return {}
    if not callable(user.getUser):
        return {}
    out = {}
    plone_user = user.getUser()
    for sheet in plone_user.listPropertysheets():
        ps = plone_user.getPropertysheet(sheet)
        out.update(dict(ps.propertyItems()))
    return out


def find_objects(uid=None):
    """Find the object by its UID

    1. get the object from the given uid
    2. fetch objects specified in the request parameters
    3. fetch objects located in the request body

    :param uid: The UID of the object to find
    :type uid: string
    :returns: List of found objects
    :rtype: list
    """
    # The objects to cut
    objects = []

    # get the object by the given uid or try to find it by the request
    # parameters
    obj = get_object_by_uid(uid) or get_object_by_request()

    if obj:
        objects.append(obj)
    else:
        # no uid -> go through the record items
        records = req.get_request_data()
        for record in records:
            # try to get the object by the given record
            obj = get_object_by_record(record)

            # no object found for this record
            if obj is None:
                continue
            objects.append(obj)

    return objects


def find_target_container(record):
    """Locates a target container for the given portal_type and record

    :param record: The dictionary representation of a content object
    :type record: dict
    :returns: folder which contains the object
    :rtype: object
    """
    parent_uid = record.pop("parent_uid", None)
    parent_path = record.pop("parent_path", None)

    # Try to find the target object
    target = None
    if parent_uid:
        target = get_object_by_uid(parent_uid)
    elif parent_path:
        target = get_object_by_path(parent_path)

    if not target:
        fail(404, "No target container found")

    return target


def create_object(container, portal_type, **data):
    """Creates an object slug

    :returns: The new created content object
    :rtype: object
    """

    if "id" in data:
        # always omit the id as senaite LIMS generates a proper one
        id = data.pop("id")
        logger.warn("Passed in ID '{}' omitted! Senaite LIMS "
                    "generates a proper ID for you" .format(id))

    try:
        # Is there any adapter registered to handle the creation of this type?
        adapter = queryAdapter(container, ICreate, name=portal_type)
        if adapter and adapter.is_creation_delegated():
            logger.info("Delegating 'create' operation of '{}' in '{}'".format(
                portal_type, api.get_path(container)
            ))
            return adapter.create_object(**data)

        # Special case for ARs
        # => return immediately w/o update
        if portal_type == "AnalysisRequest":
            obj = create_analysisrequest(container, **data)
            # Omit values which are already set through the helper
            data = u.omit(data, "SampleType", "Analyses")
            # Set the container as the client, as the AR lives in it
            data["Client"] = container
            return obj
        # Standard content creation
        else:
            # we want just a minimun viable object and set the data later
            obj = api.create(container, portal_type)
            # obj = api.create(container, portal_type, **data)
    except Unauthorized:
        fail(401, "You are not allowed to create this content")

    # Update the object with the given data, but omit the id
    try:
        update_object_with_data(obj, data)
    except APIError:
        # Failure in creation process, delete the invalid object
        # NOTE: We bypass the permission checks
        container._delObject(obj.id)
        # reraise the error
        raise

    return obj


def create_analysisrequest(container, **data):
    """Create a minimun viable AnalysisRequest

    :param container: A single folderish catalog brain or content object
    :type container: ATContentType/DexterityContentType/CatalogBrain
    """
    container = get_object(container)
    request = req.get_request()
    return create_ar(container, request, data)


def update_object_with_data(content, record):
    """Update the content with the record data

    :param content: A single folderish catalog brain or content object
    :type content: ATContentType/DexterityContentType/CatalogBrain
    :param record: The data to update
    :type record: dict
    :returns: The updated content object
    :rtype: object
    :raises:
        APIError,
        :class:`~plone.jsonapi.routes.exceptions.APIError`
    """

    # ensure we have a full content object
    content = get_object(content)

    # Look for an update-specific adapter for this object
    adapter = queryAdapter(content, IUpdate)
    if adapter:
        # Use the adapter to update the object
        logger.info("Delegating 'update' operation of '{}'".format(
            api.get_path(content)
        ))
        adapter.update_object(**record)

    else:
        # Fall-back to default update machinery
        # get the proper data manager
        dm = IDataManager(content)

        if dm is None:
            fail(400, "Update for this object is not allowed")

        # Bail-out non-update-able fields
        purged_records = copy.deepcopy(record)
        map(lambda key: purged_records.pop(key, None), SKIP_UPDATE_FIELDS)

        # Iterate through record items
        for k, v in purged_records.items():
            try:
                success = dm.set(k, v, **record)
            except Unauthorized:
                fail(401, "Not allowed to set the field '%s'" % k)
            except ValueError, exc:
                fail(400, str(exc))

            if not success:
                logger.warn("update_object_with_data::skipping key=%r", k)
                continue

            logger.debug("update_object_with_data::field %r updated", k)

    # Validate the entire content object
    invalid = validate_object(content, record)
    if invalid:
        fail(400, u.to_json(invalid))

    # do a wf transition
    if record.get("transition", None):
        t = record.get("transition")
        logger.debug(">>> Do Transition '%s' for Object %s", t, content.getId())
        do_transition_for(content, t)

    # reindex the object
    content.reindexObject()
    return content


def validate_object(brain_or_object, data):
    """Validate the entire object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :param data: The sharing dictionary as returned from the API
    :type data: dict
    :returns: invalidity status
    :rtype: dict
    """
    obj = get_object(brain_or_object)

    # Call the validator of AT Content Types
    if is_at_content(obj):
        return obj.validate(data=data)

    return {}


def deactivate_object(brain_or_object):
    """Deactivate the given object

    :param brain_or_object: A single catalog brain or content object
    :type brain_or_object: ATContentType/DexterityContentType/CatalogBrain
    :returns: Nothing
    :rtype: None
    """
    obj = get_object(brain_or_object)
    # we do not want to delete the site root!
    if is_root(obj):
        fail(401, "Deactivating the Portal is not allowed")
    try:
        do_transition_for(brain_or_object, "deactivate")
    except Unauthorized:
        fail(401, "Not allowed to deactivate object '%s'" % obj.getId())


def get_registry_records_by_keyword(keyword=None):
    """Get all the registry records (names and values) whose name
     contains the specified keyword or, if keyword is None, return
     all registry items

    :param keyword: The keyword that has to be contained in the record name
    :type keyword: str or None
    :returns: Dictionary mapping the names of the found records to its values
    """
    portal_reg = ploneapi.portal.get_tool(name="portal_registry")
    found_registers = {}
    for record in portal_reg.records:
        if keyword is None:
            found_registers[record] = api.get_registry_record(record)
        elif keyword.lower() in record.lower():
            found_registers[record] = api.get_registry_record(record)
    return found_registers


def is_relationship_object(brain_or_object):
    """Checks if the passed in brain or object is a relationship object

    :param brain_or_object: A single catalog brain or content object
    :return: True if the object is a relationship object
    """
    if 'at_references' in get_path(brain_or_object):
        return True
    return False


def get_settings_by_keyword(keyword=None):
    """Get the settings associated to the specified keyword or, if
     keyword is None, get all the settings.

    :param keyword: settings to be retrieved
    :return: dictionary with the settings plus a key to identify from which
    keyword where retrieved.
    """
    settings = []
    if keyword is None:
        # iterate over all the schemas to return all settings
        for key, ischemas in CONTROLPANEL_INTERFACE_MAPPING.items():
            settings_from_ifaces = map(get_settings_from_interface, ischemas)
            settings_from_key = {k: v for d in settings_from_ifaces for k, v in d.items()}
            settings.append({key: settings_from_key,
                             "api_url": url_for("senaite.jsonapi.v1.settings", key=key)})
        return settings
    # if keyword has value then get only the settings associated to the key
    settings_from_ifaces = map(get_settings_from_interface, CONTROLPANEL_INTERFACE_MAPPING[keyword])
    settings_from_key = {k: v for d in settings_from_ifaces for k, v in d.items()}
    settings.append({keyword: settings_from_key,
                     "api_url": url_for("senaite.jsonapi.v1.settings", key=keyword)})
    return settings


def get_settings_from_interface(iface):
    """Get the configuration settings associated to a list of schema
    interfaces

    :param iface: The schema interface from which we want to get its
    fields
    :return: Dictionary with iface name as key and as value a dictionary
    with the setting names (keys) linked to that schema and its
    values.
    """
    settings = {}
    schema_id = iface.getName()
    settings[schema_id] = {}
    schema = getAdapter(api.get_portal(), iface)
    for setting in getFieldNames(iface):
        value = getattr(schema, setting, None)
        if is_json_serializable(value):
            settings[schema_id][setting] = value
    return settings

# -----------------------------------------------------------------------------
#   Batching Helpers
# -----------------------------------------------------------------------------

def get_batch(sequence, size, start=0, endpoint=None, complete=False):
    """ create a batched result record out of a sequence (catalog brains)
    """

    batch = make_batch(sequence, size, start)

    return {
        "pagesize": batch.get_pagesize(),
        "next": batch.make_next_url(),
        "previous": batch.make_prev_url(),
        "page": batch.get_pagenumber(),
        "pages": batch.get_numpages(),
        "count": batch.get_sequence_length(),
        "items": make_items_for([b for b in batch.get_batch()],
                                endpoint, complete=complete),
    }


def make_batch(sequence, size=25, start=0):
    """Make a batch of the given size from the sequence
    """
    # we call an adapter here to allow backwards compatibility hooks
    return IBatch(Batch(sequence, size, start))
