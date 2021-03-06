# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 eBay Inc.
# Copyright (c) 2013 OpenStack LLC.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""The QoS specs extension"""

import webob

from cinder.api import extensions
from cinder.api.openstack import wsgi
from cinder.api.views import qos_specs as view_qos_specs
from cinder.api import xmlutil
from cinder import exception
from cinder.openstack.common import log as logging
from cinder.openstack.common.notifier import api as notifier_api
from cinder.openstack.common import strutils
from cinder.volume import qos_specs


LOG = logging.getLogger(__name__)

authorize = extensions.extension_authorizer('volume', 'qos_specs_manage')


class QoSSpecsTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.make_flat_dict('qos_specs', selector='qos_specs')
        return xmlutil.MasterTemplate(root, 1)


class QoSSpecTemplate(xmlutil.TemplateBuilder):
    # FIXME(zhiteng) Need to handle consumer
    def construct(self):
        tagname = xmlutil.Selector('key')

        def qosspec_sel(obj, do_raise=False):
            # Have to extract the key and value for later use...
            key, value = obj.items()[0]
            return dict(key=key, value=value)

        root = xmlutil.TemplateElement(tagname, selector=qosspec_sel)
        root.text = 'value'
        return xmlutil.MasterTemplate(root, 1)


def _check_specs(context, specs_id):
    try:
        qos_specs.get_qos_specs(context, specs_id)
    except exception.NotFound as ex:
        raise webob.exc.HTTPNotFound(explanation=unicode(ex))


class QoSSpecsController(wsgi.Controller):
    """The volume type extra specs API controller for the OpenStack API."""

    _view_builder_class = view_qos_specs.ViewBuilder

    @staticmethod
    def _notify_qos_specs_error(context, method, payload):
        notifier_api.notify(context,
                            'QoSSpecs',
                            method,
                            notifier_api.ERROR,
                            payload)

    @wsgi.serializers(xml=QoSSpecsTemplate)
    def index(self, req):
        """Returns the list of qos_specs."""
        context = req.environ['cinder.context']
        authorize(context)
        specs = qos_specs.get_all_specs(context)
        return self._view_builder.summary_list(req, specs)

    @wsgi.serializers(xml=QoSSpecsTemplate)
    def create(self, req, body=None):
        context = req.environ['cinder.context']
        authorize(context)

        if not self.is_valid_body(body, 'qos_specs'):
            raise webob.exc.HTTPBadRequest()

        specs = body['qos_specs']
        name = specs.get('name', None)
        if name is None or name == "":
            msg = _("Please specify a name for QoS specs.")
            raise webob.exc.HTTPBadRequest(explanation=msg)

        try:
            qos_specs.create(context, name, specs)
            spec = qos_specs.get_qos_specs_by_name(context, name)
            notifier_info = dict(name=name, specs=specs)
            notifier_api.notify(context, 'QoSSpecs',
                                'QoSSpecs.create',
                                notifier_api.INFO, notifier_info)
        except exception.InvalidInput as err:
            notifier_err = dict(name=name, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.create',
                                         notifier_err)
            raise webob.exc.HTTPBadRequest(explanation=str(err))
        except exception.QoSSpecsExists as err:
            notifier_err = dict(name=name, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.create',
                                         notifier_err)
            raise webob.exc.HTTPConflict(explanation=str(err))
        except exception.QoSSpecsCreateFailed as err:
            notifier_err = dict(name=name, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.create',
                                         notifier_err)
            raise webob.exc.HTTPInternalServerError(explanation=str(err))

        return self._view_builder.detail(req, spec)

    @wsgi.serializers(xml=QoSSpecsTemplate)
    def update(self, req, id, body=None):
        context = req.environ['cinder.context']
        authorize(context)

        if not self.is_valid_body(body, 'qos_specs'):
            raise webob.exc.HTTPBadRequest()
        specs = body['qos_specs']
        try:
            qos_specs.update(context, id, specs)
            notifier_info = dict(id=id, specs=specs)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.update',
                                notifier_api.INFO, notifier_info)
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.update',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.InvalidQoSSpecs as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.update',
                                         notifier_err)
            raise webob.exc.HTTPBadRequest(explanation=str(err))
        except exception.QoSSpecsUpdateFailed as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.update',
                                         notifier_err)
            raise webob.exc.HTTPInternalServerError(explanation=str(err))

        return body

    @wsgi.serializers(xml=QoSSpecsTemplate)
    def show(self, req, id):
        """Return a single qos spec item."""
        context = req.environ['cinder.context']
        authorize(context)

        try:
            spec = qos_specs.get_qos_specs(context, id)
        except exception.QoSSpecsNotFound as err:
            raise webob.exc.HTTPNotFound(explanation=str(err))

        return self._view_builder.detail(req, spec)

    def delete(self, req, id):
        """Deletes an existing qos specs."""
        context = req.environ['cinder.context']
        authorize(context)

        force = req.params.get('force', None)

        #convert string to bool type in strict manner
        force = strutils.bool_from_string(force)
        LOG.debug("Delete qos_spec: %(id)s, force: %(force)s" %
                  {'id': id, 'force': force})

        try:
            qos_specs.delete(context, id, force)
            notifier_info = dict(id=id)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.delete',
                                notifier_api.INFO, notifier_info)
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.delete',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.QoSSpecsInUse as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.delete',
                                         notifier_err)
            if force:
                msg = _('Failed to disassociate qos specs.')
                raise webob.exc.HTTPInternalServerError(explanation=msg)
            msg = _('Qos specs still in use.')
            raise webob.exc.HTTPBadRequest(explanation=msg)

        return webob.Response(status_int=202)

    def delete_keys(self, req, id, body):
        """Deletes specified keys in qos specs."""
        context = req.environ['cinder.context']
        authorize(context)

        if not (body and 'keys' in body
                and isinstance(body.get('keys'), list)):
            raise webob.exc.HTTPBadRequest()

        keys = body['keys']
        LOG.debug("Delete_key spec: %(id)s, keys: %(keys)s" %
                  {'id': id, 'keys': keys})

        try:
            qos_specs.delete_keys(context, id, keys)
            notifier_info = dict(id=id)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.delete_keys',
                                notifier_api.INFO, notifier_info)
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.delete_keys',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.QoSSpecsKeyNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.delete_keys',
                                         notifier_err)
            raise webob.exc.HTTPBadRequest(explanation=str(err))

        return webob.Response(status_int=202)

    @wsgi.serializers(xml=QoSSpecsTemplate)
    def associations(self, req, id):
        """List all associations of given qos specs."""
        context = req.environ['cinder.context']
        authorize(context)

        LOG.debug("Get associations for qos_spec id: %s" % id)

        try:
            associates = qos_specs.get_associations(context, id)
            notifier_info = dict(id=id)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.associations',
                                notifier_api.INFO, notifier_info)
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.associations',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.CinderException as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.associations',
                                         notifier_err)
            raise webob.exc.HTTPInternalServerError(explanation=str(err))

        return self._view_builder.associations(req, associates)

    def associate(self, req, id):
        """Associate a qos specs with a volume type."""
        context = req.environ['cinder.context']
        authorize(context)

        type_id = req.params.get('vol_type_id', None)

        if not type_id:
            msg = _('Volume Type id must not be None.')
            notifier_err = dict(id=id, error_message=msg)
            self._notify_qos_specs_error(context,
                                         'qos_specs.delete',
                                         notifier_err)
            raise webob.exc.HTTPBadRequest(explanation=msg)
        LOG.debug("Associate qos_spec: %(id)s with type: %(type_id)s" %
                  {'id': id, 'type_id': type_id})

        try:
            qos_specs.associate_qos_with_type(context, id, type_id)
            notifier_info = dict(id=id, type_id=type_id)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.associate',
                                notifier_api.INFO, notifier_info)
        except exception.VolumeTypeNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.associate',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.associate',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.InvalidVolumeType as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.associate',
                                         notifier_err)
            self._notify_qos_specs_error(context,
                                         'qos_specs.associate',
                                         notifier_err)
            raise webob.exc.HTTPBadRequest(explanation=str(err))
        except exception.QoSSpecsAssociateFailed as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.associate',
                                         notifier_err)
            raise webob.exc.HTTPInternalServerError(explanation=str(err))

        return webob.Response(status_int=202)

    def disassociate(self, req, id):
        """Disassociate a qos specs from a volume type."""
        context = req.environ['cinder.context']
        authorize(context)

        type_id = req.params.get('vol_type_id', None)

        if not type_id:
            msg = _('Volume Type id must not be None.')
            notifier_err = dict(id=id, error_message=msg)
            self._notify_qos_specs_error(context,
                                         'qos_specs.delete',
                                         notifier_err)
            raise webob.exc.HTTPBadRequest(explanation=msg)
        LOG.debug("Disassociate qos_spec: %(id)s from type: %(type_id)s" %
                  {'id': id, 'type_id': type_id})

        try:
            qos_specs.disassociate_qos_specs(context, id, type_id)
            notifier_info = dict(id=id, type_id=type_id)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.disassociate',
                                notifier_api.INFO, notifier_info)
        except exception.VolumeTypeNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.disassociate',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.disassociate',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.QoSSpecsDisassociateFailed as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.disassociate',
                                         notifier_err)
            raise webob.exc.HTTPInternalServerError(explanation=str(err))

        return webob.Response(status_int=202)

    def disassociate_all(self, req, id):
        """Disassociate a qos specs from all volume types."""
        context = req.environ['cinder.context']
        authorize(context)

        LOG.debug("Disassociate qos_spec: %s from all." % id)

        try:
            qos_specs.disassociate_all(context, id)
            notifier_info = dict(id=id)
            notifier_api.notify(context, 'QoSSpecs',
                                'qos_specs.disassociate_all',
                                notifier_api.INFO, notifier_info)
        except exception.QoSSpecsNotFound as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.disassociate_all',
                                         notifier_err)
            raise webob.exc.HTTPNotFound(explanation=str(err))
        except exception.QoSSpecsDisassociateFailed as err:
            notifier_err = dict(id=id, error_message=str(err))
            self._notify_qos_specs_error(context,
                                         'qos_specs.disassociate_all',
                                         notifier_err)
            raise webob.exc.HTTPInternalServerError(explanation=str(err))

        return webob.Response(status_int=202)


class Qos_specs_manage(extensions.ExtensionDescriptor):
    """QoS specs support"""

    name = "Qos_specs_manage"
    alias = "qos-specs"
    namespace = "http://docs.openstack.org/volume/ext/qos-specs/api/v1"
    updated = "2013-08-02T00:00:00+00:00"

    def get_resources(self):
        resources = []
        res = extensions.ResourceExtension(
            Qos_specs_manage.alias,
            QoSSpecsController(),
            member_actions={"associations": "GET",
                            "associate": "GET",
                            "disassociate": "GET",
                            "disassociate_all": "GET",
                            "delete_keys": "PUT"})

        resources.append(res)

        return resources
