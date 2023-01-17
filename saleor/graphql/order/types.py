import logging
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

import graphene
import prices
from django.core.exceptions import ValidationError
from graphene import relay
from promise import Promise

from ...account.models import Address
from ...checkout.utils import get_external_shipping_id
from ...core.anonymize import obfuscate_address, obfuscate_email
from ...core.permissions import (
    AccountPermissions,
    AppPermission,
    AuthorizationFilters,
    OrderPermissions,
    PaymentPermissions,
    ProductPermissions,
    has_one_of_permissions,
)
from ...core.prices import quantize_price
from ...discount import OrderDiscountType
from ...graphql.checkout.types import DeliveryMethod
from ...graphql.utils import get_user_or_app_from_context
from ...graphql.warehouse.dataloaders import StockByIdLoader, WarehouseByIdLoader
from ...order import OrderStatus, calculations, models
from ...order.models import FulfillmentStatus
from ...order.utils import (
    get_order_country,
    get_valid_collection_points_for_order,
    get_valid_shipping_methods_for_order,
)
from ...payment import ChargeStatus
from ...payment.dataloaders import PaymentsByOrderIdLoader
from ...payment.model_helpers import get_last_payment, get_total_authorized
from ...product import ProductMediaTypes
from ...product.models import ALL_PRODUCTS_PERMISSIONS
from ...shipping.interface import ShippingMethodData
from ...shipping.models import ShippingMethodChannelListing
from ...shipping.utils import convert_to_shipping_method_data
from ...tax.utils import get_display_gross_prices
from ...thumbnail.utils import get_image_or_proxy_url, get_thumbnail_size
from ..account.dataloaders import AddressByIdLoader, UserByUserIdLoader
from ..account.types import User
from ..account.utils import (
    check_is_owner_or_has_one_of_perms,
    is_owner_or_has_one_of_perms,
)
from ..app.dataloaders import AppByIdLoader
from ..app.types import App
from ..channel import ChannelContext
from ..channel.dataloaders import ChannelByIdLoader, ChannelByOrderLineIdLoader
from ..channel.types import Channel
from ..checkout.utils import prevent_sync_event_circular_query
from ..core.connection import CountableConnection
from ..core.descriptions import (
    ADDED_IN_31,
    ADDED_IN_34,
    ADDED_IN_35,
    ADDED_IN_38,
    ADDED_IN_39,
    ADDED_IN_310,
    ADDED_IN_311,
    DEPRECATED_IN_3X_FIELD,
    PREVIEW_FEATURE,
)
from ..core.enums import LanguageCodeEnum
from ..core.fields import PermissionsField
from ..core.mutations import validation_error_to_error_type
from ..core.scalars import PositiveDecimal
from ..core.tracing import traced_resolver
from ..core.types import (
    Image,
    ModelObjectType,
    Money,
    NonNullList,
    OrderError,
    TaxedMoney,
    ThumbnailField,
    Weight,
)
from ..core.utils import str_to_enum
from ..decorators import one_of_permissions_required
from ..discount.dataloaders import OrderDiscountsByOrderIDLoader, VoucherByIdLoader
from ..discount.enums import DiscountValueTypeEnum
from ..discount.types import Voucher
from ..giftcard.dataloaders import GiftCardsByOrderIdLoader
from ..giftcard.types import GiftCard
from ..invoice.dataloaders import InvoicesByOrderIdLoader
from ..invoice.types import Invoice
from ..meta.resolvers import check_private_metadata_privilege, resolve_metadata
from ..meta.types import MetadataItem, ObjectWithMetadata
from ..payment.enums import OrderAction, TransactionStatusEnum
from ..payment.types import Payment, PaymentChargeStatusEnum, TransactionItem
from ..plugins.dataloaders import (
    get_plugin_manager_promise,
    plugin_manager_promise_callback,
)
from ..product.dataloaders import (
    MediaByProductVariantIdLoader,
    ProductByVariantIdLoader,
    ProductChannelListingByProductIdAndChannelSlugLoader,
    ProductImageByProductIdLoader,
    ProductVariantByIdLoader,
    ThumbnailByProductMediaIdSizeAndFormatLoader,
)
from ..product.types import DigitalContentUrl, ProductVariant
from ..shipping.dataloaders import (
    ShippingMethodByIdLoader,
    ShippingMethodChannelListingByChannelSlugLoader,
    ShippingMethodChannelListingByShippingMethodIdAndChannelSlugLoader,
)
from ..shipping.types import ShippingMethod
from ..tax.dataloaders import (
    TaxClassByIdLoader,
    TaxConfigurationByChannelId,
    TaxConfigurationPerCountryByTaxConfigurationIDLoader,
)
from ..tax.types import TaxClass
from ..warehouse.types import Allocation, Stock, Warehouse
from .dataloaders import (
    AllocationsByOrderLineIdLoader,
    FulfillmentLinesByFulfillmentIdLoader,
    FulfillmentLinesByIdLoader,
    FulfillmentsByOrderIdLoader,
    OrderByIdLoader,
    OrderEventsByOrderIdLoader,
    OrderLineByIdLoader,
    OrderLinesByOrderIdLoader,
    TransactionItemsByOrderIDLoader,
)
from .enums import (
    FulfillmentStatusEnum,
    OrderAuthorizeStatusEnum,
    OrderChargeStatusEnum,
    OrderEventsEmailsEnum,
    OrderEventsEnum,
    OrderOriginEnum,
    OrderStatusEnum,
)
from .utils import validate_draft_order

logger = logging.getLogger(__name__)


def get_order_discount_event(discount_obj: dict):
    currency = discount_obj["currency"]

    amount = prices.Money(Decimal(discount_obj["amount_value"]), currency)

    old_amount = None
    old_amount_value = discount_obj.get("old_amount_value")
    if old_amount_value:
        old_amount = prices.Money(Decimal(old_amount_value), currency)

    return OrderEventDiscountObject(
        value=discount_obj.get("value"),
        amount=amount,
        value_type=discount_obj.get("value_type"),
        reason=discount_obj.get("reason"),
        old_value_type=discount_obj.get("old_value_type"),
        old_value=discount_obj.get("old_value"),
        old_amount=old_amount,
    )


def get_payment_status_for_order(order):
    status = ChargeStatus.NOT_CHARGED
    charged_money = order.total_charged

    if charged_money >= order.total.gross:
        status = ChargeStatus.FULLY_CHARGED
    elif charged_money and charged_money < order.total.gross:
        status = ChargeStatus.PARTIALLY_CHARGED
    return status


class OrderDiscount(graphene.ObjectType):
    value_type = graphene.Field(
        DiscountValueTypeEnum,
        required=True,
        description="Type of the discount: fixed or percent.",
    )
    value = PositiveDecimal(
        required=True,
        description="Value of the discount. Can store fixed value or percent value.",
    )
    reason = graphene.String(
        required=False, description="Explanation for the applied discount."
    )
    amount = graphene.Field(Money, description="Returns amount of discount.")


class OrderEventDiscountObject(OrderDiscount):
    old_value_type = graphene.Field(
        DiscountValueTypeEnum,
        required=False,
        description="Type of the discount: fixed or percent.",
    )
    old_value = PositiveDecimal(
        required=False,
        description="Value of the discount. Can store fixed value or percent value.",
    )
    old_amount = graphene.Field(
        Money, required=False, description="Returns amount of discount."
    )


class OrderEventOrderLineObject(graphene.ObjectType):
    quantity = graphene.Int(description="The variant quantity.")
    order_line = graphene.Field(lambda: OrderLine, description="The order line.")
    item_name = graphene.String(description="The variant name.")
    discount = graphene.Field(
        OrderEventDiscountObject, description="The discount applied to the order line."
    )


class OrderEvent(ModelObjectType[models.OrderEvent]):
    id = graphene.GlobalID(required=True)
    date = graphene.types.datetime.DateTime(
        description="Date when event happened at in ISO 8601 format."
    )
    type = OrderEventsEnum(description="Order event type.")
    user = graphene.Field(User, description="User who performed the action.")
    app = graphene.Field(
        App,
        description=(
            "App that performed the action. Requires of of the following permissions: "
            f"{AppPermission.MANAGE_APPS.name}, {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    message = graphene.String(description="Content of the event.")
    email = graphene.String(description="Email of the customer.")
    email_type = OrderEventsEmailsEnum(
        description="Type of an email sent to the customer."
    )
    amount = graphene.Float(description="Amount of money.")
    payment_id = graphene.String(
        description="The payment reference from the payment provider."
    )
    payment_gateway = graphene.String(description="The payment gateway of the payment.")
    quantity = graphene.Int(description="Number of items.")
    composed_id = graphene.String(description="Composed ID of the Fulfillment.")
    order_number = graphene.String(description="User-friendly number of an order.")
    invoice_number = graphene.String(
        description="Number of an invoice related to the order."
    )
    oversold_items = NonNullList(
        graphene.String, description="List of oversold lines names."
    )
    lines = NonNullList(OrderEventOrderLineObject, description="The concerned lines.")
    fulfilled_items = NonNullList(
        lambda: FulfillmentLine, description="The lines fulfilled."
    )
    warehouse = graphene.Field(
        Warehouse, description="The warehouse were items were restocked."
    )
    transaction_reference = graphene.String(
        description="The transaction reference of captured payment."
    )
    shipping_costs_included = graphene.Boolean(
        description="Define if shipping costs were included to the refund."
    )
    related_order = graphene.Field(
        lambda: Order, description="The order which is related to this order."
    )
    discount = graphene.Field(
        OrderEventDiscountObject, description="The discount applied to the order."
    )
    status = graphene.Field(
        TransactionStatusEnum, description="The status of payment's transaction."
    )
    reference = graphene.String(description="The reference of payment's transaction.")

    class Meta:
        description = "History log of the order."
        model = models.OrderEvent
        interfaces = [relay.Node]

    @staticmethod
    def resolve_user(root: models.OrderEvent, info):
        user_or_app = get_user_or_app_from_context(info.context)
        if not user_or_app:
            return None
        requester = user_or_app

        def _resolve_user(event_user):
            if (
                requester == event_user
                or requester.has_perm(AccountPermissions.MANAGE_USERS)
                or requester.has_perm(AccountPermissions.MANAGE_STAFF)
            ):
                return event_user
            return None

        if not root.user_id:
            return None

        return UserByUserIdLoader(info.context).load(root.user_id).then(_resolve_user)

    @staticmethod
    def resolve_app(root: models.OrderEvent, info):
        requestor = get_user_or_app_from_context(info.context)
        check_is_owner_or_has_one_of_perms(
            requestor,
            root.user,
            AppPermission.MANAGE_APPS,
            OrderPermissions.MANAGE_ORDERS,
        )
        return AppByIdLoader(info.context).load(root.app_id) if root.app_id else None

    @staticmethod
    def resolve_email(root: models.OrderEvent, _info):
        return root.parameters.get("email", None)

    @staticmethod
    def resolve_email_type(root: models.OrderEvent, _info):
        return root.parameters.get("email_type", None)

    @staticmethod
    def resolve_amount(root: models.OrderEvent, _info):
        amount = root.parameters.get("amount", None)
        return float(amount) if amount else None

    @staticmethod
    def resolve_payment_id(root: models.OrderEvent, _info):
        return root.parameters.get("payment_id", None)

    @staticmethod
    def resolve_payment_gateway(root: models.OrderEvent, _info):
        return root.parameters.get("payment_gateway", None)

    @staticmethod
    def resolve_quantity(root: models.OrderEvent, _info):
        quantity = root.parameters.get("quantity", None)
        return int(quantity) if quantity else None

    @staticmethod
    def resolve_message(root: models.OrderEvent, _info):
        return root.parameters.get("message", None)

    @staticmethod
    def resolve_composed_id(root: models.OrderEvent, _info):
        return root.parameters.get("composed_id", None)

    @staticmethod
    def resolve_oversold_items(root: models.OrderEvent, _info):
        return root.parameters.get("oversold_items", None)

    @staticmethod
    def resolve_order_number(root: models.OrderEvent, info):
        def _resolve_order_number(order: models.Order):
            return order.number

        return (
            OrderByIdLoader(info.context)
            .load(root.order_id)
            .then(_resolve_order_number)
        )

    @staticmethod
    def resolve_invoice_number(root: models.OrderEvent, _info):
        return root.parameters.get("invoice_number")

    @staticmethod
    @traced_resolver
    def resolve_lines(root: models.OrderEvent, info):
        raw_lines = root.parameters.get("lines", None)

        if not raw_lines:
            return None

        line_pks = []
        for entry in raw_lines:
            line_pk = entry.get("line_pk", None)
            if line_pk:
                line_pks.append(UUID(line_pk))

        def _resolve_lines(lines):
            results = []
            lines_dict = {str(line.pk): line for line in lines if line}
            for raw_line in raw_lines:
                line_pk = raw_line.get("line_pk")
                line_object = lines_dict.get(line_pk)
                discount = raw_line.get("discount")
                if discount:
                    discount = get_order_discount_event(discount)
                results.append(
                    OrderEventOrderLineObject(
                        quantity=raw_line["quantity"],
                        order_line=line_object,
                        item_name=raw_line["item"],
                        discount=discount,
                    )
                )

            return results

        return (
            OrderLineByIdLoader(info.context).load_many(line_pks).then(_resolve_lines)
        )

    @staticmethod
    def resolve_fulfilled_items(root: models.OrderEvent, info):
        fulfillment_lines_ids = root.parameters.get("fulfilled_items", [])

        if not fulfillment_lines_ids:
            return None

        return FulfillmentLinesByIdLoader(info.context).load_many(fulfillment_lines_ids)

    @staticmethod
    def resolve_warehouse(root: models.OrderEvent, info):
        if warehouse_pk := root.parameters.get("warehouse"):
            return WarehouseByIdLoader(info.context).load(UUID(warehouse_pk))
        return None

    @staticmethod
    def resolve_transaction_reference(root: models.OrderEvent, _info):
        return root.parameters.get("transaction_reference")

    @staticmethod
    def resolve_shipping_costs_included(root: models.OrderEvent, _info):
        return root.parameters.get("shipping_costs_included")

    @staticmethod
    def resolve_related_order(root: models.OrderEvent, info):
        order_pk = root.parameters.get("related_order_pk")
        if not order_pk:
            return None
        return OrderByIdLoader(info.context).load(UUID(order_pk))

    @staticmethod
    def resolve_discount(root: models.OrderEvent, info):
        discount_obj = root.parameters.get("discount")
        if not discount_obj:
            return None
        return get_order_discount_event(discount_obj)

    @staticmethod
    def resolve_status(root: models.OrderEvent, _info):
        return root.parameters.get("status")

    @staticmethod
    def resolve_reference(root: models.OrderEvent, _info):
        return root.parameters.get("reference")


class OrderEventCountableConnection(CountableConnection):
    class Meta:
        node = OrderEvent


class FulfillmentLine(ModelObjectType[models.FulfillmentLine]):
    id = graphene.GlobalID(required=True)
    quantity = graphene.Int(required=True)
    order_line = graphene.Field(lambda: OrderLine)

    class Meta:
        description = "Represents line of the fulfillment."
        interfaces = [relay.Node]
        model = models.FulfillmentLine

    @staticmethod
    def resolve_order_line(root: models.FulfillmentLine, info):
        return OrderLineByIdLoader(info.context).load(root.order_line_id)


class Fulfillment(ModelObjectType[models.Fulfillment]):
    id = graphene.GlobalID(required=True)
    fulfillment_order = graphene.Int(required=True)
    status = FulfillmentStatusEnum(required=True)
    tracking_number = graphene.String(required=True)
    created = graphene.DateTime(required=True)
    lines = NonNullList(
        FulfillmentLine, description="List of lines for the fulfillment."
    )
    status_display = graphene.String(description="User-friendly fulfillment status.")
    warehouse = graphene.Field(
        Warehouse,
        required=False,
        description="Warehouse from fulfillment was fulfilled.",
    )

    class Meta:
        description = "Represents order fulfillment."
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Fulfillment

    @staticmethod
    def resolve_created(root: models.Fulfillment, _info):
        return root.created_at

    @staticmethod
    def resolve_lines(root: models.Fulfillment, info):
        return FulfillmentLinesByFulfillmentIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_status_display(root: models.Fulfillment, _info):
        return root.get_status_display()

    @staticmethod
    def resolve_warehouse(root: models.Fulfillment, info):
        def _resolve_stock_warehouse(stock: Stock):
            return WarehouseByIdLoader(info.context).load(stock.warehouse_id)

        def _resolve_stock(fulfillment_lines: List[models.FulfillmentLine]):
            try:
                line = fulfillment_lines[0]
            except IndexError:
                return None

            if stock_id := line.stock_id:
                return (
                    StockByIdLoader(info.context)
                    .load(stock_id)
                    .then(_resolve_stock_warehouse)
                )

        return (
            FulfillmentLinesByFulfillmentIdLoader(info.context)
            .load(root.id)
            .then(_resolve_stock)
        )


class OrderLine(ModelObjectType[models.OrderLine]):
    id = graphene.GlobalID(required=True)
    product_name = graphene.String(required=True)
    variant_name = graphene.String(required=True)
    product_sku = graphene.String()
    product_variant_id = graphene.String()
    is_shipping_required = graphene.Boolean(required=True)
    quantity = graphene.Int(required=True)
    quantity_fulfilled = graphene.Int(required=True)
    unit_discount_reason = graphene.String()
    tax_rate = graphene.Float(required=True)
    digital_content_url = graphene.Field(DigitalContentUrl)
    thumbnail = ThumbnailField()
    unit_price = graphene.Field(
        TaxedMoney,
        description="Price of the single item in the order line.",
        required=True,
    )
    undiscounted_unit_price = graphene.Field(
        TaxedMoney,
        description=(
            "Price of the single item in the order line without applied an order line "
            "discount."
        ),
        required=True,
    )
    unit_discount = graphene.Field(
        Money,
        description="The discount applied to the single order line.",
        required=True,
    )
    unit_discount_value = graphene.Field(
        PositiveDecimal,
        description="Value of the discount. Can store fixed value or percent value",
        required=True,
    )
    total_price = graphene.Field(
        TaxedMoney, description="Price of the order line.", required=True
    )
    variant = graphene.Field(
        ProductVariant,
        required=False,
        description=(
            "A purchased product variant. Note: this field may be null if the variant "
            "has been removed from stock at all. Requires one of the following "
            "permissions to include the unpublished items: "
            f"{', '.join([p.name for p in ALL_PRODUCTS_PERMISSIONS])}."
        ),
    )
    translated_product_name = graphene.String(
        required=True, description="Product name in the customer's language"
    )
    translated_variant_name = graphene.String(
        required=True, description="Variant name in the customer's language"
    )
    allocations = PermissionsField(
        NonNullList(Allocation),
        description="List of allocations across warehouses.",
        permissions=[
            ProductPermissions.MANAGE_PRODUCTS,
            OrderPermissions.MANAGE_ORDERS,
        ],
    )
    quantity_to_fulfill = graphene.Int(
        required=True,
        description="A quantity of items remaining to be fulfilled." + ADDED_IN_31,
    )
    unit_discount_type = graphene.Field(
        DiscountValueTypeEnum,
        description="Type of the discount: fixed or percent",
    )
    tax_class = PermissionsField(
        TaxClass,
        description="Denormalized tax class of the product in this order line."
        + ADDED_IN_39
        + PREVIEW_FEATURE,
        required=False,
        permissions=[AuthorizationFilters.AUTHENTICATED_STAFF_USER],
    )
    tax_class_name = graphene.Field(
        graphene.String,
        description="Denormalized name of the tax class."
        + ADDED_IN_39
        + PREVIEW_FEATURE,
        required=False,
    )
    tax_class_metadata = NonNullList(
        MetadataItem,
        required=True,
        description="Denormalized public metadata of the tax class."
        + ADDED_IN_39
        + PREVIEW_FEATURE,
    )
    tax_class_private_metadata = NonNullList(
        MetadataItem,
        required=True,
        description=(
            "Denormalized private metadata of the tax class. Requires staff "
            "permissions to access." + ADDED_IN_39 + PREVIEW_FEATURE
        ),
    )

    class Meta:
        description = "Represents order line of particular order."
        model = models.OrderLine
        interfaces = [relay.Node, ObjectWithMetadata]
        metadata_since = ADDED_IN_35

    @staticmethod
    @traced_resolver
    def resolve_thumbnail(root: models.OrderLine, info, *, size=256, format=None):
        if not root.variant_id:
            return None

        format = format.lower() if format else None
        size = get_thumbnail_size(size)

        def _get_image_from_media(image):
            def _resolve_url(thumbnail):
                url = get_image_or_proxy_url(
                    thumbnail, image.id, "ProductMedia", size, format
                )
                return Image(alt=image.alt, url=url)

            return (
                ThumbnailByProductMediaIdSizeAndFormatLoader(info.context)
                .load((image.id, size, format))
                .then(_resolve_url)
            )

        def _get_first_variant_image(all_medias):
            if image := next(
                (
                    media
                    for media in all_medias
                    if media.type == ProductMediaTypes.IMAGE
                ),
                None,
            ):
                return image

        def _get_first_product_image(images):
            return _get_image_from_media(images[0]) if images else None

        def _resolve_thumbnail(result):
            product, variant_medias = result

            if image := _get_first_variant_image(variant_medias):
                return _get_image_from_media(image)

            # we failed to get image from variant, lets use first from product
            return (
                ProductImageByProductIdLoader(info.context)
                .load(product.id)
                .then(_get_first_product_image)
            )

        variants_product = ProductByVariantIdLoader(info.context).load(root.variant_id)
        variant_medias = MediaByProductVariantIdLoader(info.context).load(
            root.variant_id
        )
        return Promise.all([variants_product, variant_medias]).then(_resolve_thumbnail)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_unit_price(root: models.OrderLine, info):
        def _resolve_unit_price(data):
            order, lines, manager = data
            return calculations.order_line_unit(
                order, root, manager, lines
            ).price_with_discounts

        order = OrderByIdLoader(info.context).load(root.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(root.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_unit_price)

    @staticmethod
    def resolve_quantity_to_fulfill(root: models.OrderLine, info):
        return root.quantity_unfulfilled

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_unit_price(root: models.OrderLine, info):
        def _resolve_undiscounted_unit_price(data):
            order, lines, manager = data
            return calculations.order_line_unit(
                order, root, manager, lines
            ).undiscounted_price

        order = OrderByIdLoader(info.context).load(root.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(root.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(
            _resolve_undiscounted_unit_price
        )

    @staticmethod
    def resolve_unit_discount_type(root: models.OrderLine, _info):
        return root.unit_discount_type

    @staticmethod
    def resolve_unit_discount_value(root: models.OrderLine, _info):
        return root.unit_discount_value

    @staticmethod
    def resolve_unit_discount(root: models.OrderLine, _info):
        return root.unit_discount

    @staticmethod
    @traced_resolver
    def resolve_tax_rate(root: models.OrderLine, info):
        def _resolve_tax_rate(data):
            order, lines, manager = data
            return calculations.order_line_tax_rate(
                order, root, manager, lines
            ) or Decimal(0)

        order = OrderByIdLoader(info.context).load(root.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(root.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_tax_rate)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_total_price(root: models.OrderLine, info):
        def _resolve_total_price(data):
            order, lines, manager = data
            return calculations.order_line_total(
                order, root, manager, lines
            ).price_with_discounts

        order = OrderByIdLoader(info.context).load(root.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(root.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_total_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_total_price(root: models.OrderLine, info):
        def _resolve_undiscounted_total_price(data):
            order, lines, manager = data
            return calculations.order_line_total(
                order, root, manager, lines
            ).undiscounted_price

        order = OrderByIdLoader(info.context).load(root.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(root.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(
            _resolve_undiscounted_total_price
        )

    @staticmethod
    def resolve_translated_product_name(root: models.OrderLine, _info):
        return root.translated_product_name

    @staticmethod
    def resolve_translated_variant_name(root: models.OrderLine, _info):
        return root.translated_variant_name

    @staticmethod
    @traced_resolver
    def resolve_variant(root: models.OrderLine, info):
        context = info.context
        if not root.variant_id:
            return None

        def requestor_has_access_to_variant(data):
            variant, channel = data

            requester = get_user_or_app_from_context(context)
            has_required_permission = has_one_of_permissions(
                requester, ALL_PRODUCTS_PERMISSIONS
            )
            if has_required_permission:
                return ChannelContext(node=variant, channel_slug=channel.slug)

            def product_is_available(product_channel_listing):
                if product_channel_listing and product_channel_listing.is_visible:
                    return ChannelContext(node=variant, channel_slug=channel.slug)
                return None

            return (
                ProductChannelListingByProductIdAndChannelSlugLoader(context)
                .load((variant.product_id, channel.slug))
                .then(product_is_available)
            )

        variant = ProductVariantByIdLoader(context).load(root.variant_id)
        channel = ChannelByOrderLineIdLoader(context).load(root.id)

        return Promise.all([variant, channel]).then(requestor_has_access_to_variant)

    @staticmethod
    def resolve_allocations(root: models.OrderLine, info):
        return AllocationsByOrderLineIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_tax_class(root: models.OrderLine, info):
        return (
            TaxClassByIdLoader(info.context).load(root.tax_class_id)
            if root.tax_class_id
            else None
        )

    @staticmethod
    def resolve_tax_class_metadata(root: models.OrderLine, _info):
        return resolve_metadata(root.tax_class_metadata)

    @staticmethod
    def resolve_tax_class_private_metadata(root: models.OrderLine, info):
        check_private_metadata_privilege(root, info)
        return resolve_metadata(root.tax_class_private_metadata)


class Order(ModelObjectType[models.Order]):
    id = graphene.GlobalID(required=True)
    created = graphene.DateTime(required=True)
    updated_at = graphene.DateTime(required=True)
    status = OrderStatusEnum(required=True)
    user = graphene.Field(
        User,
        description=(
            "User who placed the order. This field is set only for orders placed by "
            "authenticated users. Can be fetched for orders created in Saleor 3.2 "
            "and later, for other orders requires one of the following permissions: "
            f"{AccountPermissions.MANAGE_USERS.name}, "
            f"{OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    tracking_client_id = graphene.String(required=True)
    billing_address = graphene.Field(
        "saleor.graphql.account.types.Address",
        description=(
            "Billing address. The full data can be access for orders created "
            "in Saleor 3.2 and later, for other orders requires one of the following "
            f"permissions: {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    shipping_address = graphene.Field(
        "saleor.graphql.account.types.Address",
        description=(
            "Shipping address. The full data can be access for orders created "
            "in Saleor 3.2 and later, for other orders requires one of the following "
            f"permissions: {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    shipping_method_name = graphene.String()
    collection_point_name = graphene.String()
    channel = graphene.Field(Channel, required=True)
    fulfillments = NonNullList(
        Fulfillment, required=True, description="List of shipments for the order."
    )
    lines = NonNullList(
        lambda: OrderLine, required=True, description="List of order lines."
    )
    actions = NonNullList(
        OrderAction,
        description=(
            "List of actions that can be performed in the current state of an order."
        ),
        required=True,
    )
    available_shipping_methods = NonNullList(
        ShippingMethod,
        description="Shipping methods that can be used with this order.",
        required=False,
        deprecation_reason="Use `shippingMethods`, this field will be removed in 4.0",
    )
    shipping_methods = NonNullList(
        ShippingMethod,
        description="Shipping methods related to this order.",
        required=True,
    )
    available_collection_points = NonNullList(
        Warehouse,
        description=(
            "Collection points that can be used for this order."
            + ADDED_IN_31
            + PREVIEW_FEATURE
        ),
        required=True,
    )
    invoices = NonNullList(
        Invoice,
        description=(
            "List of order invoices. Can be fetched for orders created in Saleor 3.2 "
            "and later, for other orders requires one of the following permissions: "
            f"{OrderPermissions.MANAGE_ORDERS.name}, {AuthorizationFilters.OWNER.name}."
        ),
        required=True,
    )
    number = graphene.String(
        description="User-friendly number of an order.", required=True
    )
    original = graphene.ID(
        description="The ID of the order that was the base for this order."
    )
    origin = OrderOriginEnum(description="The order origin.", required=True)
    is_paid = graphene.Boolean(
        description="Informs if an order is fully paid.", required=True
    )
    payment_status = PaymentChargeStatusEnum(
        description="Internal payment status.", required=True
    )
    payment_status_display = graphene.String(
        description="User-friendly payment status.", required=True
    )
    authorize_status = OrderAuthorizeStatusEnum(
        description=(
            "The authorize status of the order." + ADDED_IN_34 + PREVIEW_FEATURE
        ),
        required=True,
    )
    charge_status = OrderChargeStatusEnum(
        description=("The charge status of the order." + ADDED_IN_34 + PREVIEW_FEATURE),
        required=True,
    )
    tax_exemption = graphene.Boolean(
        description=(
            "Returns True if order has to be exempt from taxes."
            + ADDED_IN_38
            + PREVIEW_FEATURE
        ),
        required=True,
    )
    transactions = NonNullList(
        TransactionItem,
        description=(
            "List of transactions for the order. Requires one of the "
            "following permissions: MANAGE_ORDERS, HANDLE_PAYMENTS."
            + ADDED_IN_34
            + PREVIEW_FEATURE
        ),
        required=True,
    )
    payments = NonNullList(
        Payment, description="List of payments for the order.", required=True
    )
    total = graphene.Field(
        TaxedMoney, description="Total amount of the order.", required=True
    )
    undiscounted_total = graphene.Field(
        TaxedMoney, description="Undiscounted total amount of the order.", required=True
    )
    shipping_price = graphene.Field(
        TaxedMoney, description="Total price of shipping.", required=True
    )
    shipping_method = graphene.Field(
        ShippingMethod,
        description="Shipping method for this order.",
        deprecation_reason=(f"{DEPRECATED_IN_3X_FIELD} Use `deliveryMethod` instead."),
    )
    shipping_price = graphene.Field(
        TaxedMoney, description="Total price of shipping.", required=True
    )
    shipping_tax_rate = graphene.Float(
        required=True, description="The shipping tax rate value."
    )
    shipping_tax_class = PermissionsField(
        TaxClass,
        description="Denormalized tax class assigned to the shipping method."
        + ADDED_IN_39
        + PREVIEW_FEATURE,
        required=False,
        permissions=[AuthorizationFilters.AUTHENTICATED_STAFF_USER],
    )
    shipping_tax_class_name = graphene.Field(
        graphene.String,
        description=(
            "Denormalized name of the tax class assigned to the shipping method."
            + ADDED_IN_39
            + PREVIEW_FEATURE
        ),
        required=False,
    )
    shipping_tax_class_metadata = NonNullList(
        MetadataItem,
        required=True,
        description=(
            "Denormalized public metadata of the shipping method's tax class."
            + ADDED_IN_39
            + PREVIEW_FEATURE
        ),
    )
    shipping_tax_class_private_metadata = NonNullList(
        MetadataItem,
        required=True,
        description=(
            "Denormalized private metadata of the shipping method's tax class. "
            "Requires staff permissions to access." + ADDED_IN_39 + PREVIEW_FEATURE
        ),
    )
    token = graphene.String(
        required=True,
        deprecation_reason=(f"{DEPRECATED_IN_3X_FIELD} Use `id` instead."),
    )
    voucher = graphene.Field(Voucher)
    gift_cards = NonNullList(
        GiftCard, description="List of user gift cards.", required=True
    )
    customerNote = graphene.Boolean(required=True)
    customer_note = graphene.String(required=True)
    weight = graphene.Field(Weight, required=True)
    redirect_url = graphene.String()
    subtotal = graphene.Field(
        TaxedMoney,
        description="The sum of line prices not including shipping.",
        required=True,
    )
    status_display = graphene.String(
        description="User-friendly order status.", required=True
    )
    can_finalize = graphene.Boolean(
        description=(
            "Informs whether a draft order can be finalized"
            "(turned into a regular order)."
        ),
        required=True,
    )
    total_authorized = graphene.Field(
        Money, description="Amount authorized for the order.", required=True
    )
    total_captured = graphene.Field(
        Money, description="Amount captured by payment.", required=True
    )
    events = PermissionsField(
        NonNullList(OrderEvent),
        description="List of events associated with the order.",
        permissions=[OrderPermissions.MANAGE_ORDERS],
        required=True,
    )
    total_balance = graphene.Field(
        Money,
        description="The difference between the paid and the order total amount.",
        required=True,
    )
    user_email = graphene.String(
        description=(
            "Email address of the customer. The full data can be access for orders "
            "created in Saleor 3.2 and later, for other orders requires one of "
            f"the following permissions: {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
        required=False,
    )
    is_shipping_required = graphene.Boolean(
        description="Returns True, if order requires shipping.", required=True
    )
    delivery_method = graphene.Field(
        DeliveryMethod,
        description=(
            "The delivery method selected for this order."
            + ADDED_IN_31
            + PREVIEW_FEATURE
        ),
    )
    language_code = graphene.String(
        deprecation_reason=(
            f"{DEPRECATED_IN_3X_FIELD} "
            "Use the `languageCodeEnum` field to fetch the language code. "
        ),
        required=True,
    )
    language_code_enum = graphene.Field(
        LanguageCodeEnum, description="Order language code.", required=True
    )
    discount = graphene.Field(
        Money,
        description="Returns applied discount.",
        deprecation_reason=(
            f"{DEPRECATED_IN_3X_FIELD} Use the `discounts` field instead."
        ),
    )
    discount_name = graphene.String(
        description="Discount name.",
        deprecation_reason=(
            f"{DEPRECATED_IN_3X_FIELD} Use the `discounts` field instead."
        ),
    )
    translated_discount_name = graphene.String(
        description="Translated discount name.",
        deprecation_reason=(
            f"{DEPRECATED_IN_3X_FIELD} Use the `discounts` field instead. "
        ),
    )
    discounts = NonNullList(
        "saleor.graphql.discount.types.OrderDiscount",
        description="List of all discounts assigned to the order.",
        required=True,
    )
    errors = NonNullList(
        OrderError,
        description="List of errors that occurred during order validation.",
        default_value=[],
        required=True,
    )
    display_gross_prices = graphene.Boolean(
        description=(
            "Determines whether checkout prices should include taxes when displayed "
            "in a storefront." + ADDED_IN_39 + PREVIEW_FEATURE
        ),
        required=True,
    )
    external_reference = graphene.String(
        description=f"External ID of this order. {ADDED_IN_310}", required=False
    )
    checkout_id = graphene.ID(
        description=(
            f"ID of the checkout that the order was created from. {ADDED_IN_311}"
        ),
        required=False,
    )

    class Meta:
        description = "Represents an order in the shop."
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Order

    @staticmethod
    def resolve_created(root: models.Order, _info):
        return root.created_at

    @staticmethod
    def resolve_token(root: models.Order, info):
        return root.id

    @staticmethod
    def resolve_discounts(root: models.Order, info):
        return OrderDiscountsByOrderIDLoader(info.context).load(root.id)

    @staticmethod
    @traced_resolver
    def resolve_discount(root: models.Order, info):
        def return_voucher_discount(discounts) -> Optional[Money]:
            if not discounts:
                return None
            for discount in discounts:
                if discount.type == OrderDiscountType.VOUCHER:
                    return Money(amount=discount.value, currency=discount.currency)
            return None

        return (
            OrderDiscountsByOrderIDLoader(info.context)
            .load(root.id)
            .then(return_voucher_discount)
        )

    @staticmethod
    @traced_resolver
    def resolve_discount_name(root: models.Order, info):
        def return_voucher_name(discounts) -> Optional[Money]:
            if not discounts:
                return None
            for discount in discounts:
                if discount.type == OrderDiscountType.VOUCHER:
                    return discount.name
            return None

        return (
            OrderDiscountsByOrderIDLoader(info.context)
            .load(root.id)
            .then(return_voucher_name)
        )

    @staticmethod
    @traced_resolver
    def resolve_translated_discount_name(root: models.Order, info):
        def return_voucher_translated_name(discounts) -> Optional[Money]:
            if not discounts:
                return None
            for discount in discounts:
                if discount.type == OrderDiscountType.VOUCHER:
                    return discount.translated_name
            return None

        return (
            OrderDiscountsByOrderIDLoader(info.context)
            .load(root.id)
            .then(return_voucher_translated_name)
        )

    @staticmethod
    @traced_resolver
    def resolve_billing_address(root: models.Order, info):
        def _resolve_billing_address(data):
            if isinstance(data, Address):
                user = None
                address = data
            else:
                user, address = data

            requester = get_user_or_app_from_context(info.context)
            if root.use_old_id is False or is_owner_or_has_one_of_perms(
                requester, user, OrderPermissions.MANAGE_ORDERS
            ):
                return address
            return obfuscate_address(address)

        if not root.billing_address_id:
            return

        if root.user_id:
            user = UserByUserIdLoader(info.context).load(root.user_id)
            address = AddressByIdLoader(info.context).load(root.billing_address_id)
            return Promise.all([user, address]).then(_resolve_billing_address)
        return (
            AddressByIdLoader(info.context)
            .load(root.billing_address_id)
            .then(_resolve_billing_address)
        )

    @staticmethod
    @traced_resolver
    def resolve_shipping_address(root: models.Order, info):
        def _resolve_shipping_address(data):
            if isinstance(data, Address):
                user = None
                address = data
            else:
                user, address = data
            requester = get_user_or_app_from_context(info.context)
            if root.use_old_id is False or is_owner_or_has_one_of_perms(
                requester, user, OrderPermissions.MANAGE_ORDERS
            ):
                return address
            return obfuscate_address(address)

        if not root.shipping_address_id:
            return

        if root.user_id:
            user = UserByUserIdLoader(info.context).load(root.user_id)
            address = AddressByIdLoader(info.context).load(root.shipping_address_id)
            return Promise.all([user, address]).then(_resolve_shipping_address)
        return (
            AddressByIdLoader(info.context)
            .load(root.shipping_address_id)
            .then(_resolve_shipping_address)
        )

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_shipping_price(root: models.Order, info):
        def _resolve_shipping_price(data):
            lines, manager = data
            return calculations.order_shipping(root, manager, lines)

        lines = OrderLinesByOrderIdLoader(info.context).load(root.id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_shipping_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_shipping_tax_rate(root: models.Order, info):
        def _resolve_shipping_tax_rate(data):
            lines, manager = data
            return calculations.order_shipping_tax_rate(
                root, manager, lines
            ) or Decimal(0)

        lines = OrderLinesByOrderIdLoader(info.context)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_shipping_tax_rate)

    @staticmethod
    def resolve_actions(root: models.Order, info):
        def _resolve_actions(payments):
            actions = []
            payment = get_last_payment(payments)
            if root.can_capture(payment):
                actions.append(OrderAction.CAPTURE)
            if root.can_mark_as_paid(payments):
                actions.append(OrderAction.MARK_AS_PAID)
            if root.can_refund(payment):
                actions.append(OrderAction.REFUND)
            if root.can_void(payment):
                actions.append(OrderAction.VOID)
            return actions

        return (
            PaymentsByOrderIdLoader(info.context).load(root.id).then(_resolve_actions)
        )

    @staticmethod
    @traced_resolver
    def resolve_subtotal(root: models.Order, info):
        def _resolve_subtotal(data):
            order_lines, manager = data
            return calculations.order_subtotal(root, manager, order_lines)

        order_lines = OrderLinesByOrderIdLoader(info.context).load(root.id)
        manager = get_plugin_manager_promise(info.context)

        return Promise.all([order_lines, manager]).then(_resolve_subtotal)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    @plugin_manager_promise_callback
    def resolve_total(root: models.Order, info, manager):
        def _resolve_total(lines):
            return calculations.order_total(root, manager, lines)

        return (
            OrderLinesByOrderIdLoader(info.context).load(root.id).then(_resolve_total)
        )

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_total(root: models.Order, info):
        def _resolve_undiscounted_total(lines_and_manager):
            lines, manager = lines_and_manager
            return calculations.order_undiscounted_total(root, manager, lines)

        lines = OrderLinesByOrderIdLoader(info.context).load(root.id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_undiscounted_total)

    @staticmethod
    def resolve_total_authorized(root: models.Order, info):
        def _resolve_total_get_total_authorized(data):
            transactions, payments = data
            if transactions:
                authorized_money = prices.Money(Decimal(0), root.currency)
                for transaction in transactions:
                    authorized_money += transaction.amount_authorized
                return quantize_price(authorized_money, root.currency)
            return get_total_authorized(payments, root.currency)

        transactions = TransactionItemsByOrderIDLoader(info.context).load(root.id)
        payments = PaymentsByOrderIdLoader(info.context).load(root.id)
        return Promise.all([transactions, payments]).then(
            _resolve_total_get_total_authorized
        )

    @staticmethod
    def resolve_total_captured(root: models.Order, info):
        def _resolve_total_captured(transactions):
            if transactions:
                charged_money = prices.Money(Decimal(0), root.currency)
                for transaction in transactions:
                    charged_money += transaction.amount_charged
                return quantize_price(charged_money, root.currency)
            return root.total_charged

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(root.id)
            .then(_resolve_total_captured)
        )

    @staticmethod
    def resolve_total_balance(root: models.Order, info):
        def _resolve_total_balance(transactions):
            if transactions:
                charged_money = prices.Money(Decimal(0), root.currency)
                for transaction in transactions:
                    charged_money += transaction.amount_charged
                return quantize_price(charged_money - root.total.gross, root.currency)
            return root.total_balance

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(root.id)
            .then(_resolve_total_balance)
        )

    @staticmethod
    def resolve_fulfillments(root: models.Order, info):
        def _resolve_fulfillments(fulfillments):
            user = info.context.user
            if user and user.is_staff:
                return fulfillments
            return filter(
                lambda fulfillment: fulfillment.status != FulfillmentStatus.CANCELED,
                fulfillments,
            )

        return (
            FulfillmentsByOrderIdLoader(info.context)
            .load(root.id)
            .then(_resolve_fulfillments)
        )

    @staticmethod
    def resolve_lines(root: models.Order, info):
        return OrderLinesByOrderIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_events(root: models.Order, _info):
        return OrderEventsByOrderIdLoader(_info.context).load(root.id)

    @staticmethod
    def resolve_is_paid(root: models.Order, info):
        def _resolve_is_paid(transactions):
            if transactions:
                charged_money = prices.Money(Decimal(0), root.currency)
                for transaction in transactions:
                    charged_money += transaction.amount_charged
                return charged_money >= root.total.gross
            return root.is_fully_paid()

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(root.id)
            .then(_resolve_is_paid)
        )

    @staticmethod
    def resolve_number(root: models.Order, _info):
        return str(root.number)

    @staticmethod
    @traced_resolver
    def resolve_payment_status(root: models.Order, info):
        def _resolve_payment_status(data):
            transactions, payments, fulfillments = data

            total_fulfillment_refund = sum(
                [
                    fulfillment.total_refund_amount
                    for fulfillment in fulfillments
                    if fulfillment.total_refund_amount
                ]
            )
            if (
                total_fulfillment_refund != 0
                and total_fulfillment_refund == root.total.gross.amount
            ):
                return ChargeStatus.FULLY_REFUNDED

            if transactions:
                return get_payment_status_for_order(root)
            last_payment = get_last_payment(payments)
            if not last_payment:
                if root.total.gross.amount == 0:
                    return ChargeStatus.FULLY_CHARGED
                return ChargeStatus.NOT_CHARGED
            return last_payment.charge_status

        transactions = TransactionItemsByOrderIDLoader(info.context).load(root.id)
        payments = PaymentsByOrderIdLoader(info.context).load(root.id)
        fulfillments = FulfillmentsByOrderIdLoader(info.context).load(root.id)
        return Promise.all([transactions, payments, fulfillments]).then(
            _resolve_payment_status
        )

    @staticmethod
    def resolve_authorize_status(root: models.Order, info):
        total_covered = quantize_price(
            root.total_authorized_amount + root.total_charged_amount, root.currency
        )
        total_gross = quantize_price(root.total_gross_amount, root.currency)
        if total_covered == 0:
            return OrderAuthorizeStatusEnum.NONE
        if total_covered >= total_gross:
            return OrderAuthorizeStatusEnum.FULL
        return OrderAuthorizeStatusEnum.PARTIAL

    @staticmethod
    def resolve_charge_status(root: models.Order, info):
        total_charged = quantize_price(root.total_charged_amount, root.currency)
        total_gross = quantize_price(root.total_gross_amount, root.currency)
        if total_charged <= 0:
            return OrderChargeStatusEnum.NONE
        if total_charged < total_gross:
            return OrderChargeStatusEnum.PARTIAL
        if total_charged == total_gross:
            return OrderChargeStatusEnum.FULL
        return OrderChargeStatusEnum.OVERCHARGED

    @staticmethod
    def resolve_payment_status_display(root: models.Order, info):
        def _resolve_payment_status(data):
            transactions, payments = data
            if transactions:
                status = get_payment_status_for_order(root)
                return dict(ChargeStatus.CHOICES).get(status)
            last_payment = get_last_payment(payments)
            if not last_payment:
                if root.total.gross.amount == 0:
                    return dict(ChargeStatus.CHOICES).get(ChargeStatus.FULLY_CHARGED)
                return dict(ChargeStatus.CHOICES).get(ChargeStatus.NOT_CHARGED)
            return last_payment.get_charge_status_display()

        transactions = TransactionItemsByOrderIDLoader(info.context).load(root.id)
        payments = PaymentsByOrderIdLoader(info.context).load(root.id)
        return Promise.all([transactions, payments]).then(_resolve_payment_status)

    @staticmethod
    def resolve_payments(root: models.Order, info):
        return PaymentsByOrderIdLoader(info.context).load(root.id)

    @staticmethod
    @one_of_permissions_required(
        [OrderPermissions.MANAGE_ORDERS, PaymentPermissions.HANDLE_PAYMENTS]
    )
    def resolve_transactions(root: models.Order, info):
        return TransactionItemsByOrderIDLoader(info.context).load(root.id)

    @staticmethod
    def resolve_status_display(root: models.Order, _info):
        return root.get_status_display()

    @staticmethod
    @traced_resolver
    def resolve_can_finalize(root: models.Order, info):
        if root.status == OrderStatus.DRAFT:

            def _validate_draft_order(manager):
                country = get_order_country(root)
                try:
                    validate_draft_order(root, country, manager)
                except ValidationError:
                    return False
                return True

            return get_plugin_manager_promise(info.context).then(_validate_draft_order)
        return True

    @staticmethod
    def resolve_user_email(root: models.Order, info):
        def _resolve_user_email(user):
            requester = get_user_or_app_from_context(info.context)
            if root.use_old_id is False or is_owner_or_has_one_of_perms(
                requester, user, OrderPermissions.MANAGE_ORDERS
            ):
                return user.email if user else root.user_email
            return obfuscate_email(user.email if user else root.user_email)

        if not root.user_id:
            return _resolve_user_email(None)

        return (
            UserByUserIdLoader(info.context)
            .load(root.user_id)
            .then(_resolve_user_email)
        )

    @staticmethod
    def resolve_user(root: models.Order, info):
        def _resolve_user(user):
            requester = get_user_or_app_from_context(info.context)
            check_is_owner_or_has_one_of_perms(
                requester,
                user,
                AccountPermissions.MANAGE_USERS,
                OrderPermissions.MANAGE_ORDERS,
            )
            return user

        if not root.user_id:
            return None

        return UserByUserIdLoader(info.context).load(root.user_id).then(_resolve_user)

    @staticmethod
    def resolve_shipping_method(root: models.Order, info):
        external_app_shipping_id = get_external_shipping_id(root)

        if external_app_shipping_id:
            tax_config = TaxConfigurationByChannelId(info.context).load(root.channel_id)

            def with_tax_config(tax_config):
                prices_entered_with_tax = tax_config.prices_entered_with_tax
                price = (
                    root.shipping_price_gross
                    if prices_entered_with_tax
                    else root.shipping_price_net
                )
                return ShippingMethodData(
                    id=external_app_shipping_id,
                    name=root.shipping_method_name,
                    price=price,
                )

            return tax_config.then(with_tax_config)

        if not root.shipping_method_id:
            return None

        def wrap_shipping_method_with_channel_context(data):
            shipping_method, channel = data
            listing = (
                ShippingMethodChannelListingByShippingMethodIdAndChannelSlugLoader(
                    info.context
                ).load((shipping_method.id, channel.slug))
            )

            def calculate_price(
                listing: Optional[ShippingMethodChannelListing],
            ) -> Optional[ShippingMethodData]:
                if not listing:
                    return None
                return convert_to_shipping_method_data(shipping_method, listing)

            return listing.then(calculate_price)

        shipping_method = ShippingMethodByIdLoader(info.context).load(
            int(root.shipping_method_id)
        )
        channel = ChannelByIdLoader(info.context).load(root.channel_id)

        return Promise.all([shipping_method, channel]).then(
            wrap_shipping_method_with_channel_context
        )

    @classmethod
    def resolve_delivery_method(cls, root: models.Order, info):
        if root.shipping_method_id or get_external_shipping_id(root):
            return cls.resolve_shipping_method(root, info)
        if root.collection_point_id:
            collection_point = WarehouseByIdLoader(info.context).load(
                root.collection_point_id
            )
            return collection_point
        return None

    @classmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    # TODO: We should optimize it in/after PR#5819
    def resolve_shipping_methods(cls, root: models.Order, info):
        def with_channel(data):
            channel, manager = data

            def with_listings(channel_listings):
                return get_valid_shipping_methods_for_order(
                    root, channel_listings, manager
                )

            return (
                ShippingMethodChannelListingByChannelSlugLoader(info.context)
                .load(channel.slug)
                .then(with_listings)
            )

        channel = ChannelByIdLoader(info.context).load(root.channel_id)
        manager = get_plugin_manager_promise(info.context)

        return Promise.all([channel, manager]).then(with_channel)

    @classmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    # TODO: We should optimize it in/after PR#5819
    def resolve_available_shipping_methods(cls, root: models.Order, info):
        return cls.resolve_shipping_methods(root, info).then(
            lambda methods: [method for method in methods if method.active]
        )

    @classmethod
    @traced_resolver
    def resolve_available_collection_points(cls, root: models.Order, info):
        def get_available_collection_points(lines):
            return get_valid_collection_points_for_order(lines, root.channel_id)

        return cls.resolve_lines(root, info).then(get_available_collection_points)

    @staticmethod
    def resolve_invoices(root: models.Order, info):
        requester = get_user_or_app_from_context(info.context)
        if root.use_old_id is True:
            check_is_owner_or_has_one_of_perms(
                requester, root.user, OrderPermissions.MANAGE_ORDERS
            )
        return InvoicesByOrderIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_is_shipping_required(root: models.Order, _info):
        return root.is_shipping_required()

    @staticmethod
    def resolve_gift_cards(root: models.Order, info):
        return GiftCardsByOrderIdLoader(info.context).load(root.id)

    @staticmethod
    def resolve_voucher(root: models.Order, info):
        if not root.voucher_id:
            return None

        def wrap_voucher_with_channel_context(data):
            voucher, channel = data
            return ChannelContext(node=voucher, channel_slug=channel.slug)

        voucher = VoucherByIdLoader(info.context).load(root.voucher_id)
        channel = ChannelByIdLoader(info.context).load(root.channel_id)

        return Promise.all([voucher, channel]).then(wrap_voucher_with_channel_context)

    @staticmethod
    def resolve_language_code_enum(root: models.Order, _info):
        return LanguageCodeEnum[str_to_enum(root.language_code)]

    @staticmethod
    def resolve_original(root: models.Order, _info):
        if not root.original_id:
            return None
        return graphene.Node.to_global_id("Order", root.original_id)

    @staticmethod
    @traced_resolver
    def resolve_errors(root: models.Order, info):
        if root.status == OrderStatus.DRAFT:

            def _validate_order(manager):
                country = get_order_country(root)
                try:
                    validate_draft_order(root, country, manager)
                except ValidationError as e:
                    return validation_error_to_error_type(e, OrderError)
                return []

            return get_plugin_manager_promise(info.context).then(_validate_order)

        return []

    @staticmethod
    def resolve_display_gross_prices(root: models.Order, info):
        tax_config = TaxConfigurationByChannelId(info.context).load(root.channel_id)
        country_code = get_order_country(root)

        def load_tax_country_exceptions(tax_config):
            tax_configs_per_country = (
                TaxConfigurationPerCountryByTaxConfigurationIDLoader(info.context).load(
                    tax_config.id
                )
            )

            def calculate_display_gross_prices(tax_configs_per_country):
                tax_config_country = next(
                    (
                        tc
                        for tc in tax_configs_per_country
                        if tc.country.code == country_code
                    ),
                    None,
                )
                return get_display_gross_prices(tax_config, tax_config_country)

            return tax_configs_per_country.then(calculate_display_gross_prices)

        return tax_config.then(load_tax_country_exceptions)

    @classmethod
    def resolve_shipping_tax_class(cls, root: models.Order, info):
        if root.shipping_method_id:
            return cls.resolve_shipping_method(root, info).then(
                lambda shipping_method_data: shipping_method_data.tax_class
                if shipping_method_data
                else None
            )
        return None

    @staticmethod
    def resolve_shipping_tax_class_metadata(root: models.Order, _info):
        return resolve_metadata(root.shipping_tax_class_metadata)

    @staticmethod
    def resolve_shipping_tax_class_private_metadata(root: models.Order, info):
        check_private_metadata_privilege(root, info)
        return resolve_metadata(root.shipping_tax_class_private_metadata)

    @staticmethod
    def resolve_checkout_id(root: models.Order, _info):
        if root.checkout_token:
            return graphene.Node.to_global_id("Checkout", root.checkout_token)
        return None


class OrderCountableConnection(CountableConnection):
    class Meta:
        node = Order
