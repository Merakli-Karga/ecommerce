""" Views for interacting with the payment processor. """
from __future__ import absolute_import, unicode_literals

import iyzipay
import json
import logging
import waffle

from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View, TemplateView
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_class, get_model

from ecommerce.extensions.analytics.utils import parse_tracking_context
from ecommerce.extensions.basket.utils import basket_add_organization_attribute
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.offer.constants import DYNAMIC_DISCOUNT_FLAG
from ecommerce.extensions.payment.processors.iyzico import Iyzico

Applicator = get_class('offer.applicator', 'Applicator')
Basket = get_model('basket', 'Basket')
CartLine = get_model('basket', 'Line')

logger = logging.getLogger(__name__)


def get_basket_id_from_iyzico_id(iyzico_id):
    dot_placement = iyzico_id.rfind('.')
    return int(iyzico_id[dot_placement+1:] if dot_placement > 0 else 0)


def get_iyzico_id_from_basket_id(basket_id):
    return 'ozogretmen.courses.basket.{basket_id}'.format(basket_id=basket_id)


class IyzicoInitializationException(ValueError):
    pass


class BasketDiscountMixin(View):
    def _add_dynamic_discount_to_request(self, basket):
        if waffle.flag_is_active(self.request, DYNAMIC_DISCOUNT_FLAG) and basket.lines.count() == 1:
            raise ValueError('Iyzico payment does not support waffle {waf_name}'.format(waf_name=DYNAMIC_DISCOUNT_FLAG))

    def _get_basket(self, basket_id):
        basket = Basket.objects.get(pk=basket_id)
        basket.strategy = strategy.Default()

        self._add_dynamic_discount_to_request(basket)

        Applicator().apply(basket, basket.owner, self.request)

        basket_add_organization_attribute(basket, self.request.GET)
        return basket


class IyzicoPaymentView(BasketDiscountMixin):
    iyzico_template_name = 'payment/iyzico.html'
    error_template_name = 'payment/iyzico_callback_failed.html'

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """
        Request needs to be csrf_exempt to handle POST back from external payment processor.
        """
        return super(IyzicoPaymentView, self).dispatch(*args, **kwargs)

    @property
    def payment_processor(self):
        return Iyzico(self.request.site)

    @staticmethod
    def _get_address(user):
        address = dict([('address', '--')])
        address['contactName'] = user.username
        address['city'] = '--'
        address['country'] = '--'

        return address

    @staticmethod
    def _get_buyer_info(user):
        _, _, ip = parse_tracking_context(user, usage='embargo')
        buyer = dict([('id', str(user.id))])
        buyer['name'] = user.username
        buyer['surname'] = '--'
        buyer['email'] = user.email
        buyer['identityNumber'] = '-----'
        buyer['registrationAddress'] = '--'
        buyer['ip'] = ip or 'unknown'
        buyer['city'] = '--'
        buyer['country'] = '--'

        return buyer

    def initialize_form(self, basket, lang, base_url):
        options = {
            'api_key': self.payment_processor.api_key,
            'secret_key': self.payment_processor.secret_key,
            'base_url': self.payment_processor.base_url,
        }
        items = CartLine.objects.filter(basket=basket)

        if items.count() == 0:
            raise CartLine.DoesNotExist

        if items.count() > 1 and basket.total_discount > 0:
            # Shouldn't be possible. An exception just in case
            raise NotImplementedError

        total_price = 0
        request = dict([('locale', lang)])
        request['basketId'] = get_iyzico_id_from_basket_id(basket_id=basket.id)
        request['callbackUrl'] = '{base_url}/payment/iyzico/execute/'.format(base_url=base_url)

        request['buyer'] = self._get_buyer_info(user=basket.owner)

        address = self._get_address(user=basket.owner)
        request['shippingAddress'] = address
        request['billingAddress'] = address

        basket_items = []
        for item in items:
            basket_item = dict([('id', str(item.id))])
            basket_item['name'] = '{course_id}__{quantity}'.format(
                course_id=item.product.course.id,
                quantity=item.quantity
            )
            basket_item['category1'] = 'Courses'
            basket_item['itemType'] = 'VIRTUAL'
            basket_item['price'] = str(item.price_incl_tax * item.quantity)
            basket_items.append(basket_item)
            total_price += float(basket_item['price'])

        if basket.total_discount > 0:
            # This is always a one item discount. More than one item will raise NotImplementedError exception
            total_price = float(basket.total_incl_tax)
            basket_items[0]['price'] = str(total_price)

        request['basketItems'] = basket_items

        request['price'] = str(total_price)
        request['paidPrice'] = str(total_price)

        init = iyzipay.CheckoutFormInitialize()
        return init.create(request, options)

    def _update_context(self, request, context, basket):
        context['error'] = ''
        response = None
        try:
            lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
            response = self.initialize_form(
                basket=basket,
                lang=lang,
                base_url='{scheme}://{domain}'.format(
                    scheme=settings.DEFAULT_URL_SCHEME,
                    domain=self.request.site
                )
            )
        except CartLine.DoesNotExist:
            raise CartLine.DoesNotExist
        except Exception as e:
            logger.exception('Iyzico form initialization failed!')
            logger.exception(str(e))

        if response is None:
            context['error'] = 'Something went wrong. Please try again later.'
            logger.exception('Empty response from Iyzico!')
        else:
            bytes_data = response.read()
            data = json.loads(bytes_data)
            if data['status'] != 'success':
                raise IyzicoInitializationException(
                    'Iyzico Initialization Error {}: {}'.format(data['errorCode'], data['errorMessage'])
                )
            context['iyzico'] = data['checkoutFormContent']

    def get(self, request, basket_id):
        return self.post_or_get(request, basket_id)

    def post(self, request, basket_id):
        return self.post_or_get(request, basket_id)

    def post_or_get(self, request, basket_id):
        """
        Originally the view is called by a POST. but if auth is not done yet, it will be redirected into a GET

        This is why we need both GET and POST methods
        """
        user = request.user
        context = {}
        try:
            basket = self._get_basket(basket_id=basket_id)
        except Basket.DoesNotExist:
            logger.exception('Basket [{basket_id}] does not exist!'.format(basket_id=basket_id))
            template_name = self.error_template_name
        else:
            if basket.owner == user:
                try:
                    self._update_context(request, context, basket)
                except CartLine.DoesNotExist:
                    logger.exception('Basket [{basket_id}] is Empty!'.format(basket_id=basket_id))
                    template_name = self.error_template_name
                except IyzicoInitializationException as iyz_exc:
                    logger.exception(str(iyz_exc))
                    template_name = self.error_template_name
                else:
                    template_name = self.iyzico_template_name
            else:
                logger.exception('Basket [{basket_id}] does not belong to user [{username}]'.format(
                    basket_id=basket_id,
                    username=user.username
                ))
                template_name = self.error_template_name

        return render(request, template_name, context)


class IyzicoPaymentExecutionView(EdxOrderPlacementMixin, BasketDiscountMixin):
    @property
    def payment_processor(self):
        return Iyzico(self.request.site)

    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """
        Request needs to be csrf_exempt to handle POST back from external payment processor.
        """
        return super(IyzicoPaymentExecutionView, self).dispatch(*args, **kwargs)

    def post(self, request):
        logger.info('Starting Iyzico payment execute...')
        iyzico_response = request.POST.dict()
        token = iyzico_response.get('token', None)
        if token is None:
            logger.error('IyzicoPaymentExecutionView POST called with a (None) token')
            return redirect(self.payment_processor.error_url)

        data = self.payment_processor.retrieve_payment_info(token=token)
        if data is None:
            return redirect(self.payment_processor.error_url)

        basket_id = get_basket_id_from_iyzico_id(data['basketId'])
        logger.info('...processing Iyzico payment for basket id [{}]'.format(basket_id))

        try:
            basket = self._get_basket(basket_id=basket_id)
        except Basket.DoesNotExist:
            logger.exception('Basket [{basket_id}] does not exist!'.format(basket_id=basket_id))
            return redirect(self.payment_processor.error_url)

        receipt_url = get_receipt_page_url(
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration,
            disable_back_button=True,
        )
        try:
            with transaction.atomic():
                try:
                    logger.info('...handling Iyzico payment for basket id [{}]'.format(basket_id))
                    self.handle_payment(iyzico_response, basket)
                except PaymentError:
                    logger.exception('Payment Error for basket [{basket_id}]'.format(basket_id=basket_id))
                    return redirect(self.payment_processor.error_url)
        except:  # pylint: disable=bare-except
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            return redirect(receipt_url)

        try:
            logger.info('...creating order for basket id [{}]'.format(basket_id))
            order = self.create_order(request, basket)
        except Exception:  # pylint: disable=broad-except
            # any errors here will be logged in the create_order method. If we wanted any
            # Iyzico specific logging for this error, we would do that here.
            logger.exception('Error creating order for basket [{basket_id}]'.format(basket_id=basket_id))
            return redirect(receipt_url)

        try:
            logger.info('...handling post order process for basket id [{}]'.format(basket_id))
            self.handle_post_order(order)
        except Exception:  # pylint: disable=broad-except
            logger.exception('Error handling post order for basket [{basket_id}]'.format(basket_id=basket_id))
            self.log_order_placement_exception(basket.order_number, basket.id)

        logger.info('Payment for basket id [{}] completed successfully'.format(basket_id))
        return redirect(receipt_url)
