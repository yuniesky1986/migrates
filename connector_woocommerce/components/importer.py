# © 2009 Tech-Receptives Solutions Pvt. Ltd.
# © 2018 FactorLibre
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

import logging
from datetime import datetime
from odoo import fields, _
from odoo.addons.component.core import AbstractComponent
from odoo.addons.connector.exception import IDMissingInBackend
from odoo.addons.queue_job.exception import NothingToDoJob

_logger = logging.getLogger(__name__)


class WooImporter(AbstractComponent):
    """ Base importer for WooCommerce """

    _name = 'woocommerce.importer'
    _inherit = ['base.importer', 'base.woocommerce.connector']
    _usage = 'record.importer'

    def __init__(self, work_context):
        super(WooImporter, self).__init__(work_context)
        self.external_id = None
        self.woo_record = None

    def _get_woo_data(self):
        """ Return the raw WooCommerce data for ``self.external_id`` """
        return self.backend_adapter.read(self.external_id)

    def _before_import(self):
        """ Hook called before the import, when we have the WooCommerce
        data"""

    def _is_uptodate(self, binding):
        """Return True if the import should be skipped because
        it is already up-to-date in OpenERP"""
        WOO_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
        dt_fmt = WOO_DATETIME_FORMAT
        assert self.woo_record
        if not self.woo_record:
            return  # no update date on WooCommerce, always import it.
        if not binding:
            return  # it does not exist so it should not be skipped
        sync = binding.sync_date
        if not sync:
            return
        from_string = fields.Datetime.from_string
        sync_date = from_string(sync)
        self.woo_record['updated_at'] = {}
        self.woo_record['updated_at'] = {'to': datetime.now().strftime(dt_fmt)}
        woo_date = from_string(self.woo_record['updated_at']['to'])
        # if the last synchronization date is greater than the last
        # update in woo, we skip the import.
        # Important: at the beginning of the exporters flows, we have to
        # check if the woo_date is more recent than the sync_date
        # and if so, schedule a new import. If we don't do that, we'll
        # miss changes done in WooCommerce
        return woo_date < sync_date

    def _import_dependency(self, external_id, binding_model,
                           importer=None, always=False):
        """ Import a dependency.

        The importer class is a class or subclass of
        :class:`WooImporter`. A specific class can be defined.

        :param external_id: id of the related binding to import
        :param binding_model: name of the binding model for the relation
        :type binding_model: str | unicode
        :param importer_cls: :class:`openerp.addons.connector.\
                                     connector.ConnectorUnit`
                             class or parent class to use for the export.
                             By default: WooImporter
        :type importer_cls: :class:`openerp.addons.connector.\
                                    connector.MetaConnectorUnit`
        :param always: if True, the record is updated even if it already
                       exists, note that it is still skipped if it has
                       not been modified on WooCommerce since the last
                       update. When False, it will import it only when
                       it does not yet exist.
        :type always: boolean
        """
        if not external_id:
            return
        binder = self.binder_for(binding_model)
        if always or not binder.to_internal(external_id):
            if importer is None:
                importer = self.component(usage='record.importer',
                                          model_name=binding_model)
            try:
                importer.run(external_id)
            except NothingToDoJob:
                _logger.info(
                    'Dependency import of %s(%s) has been ignored.',
                    binding_model._name, external_id
                )

    def _import_dependencies(self):
        """ Import the dependencies for the record

        Import of dependencies can be done manually or by calling
        :meth:`_import_dependency` for each dependency.
        """
        return

    def _map_data(self):
        """ Returns an instance of
        :py:class:`~openerp.addons.connector.unit.mapper.MapRecord`

        """
        return self.mapper.map_record(self.woo_record)

    def _validate_data(self, data):
        """ Check if the values to import are correct

        Pro-actively check before the ``_create`` or
        ``_update`` if some fields are missing or invalid.

        Raise `InvalidDataError`
        """
        return

    def _must_skip(self):
        """ Hook called right after we read the data from the backend.

        If the method returns a message giving a reason for the
        skipping, the import will be interrupted and the message
        recorded in the job (if the import is called directly by the
        job, not by dependencies).

        If it returns None, the import will continue normally.

        :returns: None | str | unicode
        """
        return

    def _get_binding(self):
        return self.binder.to_internal(self.external_id)

    def _create_data(self, map_record, **kwargs):
        return map_record.values(for_create=True, **kwargs)

    def _create(self, data):
        """ Create the OpenERP record """
        # special check on data before import
        self._validate_data(data)
        model = self.model.with_context(connector_no_export=True)
        binding = model.create(data)
        _logger.debug('%d created from woo %s', binding, self.external_id)
        return binding

    def _update_data(self, map_record, **kwargs):
        return map_record.values(**kwargs)

    def _update(self, binding, data):
        """ Update an OpenERP record """
        # special check on data before import
        self._validate_data(data)
        binding.with_context(connector_no_export=True).write(data)
        _logger.debug('%d updated from woo %s', binding, self.external_id)
        return

    def _after_import(self, binding):
        """ Hook called at the end of the import """
        return

    def run(self, external_id, force=False):
        """ Run the synchronization

        :param external_id: identifier of the record on WooCommerce
        """
        self.external_id = external_id
        lock_name = 'import({}, {}, {}, {})'.format(
            self.backend_record._name,
            self.backend_record.id,
            self.work.model_name,
            external_id,
        )

        try:
            self.woo_record = self._get_woo_data()
        except IDMissingInBackend:
            return _('Record does no longer exist in WooCommerce')

        skip = self._must_skip()
        if skip:
            return skip

        binding = self._get_binding()
        if not force and self._is_uptodate(binding):
            return _('Already up-to-date.')

        # Keep a lock on this import until the transaction is committed
        # The lock is kept since we have detected that the informations
        # will be updated into Odoo
        self.advisory_lock_or_retry(lock_name)
        self._before_import()

        # import the missing linked resources
        self._import_dependencies()

        map_record = self._map_data()

        if binding:
            record = self._update_data(map_record)
            self._update(binding, record)
        else:
            record = self._create_data(map_record)
            binding = self._create(record)
        self.binder.bind(self.external_id, binding)

        self._after_import(binding)


class BatchImporter(AbstractComponent):
    """ The role of a BatchImporter is to search for a list of
    items to import, then it can either import them directly or delay
    the import of each item separately.
    """

    _name = 'woocommerce.batch.importer'
    _inherit = ['base.importer', 'base.woocommerce.connector']
    _usage = 'batch.importer'

    def run(self, filters=None):
        """ Run the synchronization """
        record_ids = self.backend_adapter.search(filters)
        for record_id in record_ids:
            self._import_record(record_id)

    def _import_record(self, record_id):
        """ Import a record directly or delay the import of the record.

        Method to implement in sub-classes.
        """
        raise NotImplementedError


class DirectBatchImporter(AbstractComponent):
    """ Import the records directly, without delaying the jobs. """
    _name = 'woocommerce.direct.batch.importer'
    _inherit = 'woocommerce.batch.importer'

    def _import_record(self, record_id):
        """ Import the record directly """
        self.model.import_record(self.backend_record, record_id)


class DelayedBatchImporter(AbstractComponent):
    """ Delay import of the records """

    _name = 'woocommerce.delayed.batch.importer'
    _inherit = 'woocommerce.batch.importer'

    def _import_record(self, external_id, job_options=None, **kwargs):
        """ Delay the import of the records"""
        delayable = self.model.with_delay(**job_options or {})
        delayable.import_record(self.backend_record, external_id, **kwargs)
