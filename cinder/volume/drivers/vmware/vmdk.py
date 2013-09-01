# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2013 VMware, Inc.
# All Rights Reserved.
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

"""
Driver for virtual machines running on VMware supported datastores.
"""

from oslo.config import cfg

from cinder import exception
from cinder.openstack.common import log as logging
from cinder import units
from cinder.volume import driver
from cinder.volume.drivers.vmware import api
from cinder.volume.drivers.vmware import error_util
from cinder.volume.drivers.vmware import vim
from cinder.volume.drivers.vmware import volumeops
from cinder.volume import volume_types

LOG = logging.getLogger(__name__)
THIN_VMDK_TYPE = 'thin'
THICK_VMDK_TYPE = 'thick'
EAGER_ZEROED_THICK_VMDK_TYPE = 'eagerZeroedThick'

vmdk_opts = [
    cfg.StrOpt('vmware_host_ip',
               default=None,
               help='IP address for connecting to VMware ESX/VC server.'),
    cfg.StrOpt('vmware_host_username',
               default=None,
               help='Username for authenticating with VMware ESX/VC server.'),
    cfg.StrOpt('vmware_host_password',
               default=None,
               help='Password for authenticating with VMware ESX/VC server.',
               secret=True),
    cfg.StrOpt('vmware_wsdl_location',
               default=None,
               help='Optional VIM service WSDL Location '
                    'e.g http://<server>/vimService.wsdl. Optional over-ride '
                    'to default location for bug work-arounds.'),
    cfg.IntOpt('vmware_api_retry_count',
               default=10,
               help='Number of times VMware ESX/VC server API must be '
                    'retried upon connection related issues.'),
    cfg.IntOpt('vmware_task_poll_interval',
               default=5,
               help='The interval used for polling remote tasks invoked on '
                    'VMware ESX/VC server.'),
    cfg.StrOpt('vmware_volume_folder',
               default='cinder-volumes',
               help='Name for the folder in the VC datacenter that will '
                    'contain cinder volumes.')
]


def _get_volume_type_extra_spec(type_id, spec_key, possible_values,
                                default_value):
    """Get extra spec value.

    If the spec value is not present in the input possible_values, then
    default_value will be returned.
    If the type_id is None, then default_value is returned.

    The caller must not consider scope and the implementation adds/removes
    scope. The scope used here is 'vmware' e.g. key 'vmware:vmdk_type' and
    so the caller must pass vmdk_type as an input ignoring the scope.

    :param type_id: Volume type ID
    :param spec_key: Extra spec key
    :param possible_values: Permitted values for the extra spec
    :param default_value: Default value for the extra spec incase of an
                          invalid value or if the entry does not exist
    :return: extra spec value
    """
    if type_id:
        spec_key = ('vmware:%s') % spec_key
        spec_value = volume_types.get_volume_type_extra_specs(type_id,
                                                              spec_key)
        if spec_value in possible_values:
            LOG.debug(_("Returning spec value %s") % spec_value)
            return spec_value

        LOG.debug(_("Invalid spec value: %s specified.") % spec_value)

    # Default we return thin disk type
    LOG.debug(_("Returning default spec value: %s.") % default_value)
    return default_value


class VMwareEsxVmdkDriver(driver.VolumeDriver):
    """Manage volumes on VMware ESX server."""

    VERSION = '1.0'

    def __init__(self, *args, **kwargs):
        super(VMwareEsxVmdkDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(vmdk_opts)
        self._session = None
        self._stats = None
        self._volumeops = None

    @property
    def session(self):
        if not self._session:
            ip = self.configuration.vmware_host_ip
            username = self.configuration.vmware_host_username
            password = self.configuration.vmware_host_password
            api_retry_count = self.configuration.vmware_api_retry_count
            task_poll_interval = self.configuration.vmware_task_poll_interval
            wsdl_loc = self.configuration.safe_get('vmware_wsdl_location')
            self._session = api.VMwareAPISession(ip, username,
                                                 password, api_retry_count,
                                                 task_poll_interval,
                                                 wsdl_loc=wsdl_loc)
        return self._session

    @property
    def volumeops(self):
        if not self._volumeops:
            self._volumeops = volumeops.VMwareVolumeOps(self.session)
        return self._volumeops

    def do_setup(self, context):
        """Perform validations and establish connection to server.

        :param context: Context information
        """

        # Throw error if required parameters are not set.
        required_params = ['vmware_host_ip',
                           'vmware_host_username',
                           'vmware_host_password']
        for param in required_params:
            if not getattr(self.configuration, param, None):
                raise exception.InvalidInput(_("%s not set.") % param)

        # Create the session object for the first time
        self._volumeops = volumeops.VMwareVolumeOps(self.session)
        LOG.info(_("Successfully setup driver: %(driver)s for "
                   "server: %(ip)s.") %
                 {'driver': self.__class__.__name__,
                  'ip': self.configuration.vmware_host_ip})

    def check_for_setup_error(self):
        pass

    def get_volume_stats(self, refresh=False):
        """Obtain status of the volume service.

        :param refresh: Whether to get refreshed information
        """

        if not self._stats:
            backend_name = self.configuration.safe_get('volume_backend_name')
            if not backend_name:
                backend_name = self.__class__.__name__
            data = {'volume_backend_name': backend_name,
                    'vendor_name': 'VMware',
                    'driver_version': self.VERSION,
                    'storage_protocol': 'LSI Logic SCSI',
                    'reserved_percentage': 0,
                    'total_capacity_gb': 'unknown',
                    'free_capacity_gb': 'unknown'}
            self._stats = data
        return self._stats

    def create_volume(self, volume):
        """Creates a volume.

        We do not create any backing. We do it only for the first time
        it is being attached to a virtual machine.

        :param volume: Volume object
        """
        pass

    def _delete_volume(self, volume):
        """Delete the volume backing if it is present.

        :param volume: Volume object
        """
        backing = self.volumeops.get_backing(volume['name'])
        if not backing:
            LOG.info(_("Backing not available, no operation to be performed."))
            return
        self.volumeops.delete_backing(backing)

    def delete_volume(self, volume):
        """Deletes volume backing.

        :param volume: Volume object
        """
        self._delete_volume(volume)

    def _get_volume_group_folder(self, datacenter):
        """Return vmFolder of datacenter as we cannot create folder in ESX.

        :param datacenter: Reference to the datacenter
        :return: vmFolder reference of the datacenter
        """
        return self.volumeops.get_vmfolder(datacenter)

    def _select_datastore_summary(self, size_bytes, datastores):
        """Get best summary from datastore list that can accomodate volume.

        The implementation selects datastore based on maximum relative
        free space, which is (free_space/total_space) and has free space to
        store the volume backing.

        :param size_bytes: Size in bytes of the volume
        :param datastores: Datastores from which a choice is to be made
                           for the volume
        :return: Best datastore summary to be picked for the volume
        """
        best_summary = None
        best_ratio = 0
        for datastore in datastores:
            summary = self.volumeops.get_summary(datastore)
            if summary.freeSpace > size_bytes:
                ratio = float(summary.freeSpace) / summary.capacity
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_summary = summary

        if not best_summary:
            msg = _("Unable to pick datastore to accomodate %(size)s bytes "
                    "from the datastores: %(dss)s.")
            LOG.error(msg % {'size': size_bytes, 'dss': datastores})
            raise error_util.VimException(msg %
                                          {'size': size_bytes,
                                           'dss': datastores})

        LOG.debug(_("Selected datastore: %s for the volume.") % best_summary)
        return best_summary

    def _get_folder_ds_summary(self, size_gb, resource_pool, datastores):
        """Get folder and best datastore summary where volume can be placed.

        :param size_gb: Size of the volume in GB
        :param resource_pool: Resource pool reference
        :param datastores: Datastores from which a choice is to be made
                           for the volume
        :return: Folder and best datastore summary where volume can be
                 placed on
        """
        datacenter = self.volumeops.get_dc(resource_pool)
        folder = self._get_volume_group_folder(datacenter)
        size_bytes = size_gb * units.GiB
        datastore_summary = self._select_datastore_summary(size_bytes,
                                                           datastores)
        return (folder, datastore_summary)

    @staticmethod
    def _get_disk_type(volume):
        """Get disk type from volume type.

        :param volume: Volume object
        :return: Disk type
        """
        return _get_volume_type_extra_spec(volume['volume_type_id'],
                                           'vmdk_type',
                                           (THIN_VMDK_TYPE, THICK_VMDK_TYPE,
                                            EAGER_ZEROED_THICK_VMDK_TYPE),
                                           THIN_VMDK_TYPE)

    def _create_backing(self, volume, host):
        """Create volume backing under the given host.

        :param volume: Volume object
        :param host: Reference of the host
        :return: Reference to the created backing
        """
        # Get datastores and resource pool of the host
        (datastores, resource_pool) = self.volumeops.get_dss_rp(host)
        # Pick a folder and datastore to create the volume backing on
        (folder, summary) = self._get_folder_ds_summary(volume['size'],
                                                        resource_pool,
                                                        datastores)
        disk_type = VMwareEsxVmdkDriver._get_disk_type(volume)
        size_kb = volume['size'] * units.MiB
        return self.volumeops.create_backing(volume['name'],
                                             size_kb,
                                             disk_type, folder,
                                             resource_pool,
                                             host,
                                             summary.name)

    def _relocate_backing(self, size_gb, backing, host):
        pass

    def _create_backing_in_inventory(self, volume):
        """Creates backing under any suitable host.

        The method tries to pick datastore that can fit the volume under
        any host in the inventory.

        :param volume: Volume object
        :return: Reference to the created backing
        """
        # Get all hosts
        hosts = self.volumeops.get_hosts()
        if not hosts:
            msg = _("There are no hosts in the inventory.")
            LOG.error(msg)
            raise error_util.VimException(msg)

        backing = None
        for host in hosts:
            try:
                host = hosts[0].obj
                backing = self._create_backing(volume, host)
                break
            except error_util.VimException as excep:
                LOG.warn(_("Unable to find suitable datastore for "
                           "volume: %(vol)s under host: %(host)s. "
                           "More details: %(excep)s") %
                         {'vol': volume['name'], 'host': host, 'excep': excep})
        if backing:
            return backing
        msg = _("Unable to create volume: %(vol)s on the hosts: %(hosts)s.")
        LOG.error(msg % {'vol': volume['name'], 'hosts': hosts})
        raise error_util.VimException(msg %
                                      {'vol': volume['name'], 'hosts': hosts})

    def _initialize_connection(self, volume, connector):
        """Get information of volume's backing.

        If the volume does not have a backing yet. It will be created.

        :param volume: Volume object
        :param connector: Connector information
        :return: Return connection information
        """
        connection_info = {'driver_volume_type': 'vmdk'}

        backing = self.volumeops.get_backing(volume['name'])
        if 'instance' in connector:
            # The instance exists
            instance = vim.get_moref(connector['instance'], 'VirtualMachine')
            LOG.debug(_("The instance: %s for which initialize connection "
                        "is called, exists.") % instance)
            # Get host managing the instance
            host = self.volumeops.get_host(instance)
            if not backing:
                # Create a backing in case it does not exist under the
                # host managing the instance.
                LOG.info(_("There is no backing for the volume: %s. "
                           "Need to create one.") % volume['name'])
                backing = self._create_backing(volume, host)
            else:
                # Relocate volume is necessary
                self._relocate_backing(volume['size'], backing, host)
        else:
            # The instance does not exist
            LOG.debug(_("The instance for which initialize connection "
                        "is called, does not exist."))
            if not backing:
                # Create a backing in case it does not exist. It is a bad use
                # case to boot from an empty volume.
                LOG.warn(_("Trying to boot from an empty volume: %s.") %
                         volume['name'])
                # Create backing
                backing = self._create_backing_in_inventory(volume)

        # Set volume's moref value and name
        connection_info['data'] = {'volume': backing.value,
                                   'volume_id': volume['id']}

        LOG.info(_("Returning connection_info: %(info)s for volume: "
                   "%(volume)s with connector: %(connector)s.") %
                 {'info': connection_info,
                  'volume': volume['name'],
                  'connector': connector})

        return connection_info

    def initialize_connection(self, volume, connector):
        """Allow connection to connector and return connection info.

        The implementation returns the following information:
        {'driver_volume_type': 'vmdk'
         'data': {'volume': $VOLUME_MOREF_VALUE
                  'volume_id': $VOLUME_ID
                 }
        }

        :param volume: Volume object
        :param connector: Connector information
        :return: Return connection information
        """
        return self._initialize_connection(volume, connector)

    def terminate_connection(self, volume, connector, force=False, **kwargs):
        pass

    def create_export(self, context, volume):
        pass

    def ensure_export(self, context, volume):
        pass

    def remove_export(self, context, volume):
        pass

    def _create_snapshot(self, snapshot):
        """Creates a snapshot.

        If the volume does not have a backing then simply pass, else create
        a snapshot.

        :param snapshot: Snapshot object
        """
        backing = self.volumeops.get_backing(snapshot['volume_name'])
        if not backing:
            LOG.info(_("There is no backing, so will not create "
                       "snapshot: %s.") % snapshot['name'])
            return
        self.volumeops.create_snapshot(backing, snapshot['name'],
                                       snapshot['display_description'])
        LOG.info(_("Successfully created snapshot: %s.") % snapshot['name'])

    def create_snapshot(self, snapshot):
        """Creates a snapshot.

        :param snapshot: Snapshot object
        """
        self._create_snapshot(snapshot)

    def _delete_snapshot(self, snapshot):
        """Delete snapshot.

        If the volume does not have a backing or the snapshot does not exist
        then simply pass, else delete the snapshot.

        :param snapshot: Snapshot object
        """
        backing = self.volumeops.get_backing(snapshot['volume_name'])
        if not backing:
            LOG.info(_("There is no backing, and so there is no "
                       "snapshot: %s.") % snapshot['name'])
        else:
            self.volumeops.delete_snapshot(backing, snapshot['name'])
            LOG.info(_("Successfully deleted snapshot: %s.") %
                     snapshot['name'])

    def delete_snapshot(self, snapshot):
        """Delete snapshot.

        :param snapshot: Snapshot object
        """
        self._delete_snapshot(snapshot)

    def _clone_backing_by_copying(self, volume, backing):
        """Creates volume clone.

        Here we copy the backing on a datastore under the host and then
        register the copied backing to the inventory.
        It is assumed here that all the source backing files are in the
        same folder on the datastore.

        :param volume: New Volume object
        :param backing: Reference to backing entity that must be cloned
        :return: Reference to the cloned backing
        """
        src_path_name = self.volumeops.get_path_name(backing)
        # If we have path like /vmfs/volumes/datastore/vm/vm.vmx
        # we need to use /vmfs/volumes/datastore/vm/ are src_path
        splits = src_path_name.split('/')
        last_split = splits[len(splits) - 1]
        src_path = src_path_name[:-len(last_split)]
        # Pick a datastore where to create the full clone under same host
        host = self.volumeops.get_host(backing)
        (datastores, resource_pool) = self.volumeops.get_dss_rp(host)
        (folder, summary) = self._get_folder_ds_summary(volume['size'],
                                                        resource_pool,
                                                        datastores)
        dest_path = '[%s] %s' % (summary.name, volume['name'])
        # Copy source backing files to a destination location
        self.volumeops.copy_backing(src_path, dest_path)
        # Register the backing to the inventory
        dest_path_name = '%s/%s' % (dest_path, last_split)
        clone = self.volumeops.register_backing(dest_path_name,
                                                volume['name'], folder,
                                                resource_pool)
        LOG.info(_("Successfully cloned new backing: %s.") % clone)
        return clone

    def _create_cloned_volume(self, volume, src_vref):
        """Creates volume clone.

        If source volume's backing does not exist, then pass.
        Here we copy the backing on a datastore under the host and then
        register the copied backing to the inventory.
        It is assumed here that all the src_vref backing files are in the
        same folder on the datastore.

        :param volume: New Volume object
        :param src_vref: Volume object that must be cloned
        """
        backing = self.volumeops.get_backing(src_vref['name'])
        if not backing:
            LOG.info(_("There is no backing for the source volume: "
                       "%(svol)s. Not creating any backing for the "
                       "volume: %(vol)s.") %
                     {'svol': src_vref['name'],
                      'vol': volume['name']})
            return
        self._clone_backing_by_copying(volume, backing)

    def create_cloned_volume(self, volume, src_vref):
        """Creates volume clone.

        :param volume: New Volume object
        :param src_vref: Volume object that must be cloned
        """
        self._create_cloned_volume(volume, src_vref)

    def _create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        If the snapshot does not exist or source volume's backing does not
        exist, then pass.
        Else we perform _create_cloned_volume and then revert the backing to
        the appropriate snapshot point.

        :param volume: Volume object
        :param snapshot: Snapshot object
        """
        backing = self.volumeops.get_backing(snapshot['volume_name'])
        if not backing:
            LOG.info(_("There is no backing for the source snapshot: "
                       "%(snap)s. Not creating any backing for the "
                       "volume: %(vol)s.") %
                     {'snap': snapshot['name'],
                      'vol': volume['name']})
            return
        snapshot_moref = self.volumeops.get_snapshot(backing,
                                                     snapshot['name'])
        if not snapshot_moref:
            LOG.info(_("There is no snapshot point for the snapshoted volume: "
                       "%(snap)s. Not creating any backing for the "
                       "volume: %(vol)s.") %
                     {'snap': snapshot['name'], 'vol': volume['name']})
            return
        clone = self._clone_backing_by_copying(volume, backing)
        # Reverting the clone to the snapshot point.
        snapshot_moref = self.volumeops.get_snapshot(clone, snapshot['name'])
        self.volumeops.revert_to_snapshot(snapshot_moref)
        LOG.info(_("Successfully reverted clone: %(clone)s to snapshot: "
                   "%(snapshot)s.") %
                 {'clone': clone, 'snapshot': snapshot_moref})

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        :param volume: Volume object
        :param snapshot: Snapshot object
        """
        self._create_volume_from_snapshot(volume, snapshot)


class VMwareVcVmdkDriver(VMwareEsxVmdkDriver):
    """Manage volumes on VMware VC server."""

    def _get_volume_group_folder(self, datacenter):
        """Get volume group folder.

        Creates a folder under the vmFolder of the input datacenter with the
        volume group name if it does not exists.

        :param datacenter: Reference to the datacenter
        :return: Reference to the volume folder
        """
        vm_folder = super(VMwareVcVmdkDriver,
                          self)._get_volume_group_folder(datacenter)
        volume_folder = self.configuration.vmware_volume_folder
        return self.volumeops.create_folder(vm_folder, volume_folder)

    def _relocate_backing(self, size_gb, backing, host):
        """Relocate volume backing under host and move to volume_group folder.

        If the volume backing is on a datastore that is visible to the host,
        then need not do any operation.

        :param size_gb: Size of the volume in GB
        :param backing: Reference to the backing
        :param host: Reference to the host
        """
        # Check if volume's datastore is visible to host managing
        # the instance
        (datastores, resource_pool) = self.volumeops.get_dss_rp(host)
        datastore = self.volumeops.get_datastore(backing)

        visible_to_host = False
        for _datastore in datastores:
            if _datastore.value == datastore.value:
                visible_to_host = True
                break
        if visible_to_host:
            return

        # The volume's backing is on a datastore that is not visible to the
        # host managing the instance. We relocate the volume's backing.

        # Pick a folder and datastore to relocate volume backing to
        (folder, summary) = self._get_folder_ds_summary(size_gb, resource_pool,
                                                        datastores)
        LOG.info(_("Relocating volume: %(backing)s to %(ds)s and %(rp)s.") %
                 {'backing': backing, 'ds': summary, 'rp': resource_pool})
        # Relocate the backing to the datastore and folder
        self.volumeops.relocate_backing(backing, summary.datastore,
                                        resource_pool, host)
        self.volumeops.move_backing_to_folder(backing, folder)

    @staticmethod
    def _get_clone_type(volume):
        """Get clone type from volume type.

        :param volume: Volume object
        :return: Clone type from the extra spec if present, else return
                 default 'full' clone type
        """
        return _get_volume_type_extra_spec(volume['volume_type_id'],
                                           'clone_type',
                                           (volumeops.FULL_CLONE_TYPE,
                                            volumeops.LINKED_CLONE_TYPE),
                                           volumeops.FULL_CLONE_TYPE)

    def _clone_backing(self, volume, backing, snapshot, clone_type):
        """Clone the backing.

        :param volume: New Volume object
        :param backing: Reference to the backing entity
        :param snapshot: Reference to snapshot entity
        :param clone_type: type of the clone
        """
        datastore = None
        if not clone_type == volumeops.LINKED_CLONE_TYPE:
            # Pick a datastore where to create the full clone under same host
            host = self.volumeops.get_host(backing)
            (datastores, resource_pool) = self.volumeops.get_dss_rp(host)
            size_bytes = volume['size'] * units.GiB
            datastore = self._select_datastore_summary(size_bytes,
                                                       datastores).datastore
        clone = self.volumeops.clone_backing(volume['name'], backing,
                                             snapshot, clone_type, datastore)
        LOG.info(_("Successfully created clone: %s.") % clone)

    def _create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        If the snapshot does not exist or source volume's backing does not
        exist, then pass.

        :param volume: New Volume object
        :param snapshot: Reference to snapshot entity
        """
        backing = self.volumeops.get_backing(snapshot['volume_name'])
        if not backing:
            LOG.info(_("There is no backing for the snapshoted volume: "
                       "%(snap)s. Not creating any backing for the "
                       "volume: %(vol)s.") %
                     {'snap': snapshot['name'], 'vol': volume['name']})
            return
        snapshot_moref = self.volumeops.get_snapshot(backing,
                                                     snapshot['name'])
        if not snapshot_moref:
            LOG.info(_("There is no snapshot point for the snapshoted volume: "
                       "%(snap)s. Not creating any backing for the "
                       "volume: %(vol)s.") %
                     {'snap': snapshot['name'], 'vol': volume['name']})
            return
        clone_type = VMwareVcVmdkDriver._get_clone_type(volume)
        self._clone_backing(volume, backing, snapshot_moref, clone_type)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot.

        :param volume: New Volume object
        :param snapshot: Reference to snapshot entity
        """
        self._create_volume_from_snapshot(volume, snapshot)

    def _create_cloned_volume(self, volume, src_vref):
        """Creates volume clone.

        If source volume's backing does not exist, then pass.

        :param volume: New Volume object
        :param src_vref: Source Volume object
        """
        backing = self.volumeops.get_backing(src_vref['name'])
        if not backing:
            LOG.info(_("There is no backing for the source volume: %(src)s. "
                       "Not creating any backing for volume: %(vol)s.") %
                     {'src': src_vref['name'], 'vol': volume['name']})
            return
        clone_type = VMwareVcVmdkDriver._get_clone_type(volume)
        snapshot = None
        if clone_type == volumeops.LINKED_CLONE_TYPE:
            # For performing a linked clone, we snapshot the volume and
            # then create the linked clone out of this snapshot point.
            name = 'snapshot-%s' % volume['id']
            snapshot = self.volumeops.create_snapshot(backing, name, None)
        self._clone_backing(volume, backing, snapshot, clone_type)

    def create_cloned_volume(self, volume, src_vref):
        """Creates volume clone.

        :param volume: New Volume object
        :param src_vref: Source Volume object
        """
        self._create_cloned_volume(volume, src_vref)