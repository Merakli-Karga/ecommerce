""" Iyzico payment processing. """
from __future__ import absolute_import, unicode_literals

import logging
from decimal import Decimal
import iyzipay
import json

from ecommerce.core.url_utils import get_ecommerce_url
from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse


logger = logging.getLogger(__name__)


class Iyzico(BasePaymentProcessor):
    """
    Iyzico REST API (May 2015)

    For reference, see https://developer.iyzico.com/docs/api/.
    """

    NAME = 'iyzico'
    TITLE = 'Iyzico'
    DEFAULT_PROFILE_NAME = 'default'

    def __init__(self, site):
        """
        Constructs a new instance of the Iyzico processor.

        Raises:
            KeyError: If a required setting is not configured for this payment processor
        """
        super(Iyzico, self).__init__(site)

    @property
    def error_url(self):
        return get_ecommerce_url(self.configuration['error_path'])

    @property
    def api_key(self):
        return self.configuration['api_key']

    @property
    def secret_key(self):
        return self.configuration['secret_key']

    @property
    def base_url(self):
        return self.configuration['base_url']

    def retrieve_payment_info(self, token):
        options = {
            'api_key': self.api_key,
            'secret_key': self.secret_key,
            'base_url': self.base_url,
        }

        request = dict([('locale', 'en')])
        request['token'] = token
        checkout_form_auth = iyzipay.CheckoutForm()
        checkout_form_auth_response = checkout_form_auth.retrieve(request, options)
        bytes_data = checkout_form_auth_response.read()
        data = json.loads(bytes_data)
        if data.get('status', '') != 'success' or data.get('paymentStatus', '') != 'SUCCESS':
            data = None

        return data

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        user = request.user

        if basket.owner != user:
            basket_id = 0
        else:
            basket_id = basket.id

        parameters = {
            'payment_page_url': '/payment/iyzico/payment/{basket_id}/'.format(basket_id=basket_id),
        }
        return parameters

    def handle_processor_response(self, response, basket=None):
        token = response.get('token', 'nothing')
        payment_info = self.retrieve_payment_info(token=token)

        transaction_id = payment_info['paymentId']
        self.record_processor_response(payment_info, transaction_id=transaction_id, basket=basket)
        logger.info("Successfully executed Iyzico payment [%s] for basket [%d].", transaction_id, basket.id)
        currency = payment_info['currency']
        total = Decimal(payment_info['paidPrice'])
        transaction_id = transaction_id
        email = basket.owner.email
        label = 'Iyzico ({})'.format(email) if email else 'Iyzico Account'
        result = HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=label,
            card_type=None
        )

        return result

    def issue_credit(self, order_number, basket, reference_number, amount, currency):
        logger.exception('Iyzico.issue_credit is not implemented but got called somehow!')
        raise NotImplementedError("Line support method not implemented!")
