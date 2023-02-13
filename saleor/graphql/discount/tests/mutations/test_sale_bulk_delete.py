import json
from unittest import mock

import graphene
import pytest

from .....discount.models import Sale, SaleChannelListing
from .....discount.utils import fetch_catalogue_info
from .....webhook.payloads import generate_sale_payload
from ....tests.utils import get_graphql_content
from ...mutations.utils import convert_catalogue_info_to_global_ids


@pytest.fixture
def sale_list(channel_USD):
    sales = Sale.objects.bulk_create(
        [Sale(name="Sale 1"), Sale(name="Sale 2"), Sale(name="Sale 3")]
    )
    SaleChannelListing.objects.bulk_create(
        [
            SaleChannelListing(sale=sale, discount_value=5, channel=channel_USD)
            for sale in sales
        ]
    )
    return list(sales)


SALE_BULK_DELETE_MUTATION = """
    mutation saleBulkDelete($ids: [ID!]!) {
        saleBulkDelete(ids: $ids) {
            count
        }
    }
    """


def test_delete_sales(staff_api_client, sale_list, permission_manage_discounts):
    variables = {
        "ids": [graphene.Node.to_global_id("Sale", sale.id) for sale in sale_list]
    }
    response = staff_api_client.post_graphql(
        SALE_BULK_DELETE_MUTATION, variables, permissions=[permission_manage_discounts]
    )
    content = get_graphql_content(response)

    assert content["data"]["saleBulkDelete"]["count"] == 3
    assert not Sale.objects.filter(id__in=[sale.id for sale in sale_list]).exists()


@mock.patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@mock.patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_delete_sales_triggers_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    staff_api_client,
    sale_list,
    permission_manage_discounts,
    any_webhook,
    settings,
):
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]
    variables = {
        "ids": [graphene.Node.to_global_id("Sale", sale.id) for sale in sale_list]
    }
    response = staff_api_client.post_graphql(
        SALE_BULK_DELETE_MUTATION, variables, permissions=[permission_manage_discounts]
    )
    content = get_graphql_content(response)

    assert content["data"]["saleBulkDelete"]["count"] == 3
    assert mocked_webhook_trigger.call_count == 3


@mock.patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@mock.patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_delete_sales_with_variants_triggers_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    staff_api_client,
    sale_list,
    permission_manage_discounts,
    any_webhook,
    settings,
    product,
    collection,
    category,
    product_variant_list,
):
    # given
    for sale in sale_list:
        sale.products.add(product)
        sale.collections.add(collection)
        sale.categories.add(category)
        sale.variants.add(*product_variant_list)

    # create list of payloads that should be called in mutation
    payloads_per_sale = []
    for sale in sale_list:
        payloads_per_sale.append(
            generate_sale_payload(
                sale,
                convert_catalogue_info_to_global_ids(fetch_catalogue_info(sale)),
                requestor=staff_api_client.user,
            )
        )

    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]
    variables = {
        "ids": [graphene.Node.to_global_id("Sale", sale.id) for sale in sale_list]
    }
    # when
    response = staff_api_client.post_graphql(
        SALE_BULK_DELETE_MUTATION, variables, permissions=[permission_manage_discounts]
    )
    content = get_graphql_content(response)
    # create a list of payloads with which the webhook was called
    called_payloads_list = []
    for arg_list in mocked_webhook_trigger.call_args_list:
        called_arg = json.loads(arg_list[0][0])
        # we don't want to compare meta fields, only rest of payloads
        called_arg[0].pop("meta")
        called_payloads_list.append(called_arg)

    # then
    for payload in payloads_per_sale:
        payload = json.loads(payload)
        payload[0].pop("meta")
        assert payload in called_payloads_list

    assert content["data"]["saleBulkDelete"]["count"] == 3
    assert mocked_webhook_trigger.call_count == 3
