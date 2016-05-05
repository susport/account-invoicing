# -*- coding: utf-8 -*-
##############################################################################
# For copyright and license notices, see __openerp__.py file in module root
# directory
##############################################################################
from openerp import models, fields, api
from openerp.tools import float_round


class AccountInvoice(models.Model):
    _inherit = 'account.invoice'

    operation_ids = fields.One2many(
        'account.invoice.operation',
        'invoice_id',
        'Invoice Operations',
        readonly=True,
        copy=True,
        states={'draft': [('readonly', False)]}
    )
    journal_type = fields.Selection(
        related='journal_id.type'
    )

    @api.multi
    def action_run_operations(self):
        self.ensure_one()
        invoices = self
        total_percentage = sum(self.operation_ids.mapped('percentage'))
        last_quantities = {}
        invoice_type = self.type
        remaining_op = len(self.operation_ids)
        sale_orders = False
        # if sale installed we get linked sales orders to update invoice links
        if self.env['ir.model'].search(
                [('model', '=', 'sale.order')]):
            sale_orders = self.env['sale.order'].search(
                [('invoice_ids', 'in', [self.id])])
        # if purchase installed we also update links
        if self.env['ir.model'].search(
                [('model', '=', 'purchase.order')]):
            purchase_orders = self.env['purchase.order'].search(
                [('invoice_ids', 'in', [self.id])])

        for operation in self.operation_ids:
            default = {
                'operation_ids': False,
                # no copiamos para poder hacer control de las cantidades
                'invoice_line': False,
                # do not copy period (if we want it we should get right period
                # for the date, eg. in inter_company_rules)
                'period_id': False,
            }

            company = False
            journal = False
            # if op journal and is different from invoice journal
            if (
                    operation.journal_id and
                    operation.journal_id != self.journal_id):
                journal = operation.journal_id
                default['journal_id'] = journal.id
                company = operation.journal_id.company_id
                default['company_id'] = company.id
            # if op company and is different from invoice company
            elif (
                    operation.company_id and
                    operation.company_id != self.company_id):
                default['company_id'] = operation.company_id.id
                # we get a journal in new company
                journal = self.with_context(
                    company_id=company.id)._default_journal()
                default['journal_id'] = journal and journal.id or False
                company = operation.company_id

            if operation.date:
                default['date_invoice'] = operation.date
            elif operation.days and operation.days2:
                # TODO tal vez podamos pasar alguna fecha a esta funcion si
                # interesa
                default['date_invoice'] = operation._get_date()

            # if journal then journal has change and we need to
            # upate, at least, account_id
            if journal:
                partner_data = self.onchange_partner_id(
                    invoice_type, self.partner_id.id,
                    date_invoice=default.get(
                        'date_invoice', False) or self.date_invoice,
                    payment_term=self.payment_term,
                    company_id=company.id)['value']
                default.update({
                    'account_id': partner_data.get('account_id', False),
                    # we dont want to change fiscal position
                    # 'fiscal_position': partner_data.get(
                    #     'fiscal_position', False),
                    'partner_bank_id': partner_data.get(
                        'partner_bank_id', False),
                    'payment_term': partner_data.get('payment_term', False),
                })

            if operation.reference:
                default['reference'] = "%s%s" % (
                    self.reference, operation.reference)

            new_invoice = self.copy(default)

            for line in self.invoice_line:
                # if last operation and total perc 100 then we adjust qtys
                if remaining_op == 1 and total_percentage == 100.0:
                    new_quantity = last_quantities.get(line.id)
                else:
                    new_quantity = line.quantity * operation.percentage / 100.0
                    if operation.rounding:
                        new_quantity = float_round(
                            new_quantity,
                            precision_rounding=operation.rounding)
                    last_quantities[line.id] = (
                        last_quantities.get(
                            line.id, line.quantity) - new_quantity)

                line_defaults = {
                    'invoice_id': new_invoice.id,
                    'quantity': new_quantity,
                }

                # if company has change, then we need to update lines
                if company and company.id != self.company_id:
                    line_data = line.with_context(
                        force_company=company.id).sudo().product_id_change(
                            line.product_id.id,
                            line.product_id.uom_id.id,
                            qty=new_quantity,
                            name='',
                            type=invoice_type,
                            partner_id=self.partner_id.id,
                            fposition_id=self.fiscal_position,
                            company_id=company.id)
                    # we only update account and taxes
                    line_defaults.update({
                        'account_id': line_data['value']['account_id'],
                        'invoice_line_tax_id': [
                            (6, 0, line_data['value'].get(
                                'invoice_line_tax_id', []))],
                    })

                if new_quantity:
                    new_line = line.copy(line_defaults)
                    # if sale_orders we update links
                    if sale_orders:
                        sale_lines = self.env['sale.order.line'].search(
                            [('invoice_lines', 'in', [line.id])])
                        sale_lines.write({'invoice_lines': [(4, new_line.id)]})
                        sale_orders.write(
                            {'invoice_ids': [(4, new_invoice.id)]})
                    if purchase_orders:
                        purchas_lines = self.env['purchase.order.line'].search(
                            [('invoice_lines', 'in', [line.id])])
                        purchas_lines.write(
                            {'invoice_lines': [(4, new_line.id)]})
                        purchase_orders.write(
                            {'invoice_ids': [(4, new_invoice.id)]})

            # if no invoice lines then we unlink the invoice
            if not new_invoice.invoice_line:
                new_invoice.unlink()
            else:
                if operation.automatic_validation:
                    new_invoice.signal_workflow('invoice_open')

                invoices += new_invoice

            # update remaining operations
            remaining_op -= 1

        # if operations sum 100, then we delete parent invoice, if not, then
        # we delete operations after succes operation
        self.operation_ids.unlink()
        # we delete original invoice if at least one was created and no need
        # for thisone
        if total_percentage == 100.0 and len(invoices) > 1:
            invoices -= self
            self.unlink()
        else:
            for line in self.invoice_line:
                line.quantity = last_quantities.get(line.id)
            self.operation_ids.unlink()

        invoices.button_reset_taxes()

        if invoice_type in ['out_invoice', 'out_refund']:
            action_ref = 'account.action_invoice_tree1'
            form_view_ref = 'account.invoice_form'
        else:
            action_ref = 'account.action_invoice_tree2'
            form_view_ref = 'account.invoice_supplier_form'
        action = self.env.ref(action_ref)
        result = action.read()[0]

        if len(invoices) > 1:
            # result[
            result['domain'] = [('id', 'in', invoices.ids)]
        else:
            form_view = self.env.ref(form_view_ref)
            result['views'] = [(form_view.id, 'form')]
            result['res_id'] = invoices.id
        return result

    @api.multi
    def action_invoice_operation(self):
        self.ensure_one()
        action = self.env.ref(
            'account_invoice_operation.action_invoice_operation_wizard')
        action_read = action.read()[0]
        return action_read

    @api.multi
    def onchange_partner_id(
            self, type, partner_id, date_invoice=False,
            payment_term=False, partner_bank_id=False, company_id=False):
        result = super(AccountInvoice, self).onchange_partner_id(
            type, partner_id, date_invoice=date_invoice,
            payment_term=payment_term, partner_bank_id=partner_bank_id,
            company_id=company_id)
        if partner_id:
            partner = self.env['res.partner'].browse(partner_id)
            if partner.default_sale_invoice_plan:
                plan_vals = partner.default_sale_invoice_plan.get_plan_vals()
                result['value']['operation_ids'] = plan_vals
        return result
