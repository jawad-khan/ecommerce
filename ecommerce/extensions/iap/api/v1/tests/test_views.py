import datetime
import urllib.error
import urllib.parse

import ddt
import mock
import pytz
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from oscar.apps.order.exceptions import UnableToPlaceOrder
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_class, get_model
from testfixtures import LogCapture

from ecommerce.core.tests import toggle_switch
from ecommerce.coupons.tests.mixins import DiscoveryMockMixin
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.enterprise.tests.mixins import EnterpriseServiceMockMixin
from ecommerce.extensions.basket.constants import EMAIL_OPT_IN_ATTRIBUTE
from ecommerce.extensions.basket.tests.mixins import BasketMixin
from ecommerce.extensions.iap.api.v1.constants import (
    COURSE_ALREADY_PAID_ON_DEVICE,
    ERROR_ALREADY_PURCHASED,
    ERROR_BASKET_ID_NOT_PROVIDED,
    ERROR_BASKET_NOT_FOUND,
    ERROR_DURING_ORDER_CREATION,
    ERROR_DURING_PAYMENT_HANDLING,
    ERROR_DURING_POST_ORDER_OP,
    ERROR_WHILE_OBTAINING_BASKET_FOR_USER,
    LOGGER_BASKET_NOT_FOUND,
    LOGGER_PAYMENT_FAILED_FOR_BASKET,
    NO_PRODUCT_AVAILABLE,
    PRODUCTS_DO_NOT_EXIST
)
from ecommerce.extensions.iap.api.v1.google_validator import GooglePlayValidator
from ecommerce.extensions.iap.api.v1.ios_validator import IOSValidator
from ecommerce.extensions.iap.api.v1.serializers import MobileOrderSerializer
from ecommerce.extensions.iap.api.v1.views import MobileCoursePurchaseExecutionView
from ecommerce.extensions.iap.processors.android_iap import AndroidIAP
from ecommerce.extensions.iap.processors.ios_iap import IOSIAP
from ecommerce.extensions.order.utils import UserAlreadyPlacedOrder
from ecommerce.extensions.payment.exceptions import RedundantPaymentNotificationError
from ecommerce.extensions.payment.tests.mixins import PaymentEventsMixin
from ecommerce.extensions.test.factories import create_basket, create_order
from ecommerce.tests.factories import ProductFactory, StockRecordFactory
from ecommerce.tests.mixins import LmsApiMockMixin
from ecommerce.tests.testcases import TestCase

Basket = get_model('basket', 'Basket')
BasketAttribute = get_model('basket', 'BasketAttribute')
BasketAttributeType = get_model('basket', 'BasketAttributeType')
Catalog = get_model('catalogue', 'Catalog')
Product = get_model('catalogue', 'Product')
Order = get_model('order', 'Order')
OrderLine = get_model('order', 'Line')
Selector = get_class('partner.strategy', 'Selector')
StockRecord = get_model('partner', 'StockRecord')
Voucher = get_model('voucher', 'Voucher')


@ddt.ddt
class MobileBasketAddItemsViewTests(DiscoveryMockMixin, LmsApiMockMixin, BasketMixin,
                                    EnterpriseServiceMockMixin, TestCase):
    """ MobileBasketAddItemsView view tests. """
    path = reverse('iap:mobile-basket-add')

    def setUp(self):
        super(MobileBasketAddItemsViewTests, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)

        self.course = CourseFactory(partner=self.partner)
        product = self.course.create_or_update_seat('verified', False, 50)
        self.stock_record = StockRecordFactory(product=product, partner=self.partner)
        self.catalog = Catalog.objects.create(partner=self.partner)
        self.catalog.stock_records.add(self.stock_record)

    def _get_response(self, product_skus, **url_params):
        qs = urllib.parse.urlencode({'sku': product_skus}, True)
        url = '{root}?{qs}'.format(root=self.path, qs=qs)
        for name, value in url_params.items():
            url += '&{}={}'.format(name, value)
        return self.client.get(url)

    def test_add_multiple_products_to_basket(self):
        """ Verify the basket accepts multiple products. """
        products = ProductFactory.create_batch(3, stockrecords__partner=self.partner)
        response = self._get_response([product.stockrecords.first().partner_sku for product in products])
        self.assertEqual(response.status_code, 200)

        request = response.wsgi_request
        basket = Basket.get_basket(request.user, request.site)
        self.assertEqual(basket.status, Basket.OPEN)
        self.assertEqual(basket.lines.count(), len(products))

    def test_add_multiple_products_no_skus_provided(self):
        """ Verify the Bad request exception is thrown when no skus are provided. """
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'No SKUs provided.')

    def test_add_multiple_products_no_available_products(self):
        """
        Verify that adding multiple products to the basket results in an error if
        the products do not exist.
        """
        response = self.client.get(self.path, data=[('sku', 1), ('sku', 2)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], PRODUCTS_DO_NOT_EXIST.format(skus='1, 2'))

    def test_all_already_purchased_products(self):
        """
        Test user can not purchase products again using the multiple item view
        """
        course = CourseFactory(partner=self.partner)
        product1 = course.create_or_update_seat("Verified", True, 0)
        product2 = course.create_or_update_seat("Professional", True, 0)
        stock_record = StockRecordFactory(product=product1, partner=self.partner)
        catalog = Catalog.objects.create(partner=self.partner)
        catalog.stock_records.add(stock_record)
        stock_record = StockRecordFactory(product=product2, partner=self.partner)
        catalog.stock_records.add(stock_record)

        with mock.patch.object(UserAlreadyPlacedOrder, 'user_already_placed_order', return_value=True):
            response = self._get_response(
                [product.stockrecords.first().partner_sku for product in [product1, product2]],
            )
            self.assertEqual(response.status_code, 406)
            self.assertEqual(response.json()['error'], ERROR_ALREADY_PURCHASED)

    def test_not_already_purchased_products(self):
        """
        Test user can purchase products which have not been already purchased
        """
        products = ProductFactory.create_batch(3, stockrecords__partner=self.partner)
        with mock.patch.object(UserAlreadyPlacedOrder, 'user_already_placed_order', return_value=False):
            response = self._get_response([product.stockrecords.first().partner_sku for product in products])
            self.assertEqual(response.status_code, 200)

    def test_one_already_purchased_product(self):
        """
        Test prepare_basket removes already purchased product and checkout for the rest of products
        """
        order = create_order(site=self.site, user=self.user)
        products = ProductFactory.create_batch(3, stockrecords__partner=self.partner)
        products.append(OrderLine.objects.get(order=order).product)
        response = self._get_response([product.stockrecords.first().partner_sku for product in products])
        request = response.wsgi_request
        basket = Basket.get_basket(request.user, request.site)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(basket.lines.count(), len(products) - 1)

    def test_no_available_product(self):
        """ The view should return HTTP 400 if the product is not available for purchase. """
        product = self.stock_record.product
        product.expires = pytz.utc.localize(datetime.datetime.min)
        product.save()
        self.assertFalse(Selector().strategy().fetch_for_product(product).availability.is_available_to_buy)

        expected_content = NO_PRODUCT_AVAILABLE
        response = self._get_response(self.stock_record.partner_sku)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], expected_content)

    def test_with_both_unavailable_and_available_products(self):
        """ Verify the basket ignores unavailable products and continue with available products. """
        products = ProductFactory.create_batch(3, stockrecords__partner=self.partner)

        products[0].expires = pytz.utc.localize(datetime.datetime.min)
        products[0].save()
        self.assertFalse(Selector().strategy().fetch_for_product(products[0]).availability.is_available_to_buy)

        response = self._get_response([product.stockrecords.first().partner_sku for product in products])
        self.assertEqual(response.status_code, 200)

        request = response.wsgi_request
        basket = Basket.get_basket(request.user, request.site)
        self.assertEqual(basket.status, Basket.OPEN)

    @ddt.data(
        ('false', 'False'),
        ('true', 'True'),
    )
    @ddt.unpack
    def test_email_opt_in_when_explicitly_given(self, opt_in, expected_value):
        """
        Verify the email_opt_in query string is saved into a BasketAttribute.
        """
        response = self._get_response(self.stock_record.partner_sku, email_opt_in=opt_in)
        request = response.wsgi_request
        basket = Basket.get_basket(request.user, request.site)
        basket_attribute = BasketAttribute.objects.get(
            basket=basket,
            attribute_type=BasketAttributeType.objects.get(name=EMAIL_OPT_IN_ATTRIBUTE),
        )
        self.assertEqual(basket_attribute.value_text, expected_value)

    def test_email_opt_in_when_not_given(self):
        """
        Verify that email_opt_in defaults to false if not specified.
        """
        response = self._get_response(self.stock_record.partner_sku)
        request = response.wsgi_request
        basket = Basket.get_basket(request.user, request.site)
        basket_attribute = BasketAttribute.objects.get(
            basket=basket,
            attribute_type=BasketAttributeType.objects.get(name=EMAIL_OPT_IN_ATTRIBUTE),
        )
        self.assertEqual(basket_attribute.value_text, 'False')


class MobileCoursePurchaseExecutionViewTests(PaymentEventsMixin, TestCase):
    """ MobileCoursePurchaseExecutionView view tests. """
    path = reverse('iap:iap-execute')

    def setUp(self):
        super(MobileCoursePurchaseExecutionViewTests, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)

        self.course = CourseFactory(partner=self.partner)
        product = self.course.create_or_update_seat('verified', False, 50)
        self.basket = create_basket(
            owner=self.user, site=self.site, price='50.0', product_class=product.product_class
        )
        self.basket.freeze()

        self.processor = AndroidIAP(self.site)
        self.processor_name = self.processor.NAME
        self.logger_name = 'ecommerce.extensions.iap.api.v1.views'

        self.post_data = {
            'transactionId': 'transactionId.android.test.purchased',
            'productId': 'android.test.purchased',
            'purchaseToken': 'inapp:org.edx.mobile:android.test.purchased',
            'payment_processor': 'android-iap',
            'basket_id': self.basket.id
        }
        order_message = "Order Failure: {} payment was received, but an order for basket [{}] could not be placed."
        self.order_placement_error_message = order_message.format(self.processor.NAME.title(), self.basket.id)

    def _assert_response(self, error_message):
        """
        Check if response is as expected.
        """
        response = self.client.post(self.path, data=self.post_data)
        self.assertEqual(response.json(), error_message)
        return response

    def test_payment_error(self):
        """
        Verify that a user who has approved payment is redirected to the configured receipt
        page when payment execution fails.
        """
        with mock.patch.object(MobileCoursePurchaseExecutionView, 'handle_payment',
                               side_effect=PaymentError) as fake_handle_payment:
            with LogCapture(self.logger_name) as logger:
                self._assert_response({'error': ERROR_DURING_PAYMENT_HANDLING})
                self.assertTrue(fake_handle_payment.called)

                logger.check(
                    (
                        self.logger_name,
                        'INFO',
                        'Payment [{payment_id}] approved by payer [{payer_id}]'.format(
                            payment_id=self.post_data.get('transactionId'),
                            payer_id=self.user.id
                        )
                    ),
                )

    def test_unanticipated_error_during_payment_handling(self):
        """
        Verify that a user who has approved payment is redirected to the configured receipt
        page when payment execution fails in an unanticipated manner.
        """
        with mock.patch.object(MobileCoursePurchaseExecutionView, 'handle_payment',
                               side_effect=KeyError) as fake_handle_payment:
            with LogCapture(self.logger_name) as logger:
                self._assert_response({'error': ERROR_DURING_PAYMENT_HANDLING})
                self.assertTrue(fake_handle_payment.called)

                logger.check_present(
                    (
                        self.logger_name,
                        'ERROR',
                        LOGGER_PAYMENT_FAILED_FOR_BASKET % (self.basket.id)
                    ),
                )

    def test_unable_to_place_order(self):
        """
        Verify that a user who has approved payment is redirected to the configured receipt
        page when the payment is executed but an order cannot be placed.
        """
        with mock.patch.object(MobileCoursePurchaseExecutionView, 'handle_order_placement',
                               side_effect=UnableToPlaceOrder) as fake_handle_order_placement, \
                mock.patch.object(GooglePlayValidator, 'validate') as fake_google_validation, \
                LogCapture(self.DUPLICATE_ORDER_LOGGER_NAME) as logger:
            fake_google_validation.return_value = {
                'resource': {
                    'orderId': 'orderId.android.test.purchased'
                }
            }
            self._assert_response({'error': ERROR_DURING_ORDER_CREATION})
            self.assertTrue(fake_google_validation.called)
            self.assertTrue(fake_handle_order_placement.called)
            logger.check(
                (self.DUPLICATE_ORDER_LOGGER_NAME, 'ERROR', self.order_placement_error_message)
            )

    def test_unanticipated_error_during_order_placement(self):
        """
        Verify that unanticipated errors during order placement are handled gracefully.
        """
        with mock.patch.object(MobileCoursePurchaseExecutionView, 'handle_order_placement',
                               side_effect=UnableToPlaceOrder) as fake_handle_order_placement, \
                mock.patch.object(GooglePlayValidator, 'validate') as fake_google_validation, \
                LogCapture(self.DUPLICATE_ORDER_LOGGER_NAME) as logger:
            fake_google_validation.return_value = {
                'resource': {
                    'orderId': 'orderId.android.test.purchased'
                }
            }
            self._assert_response({'error': 'An error occurred during order creation.'})
            self.assertTrue(fake_handle_order_placement.called)
            logger.check(
                (self.DUPLICATE_ORDER_LOGGER_NAME, 'ERROR', self.order_placement_error_message)
            )

    def test_duplicate_order_attempt_logging(self):
        """
        Verify that attempts at creation of a duplicate order are logged correctly
        """
        prior_order = create_order()
        dummy_view = MobileCoursePurchaseExecutionView()
        self.request.site = self.site
        dummy_view.request = self.request

        with LogCapture(self.DUPLICATE_ORDER_LOGGER_NAME) as log_capture, self.assertRaises(Exception):
            dummy_view.create_order(request=self.request, basket=prior_order.basket)
            log_capture.check(
                (
                    self.DUPLICATE_ORDER_LOGGER_NAME,
                    'ERROR',
                    self.get_duplicate_order_error_message(payment_processor='Paypal', order=prior_order)
                ),
            )

    def test_payment_error_with_no_basket(self):
        """
        Verify that we fail gracefully when any Exception occurred in _get_basket() method,
        logging the exception.
        """
        dummy_basket_id = self.basket.id + 1
        self.post_data['basket_id'] = dummy_basket_id
        with LogCapture(self.logger_name) as logger:
            self._assert_response({'error': ERROR_BASKET_NOT_FOUND.format(dummy_basket_id)})
            logger.check_present((self.logger_name, 'ERROR', LOGGER_BASKET_NOT_FOUND % dummy_basket_id))

    def test_payment_error_with_unanticipated_error_while_getting_basket(self):
        """
        Verify that we fail gracefully when an unanticipated Exception occurred while
        getting the basket.
        """
        with mock.patch.object(MobileCoursePurchaseExecutionView, '_get_basket', side_effect=KeyError), \
                LogCapture(self.logger_name) as logger:
            self._assert_response({'error': ERROR_WHILE_OBTAINING_BASKET_FOR_USER.format(self.user.email)})
            logger.check_present(
                (
                    self.logger_name,
                    'ERROR',
                    ERROR_WHILE_OBTAINING_BASKET_FOR_USER.format(self.user.email)
                ),
            )

    def test_iap_payment_execution_ios(self):
        """
        Verify that a user gets successful response if payment is handled correctly and
        order is created successfully.
        """
        ios_post_data = self.post_data
        ios_post_data['payment_processor'] = IOSIAP(self.site).NAME
        with mock.patch.object(IOSValidator, 'validate') as fake_ios_validation:
            fake_ios_validation.return_value = {
                'receipt': {
                    'in_app': [{
                        'original_transaction_id': '123456',
                        'transaction_id': '123456'
                    }]
                }
            }
            response = self.client.post(self.path, data=ios_post_data)
            order = Order.objects.get(number=self.basket.order_number)
            self.assertEqual(response.json(), {'order_data': MobileOrderSerializer(order).data})

    def test_iap_payment_execution_android(self):
        """
        Verify that a user gets successful response if payment is handled correctly and
        order is created successfully for Android.
        """
        with mock.patch.object(GooglePlayValidator, 'validate') as fake_google_validation:
            fake_google_validation.return_value = {
                'resource': {
                    'orderId': 'orderId.android.test.purchased'
                }
            }
            response = self.client.post(self.path, data=self.post_data)
            order = Order.objects.get(number=self.basket.order_number)
            self.assertEqual(response.json(), {'order_data': MobileOrderSerializer(order).data})

    def test_iap_payment_execution_basket_id_error(self):
        """
        Verify that a message is returned if basket_id is missing in
        """
        missing_basket_id_post_data = self.post_data
        missing_basket_id_post_data.pop('basket_id')
        error_message = '"{}"'.format(ERROR_BASKET_ID_NOT_PROVIDED)
        error_response = '{"error": ' + error_message + '}'
        expected_response = error_response.encode('UTF-8')
        expected_response_status_code = 400
        with mock.patch.object(GooglePlayValidator, 'validate') as fake_google_validation:
            fake_google_validation.return_value = {
                'resource': {
                    'orderId': 'orderId.android.test.purchased'
                }
            }
            response = self.client.post(self.path, data=missing_basket_id_post_data)
            self.assertEqual(response.status_code, expected_response_status_code)
            self.assertEqual(response.content, expected_response)

    @mock.patch('ecommerce.extensions.checkout.mixins.EdxOrderPlacementMixin.handle_payment')
    def test_redundant_payment_notification_error(self, mock_handle_payment):
        mock_handle_payment.side_effect = RedundantPaymentNotificationError()
        expected_response_status_code = 409
        error_message = COURSE_ALREADY_PAID_ON_DEVICE.encode('UTF-8')
        expected_response_content = b'{"error": "%s"}' % error_message
        with mock.patch.object(GooglePlayValidator, 'validate') as fake_google_validation:
            fake_google_validation.return_value = {
                'resource': {
                    'orderId': 'orderId.android.test.purchased'
                }
            }
            response = self.client.post(self.path, data=self.post_data)
            self.assertTrue(mock_handle_payment.called)
            self.assertEqual(response.status_code, expected_response_status_code)
            self.assertEqual(response.content, expected_response_content)

    @mock.patch('ecommerce.extensions.checkout.mixins.EdxOrderPlacementMixin.handle_post_order')
    def test_post_order_exception(self, mock_handle_post_order):
        mock_handle_post_order.side_effect = ValueError()
        expected_response_status_code = 200
        error_message = ERROR_DURING_POST_ORDER_OP.encode('UTF-8')
        expected_response_content = b'{"error": "%s"}' % error_message
        with mock.patch.object(GooglePlayValidator, 'validate') as fake_google_validation:
            fake_google_validation.return_value = {
                'resource': {
                    'orderId': 'orderId.android.test.purchased'
                }
            }
            response = self.client.post(self.path, data=self.post_data)
            self.assertTrue(mock_handle_post_order.called)
            self.assertEqual(response.status_code, expected_response_status_code)
            self.assertEqual(response.content, expected_response_content)


class TestMobileCheckoutView(TestCase):
    """ Tests for MobileCheckoutView API view. """
    path = reverse('iap:iap-checkout')

    def setUp(self):
        super(TestMobileCheckoutView, self).setUp()
        self.user = self.create_user()
        self.client.login(username=self.user.username, password=self.password)

        self.course = CourseFactory(partner=self.partner)
        product = self.course.create_or_update_seat('verified', False, 50)
        self.basket = create_basket(
            owner=self.user, site=self.site, price='50.0', product_class=product.product_class
        )

        self.processor = AndroidIAP(self.site)
        self.processor_name = self.processor.NAME

        self.post_data = {
            'basket_id': self.basket.id,
            'payment_processor': 'android-iap'
        }

    def test_authentication_required(self):
        """ Verify the endpoint requires authentication. """
        self.client.logout()
        response = self.client.post(self.path, data=self.post_data)
        self.assertEqual(response.status_code, 401)

    def test_no_basket(self):
        """ Verify the endpoint returns HTTP 400 if the user has no associated baskets. """
        self.user.baskets.all().delete()
        expected_content = b'{"error": "Basket [%s] not found."}' % str(self.post_data['basket_id']).encode()
        response = self.client.post(self.path, data=self.post_data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, expected_content)

    @override_settings(
        PAYMENT_PROCESSORS=['ecommerce.extensions.iap.processors.android_iap.AndroidIAP']
    )
    def test_view_response(self):
        """ Verify the endpoint returns a successful response when the user is able to checkout. """
        toggle_switch(settings.PAYMENT_PROCESSOR_SWITCH_PREFIX + self.processor_name, True)
        response = self.client.post(self.path, data=self.post_data)
        self.assertEqual(response.status_code, 200)

        basket = Basket.objects.get(id=self.basket.id)
        self.assertEqual(basket.status, Basket.FROZEN)
        response_data = response.json()
        self.assertIn(reverse('iap:iap-execute'), response_data['payment_page_url'])
        self.assertEqual(response_data['payment_processor'], self.processor_name)
