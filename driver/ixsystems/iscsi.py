#  Copyright (c) 2016 iXsystems
#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.
"""
Volume driver for iXsystems iSCSI storage systems.

This driver requires iXsystems storage systems with installed iSCSI licenses.
"""

import simplejson as json
import re

from cinder.volume import driver
from cinder.volume.drivers.ixsystems import common
from cinder.volume.drivers.ixsystems import options
from cinder.volume.drivers.ixsystems.freenasapi import FreeNASApiError
from cinder.volume.drivers.ixsystems import utils as ix_utils
from cinder import context
import cinder.db.api as cinderapi
from cinder.message import api
from cinder.message.message_field import Action, Detail
from oslo_config import cfg
from oslo_log import log as logging
from cinder import interface

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.register_opts(options.ixsystems_connection_opts)
CONF.register_opts(options.ixsystems_transport_opts)
CONF.register_opts(options.ixsystems_basicauth_opts)
CONF.register_opts(options.ixsystems_apikeyauth_opts)
CONF.register_opts(options.ixsystems_provisioning_opts)

@interface.volumedriver
class FreeNASISCSIDriver(driver.ISCSIDriver):
    """FREENAS iSCSI volume driver."""

    VERSION = "2.0.0"
    IGROUP_PREFIX = 'openstack-'

    required_flags = ['ixsystems_transport_type', 'ixsystems_server_hostname',
                      'ixsystems_server_port', 'ixsystems_server_iscsi_port',
                      'ixsystems_volume_backend_name', 'ixsystems_vendor_name',
                      'ixsystems_storage_protocol', 'ixsystems_datastore_pool',
                      'ixsystems_dataset_path', 'ixsystems_iqn_prefix',]

    def __init__(self, *args, **kwargs):
        """Initialize FreeNASISCSIDriver Class."""

        LOG.info('iXsystems: Init Cinder Driver')
        super().__init__(*args, **kwargs)
        self.configuration.append_config_values(options.ixsystems_connection_opts)
        self.configuration.append_config_values(options.ixsystems_basicauth_opts)
        self.configuration.append_config_values(options.ixsystems_apikeyauth_opts)
        self.configuration.append_config_values(options.ixsystems_transport_opts)
        self.configuration.append_config_values(options.ixsystems_provisioning_opts)
        self.configuration.ixsystems_iqn_prefix += ':'
        self.common = common.TrueNASCommon(configuration=self.configuration)
        self.stats = {}

    def check_for_setup_error(self):
        """Check for iXsystems FREENAS configuration parameters."""
        LOG.info('iXSystems: Check For Setup Error')
        self.common.check_flags()

    def do_setup(self, context):
        """Setup iXsystems FREENAS driver.

            Check for configuration flags and setup iXsystems FREENAS client
        """
        LOG.info('iXsystems Do Setup')
        self.check_for_setup_error()
        self.common.do_custom_setup()

    def create_volume(self, volume):
        """Creates a volume of specified size and export it as iscsi target."""
        LOG.info('iXsystems Create Volume')
        LOG.debug(f'create_volume : volume name :: {volume["name"]}')

        freenas_volume = ix_utils.generate_freenas_volume_name(
            volume['name'],
            self.configuration.ixsystems_iqn_prefix)

        LOG.debug(f'volume name after freenas generate : \
        {json.dumps(freenas_volume)}')

        freenas_volume['size'] = volume['size']
        freenas_volume['target_size'] = volume['size']

        self.common.create_volume(freenas_volume['name'],
                                   freenas_volume['size'])
        # Remove LUN Creation from here,check at initi
        self.common.create_iscsitarget(freenas_volume['target'],
                                        freenas_volume['name'])

    def delete_volume(self, volume):
        """Deletes volume and corresponding iscsi target."""
        LOG.info('iXsystems Delete Volume')
        LOG.debug(f'delete_volume {volume["name"]}')

        freenas_volume = ix_utils.generate_freenas_volume_name(
            volume['name'],
            self.configuration.ixsystems_iqn_prefix)

        if freenas_volume['target']:
            self.common.delete_iscsitarget(freenas_volume['target'])
        if freenas_volume['name']:
            self.common.delete_volume(freenas_volume['name'])

    def create_export(self, context, volume, connector):
        """Driver entry point to get the export info for a new volume."""
        LOG.info('iXsystems Create Export')
        LOG.debug(f'create_export {volume["name"]}')

        handle = self.common.create_export(volume['name'])
        LOG.info(f'provider_location: {handle}')
        return {'provider_location': handle}

    def ensure_export(self, context, volume):
        """Driver entry point to get the export info for an existing volume."""
        LOG.info('iXsystems Ensure Export')
        LOG.debug(f'ensure_export {volume["name"]}')

        handle = self.common.create_export(volume['name'])
        LOG.info(f'provider_location: {handle}')
        return {'provider_location': handle}

    def remove_export(self, context, volume):
        """Driver exntry point to remove an export for a volume.

           we have nothing to do for unexporting.
        """

    def check_connection(self):
        """Connection safety check"""
        if ix_utils.parse_truenas_version(self.common.system_version())[1] in ('12.0', '13.0'):
            LOG.debug(f"Tunable: {str(self.common.tunable())}")
            tunable = self.common.tunable()
            # Default value from Truenas 12 kern.cam.ctl.max_ports 256,
            # kern.cam.ctl.max_luns 1024 common.tunable() returns a
            # list of dict
            # [{'var':'kern.cam.ctl.max_luns','enabled':True,'value':'256'},
            # {'var':'kern.cam.ctl.max_ports','enabled':True,'value':'1024'}]
            # Retrive attach_max_allow from min value of common.tunable()
            # kern.cam.ctl.max_luns and kern.cam.ctl.max_ports
            max_ports, max_luns = 256, 1024
            attach_max_allow = min(max_luns, max_ports)
            for item in tunable:
                if (item.get('enabled') and
                        item.get('var') == 'kern.cam.ctl.max_luns' and
                        str(item.get('value')).isnumeric()):
                    max_luns = int(item['value'])
                if (item.get('enabled') and
                        item.get('var') == 'kern.cam.ctl.max_ports' and
                        str(item.get('value')).isnumeric()):
                    max_ports = int(item['value'])
            attach_max_allow = min(max_luns, max_ports)
            LOG.debug(f"Tunable OS max_luns/max_ports: {attach_max_allow}")

            # check cinder driver already loaded before executing upstream code
            if (len(cinderapi.CONF.list_all_sections()) > 0):
                ctx = context.get_admin_context()
                vols = cinderapi.volume_get_all(ctx)
                attached_truenas_vol_count = 0
                attached_truenas_vol_count = len(
                    [vol for vol in vols
                        if vol.host and vol.host.find("@ixsystems-iscsi#") > 0
                            and vol.attach_status == 'attached'])
                if (attached_truenas_vol_count >= attach_max_allow):
                    LOG.error("Maximum lun/port limitation reached. Change \
                    kern.cam.ctl.max_luns and kern.cam.ctl.max_ports \
                    in tunable settings to allow more lun attachments.")
                    return False
        return True

    def initialize_connection(self, volume, connector):
        """Do connection validation for know faiture before return 
        connection to upstream cinder manager"""
        if self.check_connection() is False:
            exception = FreeNASApiError('Maximum lun/port limitation reached. \
            Change kern.cam.ctl.max_luns and kern.cam.ctl.max_ports in \
            tunable settings to allow more lun attachments.')
            message_api = api.API()
            ctx = context.get_admin_context()
            ctx.project_id = volume.project_id
            message_api.create(ctx, action=Action.ATTACH_VOLUME, resource_uuid=volume.id,
                               exception=exception, detail=Detail.ATTACH_ERROR)
            raise exception
        """Driver entry point to attach a volume to an instance."""
        LOG.info('iXsystems Initialise Connection')
        freenas_volume = ix_utils.generate_freenas_volume_name(
            volume['name'],
            self.configuration.ixsystems_iqn_prefix)

        if not freenas_volume['name']:
            # is this snapshot?
            freenas_volume = ix_utils.generate_freenas_snapshot_name(
                volume['name'],
                self.configuration.ixsystems_iqn_prefix)

        properties = {}
        properties['target_discovered'] = False
        properties['target_portal'] = ix_utils.get_iscsi_portal(
            self.configuration.ixsystems_server_hostname,
            self.configuration.ixsystems_server_iscsi_port)
        properties['target_iqn'] = freenas_volume['iqn']
        properties['volume_id'] = volume['id']

        LOG.debug(f'initialize_connection data: {properties}')
        return {'driver_volume_type': 'iscsi', 'data': properties}

    def terminate_connection(self, volume, connector, **kwargs):
        """Driver entry point to detach a volume from an instance."""

    def create_snapshot(self, snapshot):
        """Driver entry point for creating a snapshot."""
        LOG.info('iXsystems Create Snapshot')
        LOG.debug(f'create_snapshot {snapshot["name"]}')

        freenas_snapshot = ix_utils.generate_freenas_snapshot_name(
            snapshot['name'], self.configuration.ixsystems_iqn_prefix)
        freenas_volume = ix_utils.generate_freenas_volume_name(
            snapshot['volume_name'], self.configuration.ixsystems_iqn_prefix)

        self.common.create_snapshot(freenas_snapshot['name'],
                                     freenas_volume['name'])

    def delete_snapshot(self, snapshot):
        """Driver entry point for deleting a snapshot."""
        LOG.info('iXsystems Delete Snapshot')
        LOG.debug(f'delete_snapshot {snapshot["name"]}')
        freenas_snapshot = ix_utils.generate_freenas_snapshot_name(
            snapshot['name'],
            self.configuration.ixsystems_iqn_prefix)
        freenas_volume = ix_utils.generate_freenas_volume_name(
            snapshot['volume_name'],
            self.configuration.ixsystems_iqn_prefix)

        self.common.delete_snapshot(freenas_snapshot['name'],
                                     freenas_volume['name'])

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from snapshot."""
        LOG.info('iXsystems Create Volume From Snapshot')
        LOG.info(f'create_volume_from_snapshot {snapshot["name"]}')

        existing_vol = ix_utils.generate_freenas_volume_name(
            snapshot['volume_name'], self.configuration.ixsystems_iqn_prefix)
        freenas_snapshot = ix_utils.generate_freenas_snapshot_name(
            snapshot['name'], self.configuration.ixsystems_iqn_prefix)
        freenas_volume = ix_utils.generate_freenas_volume_name(
            volume['name'], self.configuration.ixsystems_iqn_prefix)
        freenas_volume['size'] = volume['size']
        freenas_volume['target_size'] = volume['size']

        self.common.create_volume_from_snapshot(freenas_volume['name'],
                                                 freenas_snapshot['name'],
                                                 existing_vol['name'])
        self.common.create_iscsitarget(freenas_volume['target'],
                                        freenas_volume['name'])

        # Promote image cache volume created by cinder service account
        # by checking project_id is cinder service project and display name match
        # image-[a-zA-Z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+ pattern
        # This is required because image cache volume cloned from the snapshot of first volume
        # provisioned by this image from upstream cinder flow code
        # Without promoting image cache volume, the first volume created can no longer be deleted
        if (self.configuration.safe_get('image_volume_cache_enabled')
            and self.common.is_service_project(volume['project_id'])
            and re.match(r"image-[a-zA-Z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+",
                         volume['display_name'])):
            self.common.promote_volume(freenas_volume['name'])

    def get_volume_stats(self, refresh=False):
        """Get stats info from volume group / pool."""
        LOG.info('iXsystems Get Volume Status')
        if refresh:
            self.stats = self.common.update_volume_stats()
        LOG.info(f'get_volume_stats: {self.stats}')
        return self.stats

    def create_cloned_volume(self, volume, src_vref):
        """Creates a volume from source volume."""
        LOG.info('iXsystems Create Cloned Volume')
        LOG.info(f'create_cloned_volume: {volume["id"]}')

        temp_snapshot = {'volume_name': src_vref['name'],
                         'name': f'name-{volume["id"]}'}

        self.create_snapshot(temp_snapshot)
        self.create_volume_from_snapshot(volume, temp_snapshot)
        # self.delete_snapshot(temp_snapshot)
        # with API v2.0 this causes FreeNAS error
        # "snapshot has dependent clones".  Cannot delete while volume is
        # active.  Instead, added check and deletion of orphaned dependent
        # clones in common._delete_volume()

    def extend_volume(self, volume, new_size):
        """Driver entry point to extend an existing volumes size."""
        LOG.info('iXsystems Extent Volume')
        LOG.info(f'extend_volume { volume["name"]}')

        freenas_volume = ix_utils.generate_freenas_volume_name(
            volume['name'], self.configuration.ixsystems_iqn_prefix)
        freenas_new_size = new_size

        if volume['size'] != freenas_new_size:
            self.common.extend_volume(freenas_volume['name'],
                                       freenas_new_size)