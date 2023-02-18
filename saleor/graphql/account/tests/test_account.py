import datetime
import json
import os
import re
from collections import defaultdict
from datetime import timedelta
from unittest.mock import ANY, MagicMock, Mock, call, patch
from urllib.parse import urlencode

import graphene
import pytest
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.files import File
from django.test import override_settings
from django.utils import timezone
from django.utils.functional import SimpleLazyObject
from freezegun import freeze_time

from ....account import events as account_events
from ....account.error_codes import AccountErrorCode
from ....account.models import Address, Group, User
from ....account.notifications import get_default_user_payload
from ....account.search import (
    generate_address_search_document_value,
    generate_user_fields_search_document_value,
    prepare_user_search_document_value,
)
from ....checkout import AddressType
from ....core.jwt import create_token
from ....core.notify_events import NotifyEventType
from ....core.tests.utils import get_site_context_payload
from ....core.tokens import account_delete_token_generator
from ....core.utils.json_serializer import CustomJsonEncoder
from ....core.utils.url import prepare_url
from ....order import OrderStatus
from ....order.models import FulfillmentStatus, Order
from ....permission.enums import AccountPermissions, OrderPermissions
from ....product.tests.utils import create_image
from ....thumbnail.models import Thumbnail
from ....webhook.event_types import WebhookEventAsyncType
from ....webhook.payloads import (
    generate_customer_payload,
    generate_meta,
    generate_requestor,
)
from ...core.enums import ThumbnailFormatEnum
from ...core.utils import str_to_enum, to_global_id_or_none
from ...tests.utils import (
    assert_graphql_error_with_message,
    assert_no_permission,
    get_graphql_content,
    get_graphql_content_from_response,
    get_multipart_request_body,
)
from ..mutations.base import INVALID_TOKEN
from ..mutations.staff import CustomerDelete, StaffDelete, StaffUpdate, UserDelete
from ..tests.utils import convert_dict_keys_to_camel_case


def generate_address_webhook_call_args(address, event, requestor, webhook):
    return [
        json.dumps(
            {
                "id": graphene.Node.to_global_id("Address", address.id),
                "city": address.city,
                "country": {"code": address.country.code, "name": address.country.name},
                "company_name": address.company_name,
                "meta": generate_meta(
                    requestor_data=generate_requestor(
                        SimpleLazyObject(lambda: requestor)
                    )
                ),
            },
            cls=CustomJsonEncoder,
        ),
        event,
        [webhook],
        address,
        SimpleLazyObject(lambda: requestor),
    ]


@pytest.fixture
def query_customer_with_filter():
    query = """
    query ($filter: CustomerFilterInput!, ) {
        customers(first: 5, filter: $filter) {
            totalCount
            edges {
                node {
                    id
                    lastName
                    firstName
                }
            }
        }
    }
    """
    return query


@pytest.fixture
def query_staff_users_with_filter():
    query = """
    query ($filter: StaffUserInput!, ) {
        staffUsers(first: 5, filter: $filter) {
            totalCount
            edges {
                node {
                    id
                    lastName
                    firstName
                }
            }
        }
    }
    """
    return query


FULL_USER_QUERY = """
    query User($id: ID!) {
        user(id: $id) {
            email
            firstName
            lastName
            isStaff
            isActive
            addresses {
                id
                isDefaultShippingAddress
                isDefaultBillingAddress
            }
            checkoutIds
            orders(first: 10) {
                totalCount
                edges {
                    node {
                        id
                    }
                }
            }
            languageCode
            dateJoined
            lastLogin
            defaultShippingAddress {
                firstName
                lastName
                companyName
                streetAddress1
                streetAddress2
                city
                cityArea
                postalCode
                countryArea
                phone
                country {
                    code
                }
                isDefaultShippingAddress
                isDefaultBillingAddress
            }
            defaultBillingAddress {
                firstName
                lastName
                companyName
                streetAddress1
                streetAddress2
                city
                cityArea
                postalCode
                countryArea
                phone
                country {
                    code
                }
                isDefaultShippingAddress
                isDefaultBillingAddress
            }
            avatar {
                url
            }
            userPermissions {
                code
                sourcePermissionGroups(userId: $id) {
                    name
                }
            }
            permissionGroups {
                name
                permissions {
                    code
                }
            }
            editableGroups {
                name
            }
            giftCards(first: 10) {
                edges {
                    node {
                        id
                    }
                }
            }
            checkouts(first: 10) {
                edges {
                    node {
                        id
                    }
                }
            }
        }
    }
"""


def test_query_customer_user(
    staff_api_client,
    customer_user,
    gift_card_used,
    gift_card_expiry_date,
    address,
    permission_manage_users,
    permission_manage_orders,
    media_root,
    settings,
    checkout,
):
    user = customer_user
    user.default_shipping_address.country = "US"
    user.default_shipping_address.save()
    user.addresses.add(address.get_copy())

    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save()

    checkout.user = user
    checkout.save()

    Group.objects.create(name="empty group")

    query = FULL_USER_QUERY
    ID = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": ID}
    staff_api_client.user.user_permissions.add(
        permission_manage_users, permission_manage_orders
    )
    response = staff_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert data["email"] == user.email
    assert data["firstName"] == user.first_name
    assert data["lastName"] == user.last_name
    assert data["isStaff"] == user.is_staff
    assert data["isActive"] == user.is_active
    assert data["orders"]["totalCount"] == user.orders.count()
    assert data["avatar"]["url"]
    assert data["languageCode"] == settings.LANGUAGE_CODE.upper()
    assert len(data["editableGroups"]) == 0

    assert len(data["addresses"]) == user.addresses.count()
    for address in data["addresses"]:
        if address["isDefaultShippingAddress"]:
            address_id = graphene.Node.to_global_id(
                "Address", user.default_shipping_address.id
            )
            assert address["id"] == address_id
        if address["isDefaultBillingAddress"]:
            address_id = graphene.Node.to_global_id(
                "Address", user.default_billing_address.id
            )
            assert address["id"] == address_id

    address = data["defaultShippingAddress"]
    user_address = user.default_shipping_address
    assert address["firstName"] == user_address.first_name
    assert address["lastName"] == user_address.last_name
    assert address["companyName"] == user_address.company_name
    assert address["streetAddress1"] == user_address.street_address_1
    assert address["streetAddress2"] == user_address.street_address_2
    assert address["city"] == user_address.city
    assert address["cityArea"] == user_address.city_area
    assert address["postalCode"] == user_address.postal_code
    assert address["country"]["code"] == user_address.country.code
    assert address["countryArea"] == user_address.country_area
    assert address["phone"] == user_address.phone.as_e164
    assert address["isDefaultShippingAddress"] is None
    assert address["isDefaultBillingAddress"] is None

    address = data["defaultBillingAddress"]
    user_address = user.default_billing_address
    assert address["firstName"] == user_address.first_name
    assert address["lastName"] == user_address.last_name
    assert address["companyName"] == user_address.company_name
    assert address["streetAddress1"] == user_address.street_address_1
    assert address["streetAddress2"] == user_address.street_address_2
    assert address["city"] == user_address.city
    assert address["cityArea"] == user_address.city_area
    assert address["postalCode"] == user_address.postal_code
    assert address["country"]["code"] == user_address.country.code
    assert address["countryArea"] == user_address.country_area
    assert address["phone"] == user_address.phone.as_e164
    assert address["isDefaultShippingAddress"] is None
    assert address["isDefaultBillingAddress"] is None
    assert len(data["giftCards"]) == 1
    assert data["giftCards"]["edges"][0]["node"]["id"] == graphene.Node.to_global_id(
        "GiftCard", gift_card_used.pk
    )
    assert data["checkoutIds"] == [to_global_id_or_none(checkout)]
    assert data["checkouts"]["edges"][0]["node"]["id"] == graphene.Node.to_global_id(
        "Checkout", checkout.pk
    )


def test_query_customer_user_with_orders(
    staff_api_client,
    customer_user,
    order_list,
    permission_manage_users,
    permission_manage_orders,
):
    # given
    query = FULL_USER_QUERY
    order_unfulfilled = order_list[0]
    order_unfulfilled.user = customer_user

    order_unconfirmed = order_list[1]
    order_unconfirmed.status = OrderStatus.UNCONFIRMED
    order_unconfirmed.user = customer_user

    order_draft = order_list[2]
    order_draft.status = OrderStatus.DRAFT
    order_draft.user = customer_user

    Order.objects.bulk_update(
        [order_unconfirmed, order_draft, order_unfulfilled], ["user", "status"]
    )

    id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": id}

    # when
    response = staff_api_client.post_graphql(
        query,
        variables,
        permissions=[permission_manage_users, permission_manage_orders],
    )

    # then
    content = get_graphql_content(response)
    user = content["data"]["user"]
    assert {order["node"]["id"] for order in user["orders"]["edges"]} == {
        graphene.Node.to_global_id("Order", order.pk) for order in order_list
    }


def test_query_customer_user_with_orders_no_manage_orders_perm(
    staff_api_client,
    customer_user,
    order_list,
    permission_manage_users,
):
    # given
    query = FULL_USER_QUERY
    order_unfulfilled = order_list[0]
    order_unfulfilled.user = customer_user

    order_unconfirmed = order_list[1]
    order_unconfirmed.status = OrderStatus.UNCONFIRMED
    order_unconfirmed.user = customer_user

    order_draft = order_list[2]
    order_draft.status = OrderStatus.DRAFT
    order_draft.user = customer_user

    Order.objects.bulk_update(
        [order_unconfirmed, order_draft, order_unfulfilled], ["user", "status"]
    )

    id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": id}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    # then
    assert_no_permission(response)


def test_query_customer_user_app(
    app_api_client,
    customer_user,
    address,
    permission_manage_users,
    permission_manage_staff,
    permission_manage_orders,
    media_root,
    app,
):
    user = customer_user
    user.default_shipping_address.country = "US"
    user.default_shipping_address.save()
    user.addresses.add(address.get_copy())

    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save()

    Group.objects.create(name="empty group")

    query = FULL_USER_QUERY
    ID = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": ID}
    app.permissions.add(
        permission_manage_staff, permission_manage_users, permission_manage_orders
    )
    response = app_api_client.post_graphql(query, variables)

    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert data["email"] == user.email


def test_query_customer_user_with_orders_by_app_no_manage_orders_perm(
    app_api_client,
    customer_user,
    order_list,
    permission_manage_users,
):
    # given
    query = FULL_USER_QUERY
    order_unfulfilled = order_list[0]
    order_unfulfilled.user = customer_user

    order_unconfirmed = order_list[1]
    order_unconfirmed.status = OrderStatus.UNCONFIRMED
    order_unconfirmed.user = customer_user

    order_draft = order_list[2]
    order_draft.status = OrderStatus.DRAFT
    order_draft.user = customer_user

    Order.objects.bulk_update(
        [order_unconfirmed, order_draft, order_unfulfilled], ["user", "status"]
    )

    id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": id}

    # when
    response = app_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    # then
    assert_no_permission(response)


def test_query_staff_user(
    staff_api_client,
    staff_user,
    address,
    permission_manage_users,
    media_root,
    permission_manage_orders,
    permission_manage_products,
    permission_manage_staff,
    permission_manage_menus,
):
    staff_user.user_permissions.add(permission_manage_orders, permission_manage_staff)

    groups = Group.objects.bulk_create(
        [
            Group(name="manage users"),
            Group(name="another user group"),
            Group(name="another group"),
            Group(name="empty group"),
        ]
    )
    group1, group2, group3, group4 = groups

    group1.permissions.add(permission_manage_users, permission_manage_products)

    # user groups
    staff_user.groups.add(group1, group2)

    # another group (not user group) with permission_manage_users
    group3.permissions.add(permission_manage_users, permission_manage_menus)

    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image2.jpg"
    staff_user.avatar = avatar_mock
    staff_user.save()

    query = FULL_USER_QUERY
    user_id = graphene.Node.to_global_id("User", staff_user.pk)
    variables = {"id": user_id}
    response = staff_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["user"]

    assert data["email"] == staff_user.email
    assert data["firstName"] == staff_user.first_name
    assert data["lastName"] == staff_user.last_name
    assert data["isStaff"] == staff_user.is_staff
    assert data["isActive"] == staff_user.is_active
    assert data["orders"]["totalCount"] == staff_user.orders.count()
    assert data["avatar"]["url"]

    assert len(data["permissionGroups"]) == 2
    assert {group_data["name"] for group_data in data["permissionGroups"]} == {
        group1.name,
        group2.name,
    }
    assert len(data["userPermissions"]) == 4
    assert len(data["editableGroups"]) == Group.objects.count() - 1
    assert {data_group["name"] for data_group in data["editableGroups"]} == {
        group1.name,
        group2.name,
        group4.name,
    }

    formated_user_permissions_result = [
        {
            "code": perm["code"].lower(),
            "groups": {group["name"] for group in perm["sourcePermissionGroups"]},
        }
        for perm in data["userPermissions"]
    ]
    all_permissions = group1.permissions.all() | staff_user.user_permissions.all()
    for perm in all_permissions:
        source_groups = {group.name for group in perm.group_set.filter(user=staff_user)}
        expected_data = {"code": perm.codename, "groups": source_groups}
        assert expected_data in formated_user_permissions_result


def test_query_staff_user_with_order_and_without_manage_orders_perm(
    staff_api_client,
    staff_user,
    order_list,
    permission_manage_staff,
):
    # given
    staff_user.user_permissions.add(permission_manage_staff)

    order_unfulfilled = order_list[0]
    order_unfulfilled.user = staff_user

    order_unconfirmed = order_list[1]
    order_unconfirmed.status = OrderStatus.UNCONFIRMED
    order_unconfirmed.user = staff_user

    order_draft = order_list[2]
    order_draft.status = OrderStatus.DRAFT
    order_draft.user = staff_user

    Order.objects.bulk_update(
        [order_unconfirmed, order_draft, order_unfulfilled], ["user", "status"]
    )

    query = FULL_USER_QUERY
    user_id = graphene.Node.to_global_id("User", staff_user.pk)
    variables = {"id": user_id}
    response = staff_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["user"]

    assert data["email"] == staff_user.email
    assert data["orders"]["totalCount"] == 2
    assert {node["node"]["id"] for node in data["orders"]["edges"]} == {
        graphene.Node.to_global_id("Order", order.pk)
        for order in [order_unfulfilled, order_unconfirmed]
    }


def test_query_staff_user_with_orders_and_manage_orders_perm(
    staff_api_client,
    staff_user,
    order_list,
    permission_manage_staff,
    permission_manage_orders,
):
    # given
    staff_user.user_permissions.add(permission_manage_staff, permission_manage_orders)

    order_unfulfilled = order_list[0]
    order_unfulfilled.user = staff_user

    order_unconfirmed = order_list[1]
    order_unconfirmed.status = OrderStatus.UNCONFIRMED
    order_unconfirmed.user = staff_user

    order_draft = order_list[2]
    order_draft.status = OrderStatus.DRAFT
    order_draft.user = staff_user

    Order.objects.bulk_update(
        [order_unconfirmed, order_draft, order_unfulfilled], ["user", "status"]
    )

    query = FULL_USER_QUERY
    user_id = graphene.Node.to_global_id("User", staff_user.pk)
    variables = {"id": user_id}
    response = staff_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["user"]

    assert data["email"] == staff_user.email
    assert data["orders"]["totalCount"] == 3
    assert {node["node"]["id"] for node in data["orders"]["edges"]} == {
        graphene.Node.to_global_id("Order", order.pk)
        for order in [order_unfulfilled, order_unconfirmed, order_draft]
    }


USER_QUERY = """
    query User($id: ID $email: String, $externalReference: String) {
        user(id: $id, email: $email, externalReference: $externalReference) {
            id
            email
            externalReference
        }
    }
"""


def test_query_user_by_email_address(
    user_api_client, customer_user, permission_manage_users
):
    email = customer_user.email
    variables = {"email": email}
    response = user_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert customer_user.email == data["email"]


def test_query_user_by_external_reference(
    user_api_client, customer_user, permission_manage_users
):
    # given
    user = customer_user
    ext_ref = "test-ext-ref"
    user.external_reference = ext_ref
    user.save(update_fields=["external_reference"])
    variables = {"externalReference": ext_ref}

    # when
    response = user_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["user"]
    assert data["externalReference"] == user.external_reference


def test_query_user_by_id_and_email(
    user_api_client, customer_user, permission_manage_users
):
    email = customer_user.email
    id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {
        "id": id,
        "email": email,
    }
    response = user_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )
    assert_graphql_error_with_message(
        response, "Argument 'id' cannot be combined with 'email'"
    )


def test_customer_can_not_see_other_users_data(user_api_client, staff_user):
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": id}
    response = user_api_client.post_graphql(USER_QUERY, variables)
    assert_no_permission(response)


def test_user_query_anonymous_user(api_client):
    variables = {"id": ""}
    response = api_client.post_graphql(USER_QUERY, variables)
    assert_no_permission(response)


def test_user_query_permission_manage_users_get_customer(
    staff_api_client, customer_user, permission_manage_users
):
    customer_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {"id": customer_id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert customer_user.email == data["email"]


def test_user_query_as_app(app_api_client, customer_user, permission_manage_users):
    customer_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {"id": customer_id}
    response = app_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert customer_user.email == data["email"]


def test_user_query_permission_manage_users_get_staff(
    staff_api_client, staff_user, permission_manage_users
):
    staff_id = graphene.Node.to_global_id("User", staff_user.pk)
    variables = {"id": staff_id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    assert not content["data"]["user"]


def test_user_query_permission_manage_staff_get_customer(
    staff_api_client, customer_user, permission_manage_staff
):
    customer_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {"id": customer_id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    assert not content["data"]["user"]


def test_user_query_permission_manage_staff_get_staff(
    staff_api_client, staff_user, permission_manage_staff
):
    staff_id = graphene.Node.to_global_id("User", staff_user.pk)
    variables = {"id": staff_id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert staff_user.email == data["email"]


@pytest.mark.parametrize("id", ["'", "abc"])
def test_user_query_invalid_id(
    id, staff_api_client, customer_user, permission_manage_users
):
    variables = {"id": id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )

    content = get_graphql_content_from_response(response)
    assert len(content["errors"]) == 1
    assert content["errors"][0]["message"] == f"Couldn't resolve id: {id}."
    assert content["data"]["user"] is None


def test_user_query_object_with_given_id_does_not_exist(
    staff_api_client, permission_manage_users
):
    id = graphene.Node.to_global_id("User", -1)
    variables = {"id": id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )

    content = get_graphql_content(response)
    assert content["data"]["user"] is None


def test_user_query_object_with_invalid_object_type(
    staff_api_client, customer_user, permission_manage_users
):
    id = graphene.Node.to_global_id("Order", customer_user.pk)
    variables = {"id": id}
    response = staff_api_client.post_graphql(
        USER_QUERY, variables, permissions=[permission_manage_users]
    )

    content = get_graphql_content(response)
    assert content["data"]["user"] is None


USER_AVATAR_QUERY = """
    query User($id: ID, $size: Int, $format: ThumbnailFormatEnum) {
        user(id: $id) {
            id
            avatar(size: $size, format: $format) {
                url
                alt
            }
        }
    }
"""


def test_query_user_avatar_with_size_and_format_proxy_url_returned(
    staff_api_client, media_root, permission_manage_staff, site_settings
):
    # given
    user = staff_api_client.user
    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save(update_fields=["avatar"])

    format = ThumbnailFormatEnum.WEBP.name

    user_id = graphene.Node.to_global_id("User", user.id)
    user_uuid = graphene.Node.to_global_id("User", user.uuid)
    variables = {"id": user_id, "size": 120, "format": format}

    # when
    response = staff_api_client.post_graphql(
        USER_AVATAR_QUERY, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["user"]
    domain = site_settings.site.domain
    assert (
        data["avatar"]["url"]
        == f"http://{domain}/thumbnail/{user_uuid}/128/{format.lower()}/"
    )


def test_query_user_avatar_with_size_proxy_url_returned(
    staff_api_client, media_root, permission_manage_staff, site_settings
):
    # given
    user = staff_api_client.user
    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save(update_fields=["avatar"])

    user_id = graphene.Node.to_global_id("User", user.id)
    user_uuid = graphene.Node.to_global_id("User", user.uuid)
    variables = {"id": user_id, "size": 120}

    # when
    response = staff_api_client.post_graphql(
        USER_AVATAR_QUERY, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert (
        data["avatar"]["url"]
        == f"http://{site_settings.site.domain}/thumbnail/{user_uuid}/128/"
    )


def test_query_user_avatar_with_size_thumbnail_url_returned(
    staff_api_client, media_root, permission_manage_staff, site_settings
):
    # given
    user = staff_api_client.user
    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save(update_fields=["avatar"])

    thumbnail_mock = MagicMock(spec=File)
    thumbnail_mock.name = "thumbnail_image.jpg"
    Thumbnail.objects.create(user=user, size=128, image=thumbnail_mock)

    id = graphene.Node.to_global_id("User", user.pk)
    variables = {"id": id, "size": 120}

    # when
    response = staff_api_client.post_graphql(
        USER_AVATAR_QUERY, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert (
        data["avatar"]["url"]
        == f"http://{site_settings.site.domain}/media/thumbnails/{thumbnail_mock.name}"
    )


def test_query_user_avatar_original_size_custom_format_provided_original_image_returned(
    staff_api_client, media_root, permission_manage_staff, site_settings
):
    # given
    user = staff_api_client.user
    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save(update_fields=["avatar"])

    format = ThumbnailFormatEnum.WEBP.name

    id = graphene.Node.to_global_id("User", user.pk)
    variables = {"id": id, "format": format, "size": 0}

    # when
    response = staff_api_client.post_graphql(
        USER_AVATAR_QUERY, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert (
        data["avatar"]["url"]
        == f"http://{site_settings.site.domain}/media/user-avatars/{avatar_mock.name}"
    )


def test_query_user_avatar_no_size_value(
    staff_api_client, media_root, permission_manage_staff, site_settings
):
    # given
    user = staff_api_client.user
    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save(update_fields=["avatar"])

    id = graphene.Node.to_global_id("User", user.pk)
    variables = {"id": id}

    user_uuid = graphene.Node.to_global_id("User", user.uuid)

    # when
    response = staff_api_client.post_graphql(
        USER_AVATAR_QUERY, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert (
        data["avatar"]["url"]
        == f"http://{site_settings.site.domain}/thumbnail/{user_uuid}/4096/"
    )


def test_query_user_avatar_no_image(staff_api_client, permission_manage_staff):
    # given
    user = staff_api_client.user

    id = graphene.Node.to_global_id("User", user.pk)
    variables = {"id": id}

    # when
    response = staff_api_client.post_graphql(
        USER_AVATAR_QUERY, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["user"]
    assert data["id"]
    assert not data["avatar"]


def test_query_customers(staff_api_client, user_api_client, permission_manage_users):
    query = """
    query Users {
        customers(first: 20) {
            totalCount
            edges {
                node {
                    isStaff
                }
            }
        }
    }
    """
    variables = {}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]
    assert users
    assert all([not user["node"]["isStaff"] for user in users])

    # check permissions
    response = user_api_client.post_graphql(query, variables)
    assert_no_permission(response)


def test_query_staff(
    staff_api_client, user_api_client, staff_user, admin_user, permission_manage_staff
):
    query = """
    {
        staffUsers(first: 20) {
            edges {
                node {
                    email
                    isStaff
                }
            }
        }
    }
    """
    variables = {}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUsers"]["edges"]
    assert len(data) == 2
    staff_emails = [user["node"]["email"] for user in data]
    assert sorted(staff_emails) == [admin_user.email, staff_user.email]
    assert all([user["node"]["isStaff"] for user in data])

    # check permissions
    response = user_api_client.post_graphql(query, variables)
    assert_no_permission(response)


def test_who_can_see_user(
    staff_user, customer_user, staff_api_client, permission_manage_users
):
    query = """
    query Users {
        customers {
            totalCount
        }
    }
    """

    # Random person (even staff) can't see users data without permissions
    ID = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": ID}
    response = staff_api_client.post_graphql(USER_QUERY, variables)
    assert_no_permission(response)

    response = staff_api_client.post_graphql(query)
    assert_no_permission(response)

    # Add permission and ensure staff can see user(s)
    staff_user.user_permissions.add(permission_manage_users)
    response = staff_api_client.post_graphql(USER_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["user"]["email"] == customer_user.email

    response = staff_api_client.post_graphql(query)
    content = get_graphql_content(response)
    assert content["data"]["customers"]["totalCount"] == 1


ME_QUERY = """
    query Me {
        me {
            id
            email
            checkout {
                token
            }
            userPermissions {
                code
                name
            }
            checkouts(first: 10) {
                edges {
                    node {
                        id
                    }
                }
                totalCount
            }
        }
    }
"""


def test_me_query(user_api_client):
    response = user_api_client.post_graphql(ME_QUERY)
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert data["email"] == user_api_client.user.email


def test_me_user_permissions_query(
    user_api_client, permission_manage_users, permission_group_manage_users
):
    user = user_api_client.user
    user.user_permissions.add(permission_manage_users)
    user.groups.add(permission_group_manage_users)
    response = user_api_client.post_graphql(ME_QUERY)
    content = get_graphql_content(response)
    user_permissions = content["data"]["me"]["userPermissions"]

    assert len(user_permissions) == 1
    assert user_permissions[0]["code"] == permission_manage_users.codename.upper()


def test_me_query_anonymous_client(api_client):
    response = api_client.post_graphql(ME_QUERY)
    content = get_graphql_content(response)
    assert content["data"]["me"] is None


def test_me_query_customer_can_not_see_note(
    staff_user, staff_api_client, permission_manage_users
):
    query = """
    query Me {
        me {
            id
            email
            note
        }
    }
    """
    # Random person (even staff) can't see own note without permissions
    response = staff_api_client.post_graphql(query)
    assert_no_permission(response)

    # Add permission and ensure staff can see own note
    response = staff_api_client.post_graphql(
        query, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert data["email"] == staff_api_client.user.email
    assert data["note"] == staff_api_client.user.note


def test_me_query_checkout(user_api_client, checkout):
    user = user_api_client.user
    checkout.user = user
    checkout.save()

    response = user_api_client.post_graphql(ME_QUERY)
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert data["checkout"]["token"] == str(checkout.token)
    assert data["checkouts"]["edges"][0]["node"]["id"] == graphene.Node.to_global_id(
        "Checkout", checkout.pk
    )


def test_me_query_checkout_with_inactive_channel(user_api_client, checkout):
    user = user_api_client.user
    channel = checkout.channel
    channel.is_active = False
    channel.save()
    checkout.user = user
    checkout.save()

    response = user_api_client.post_graphql(ME_QUERY)
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert not data["checkout"]
    assert not data["checkouts"]["edges"]


def test_me_query_checkouts_with_channel(user_api_client, checkout, checkout_JPY):
    query = """
        query Me($channel: String) {
            me {
                checkouts(first: 10, channel: $channel) {
                    edges {
                        node {
                            id
                            channel {
                                slug
                            }
                        }
                    }
                    totalCount
                }
            }
        }
    """

    user = user_api_client.user
    checkout.user = checkout_JPY.user = user
    checkout.save()
    checkout_JPY.save()

    response = user_api_client.post_graphql(query, {"channel": checkout.channel.slug})

    content = get_graphql_content(response)
    data = content["data"]["me"]["checkouts"]
    assert data["edges"][0]["node"]["id"] == graphene.Node.to_global_id(
        "Checkout", checkout.pk
    )
    assert data["totalCount"] == 1
    assert data["edges"][0]["node"]["channel"]["slug"] == checkout.channel.slug


QUERY_ME_CHECKOUT_TOKENS = """
query getCheckoutTokens($channel: String) {
  me {
    checkoutTokens(channel: $channel)
  }
}
"""


def test_me_checkout_tokens_without_channel_param(
    user_api_client, checkouts_assigned_to_customer
):
    # given
    checkouts = checkouts_assigned_to_customer

    # when
    response = user_api_client.post_graphql(QUERY_ME_CHECKOUT_TOKENS)

    # then
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert len(data["checkoutTokens"]) == len(checkouts)
    for checkout in checkouts:
        assert str(checkout.token) in data["checkoutTokens"]


def test_me_checkout_tokens_without_channel_param_inactive_channel(
    user_api_client, channel_PLN, checkouts_assigned_to_customer
):
    # given
    channel_PLN.is_active = False
    channel_PLN.save()
    checkouts = checkouts_assigned_to_customer

    # when
    response = user_api_client.post_graphql(QUERY_ME_CHECKOUT_TOKENS)

    # then
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert str(checkouts[0].token) in data["checkoutTokens"]
    assert str(checkouts[1].token) not in data["checkoutTokens"]


def test_me_checkout_tokens_with_channel(
    user_api_client, channel_USD, checkouts_assigned_to_customer
):
    # given
    checkouts = checkouts_assigned_to_customer

    # when
    response = user_api_client.post_graphql(
        QUERY_ME_CHECKOUT_TOKENS, {"channel": channel_USD.slug}
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert str(checkouts[0].token) in data["checkoutTokens"]
    assert str(checkouts[1].token) not in data["checkoutTokens"]


def test_me_checkout_tokens_with_inactive_channel(
    user_api_client, channel_USD, checkouts_assigned_to_customer
):
    # given
    channel_USD.is_active = False
    channel_USD.save()

    # when
    response = user_api_client.post_graphql(
        QUERY_ME_CHECKOUT_TOKENS, {"channel": channel_USD.slug}
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert not data["checkoutTokens"]


def test_me_checkout_tokens_with_not_existing_channel(
    user_api_client, checkouts_assigned_to_customer
):
    # given

    # when
    response = user_api_client.post_graphql(
        QUERY_ME_CHECKOUT_TOKENS, {"channel": "Not-existing"}
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["me"]
    assert not data["checkoutTokens"]


def test_me_with_cancelled_fulfillments(
    user_api_client, fulfilled_order_with_cancelled_fulfillment
):
    query = """
    query Me {
        me {
            orders (first: 1) {
                edges {
                    node {
                        id
                        fulfillments {
                            status
                        }
                    }
                }
            }
        }
    }
    """
    response = user_api_client.post_graphql(query)
    content = get_graphql_content(response)
    order_id = graphene.Node.to_global_id(
        "Order", fulfilled_order_with_cancelled_fulfillment.id
    )
    data = content["data"]["me"]
    order = data["orders"]["edges"][0]["node"]
    assert order["id"] == order_id
    fulfillments = order["fulfillments"]
    assert len(fulfillments) == 1
    assert fulfillments[0]["status"] == FulfillmentStatus.FULFILLED.upper()


def test_user_with_cancelled_fulfillments(
    staff_api_client,
    customer_user,
    permission_manage_users,
    permission_manage_orders,
    fulfilled_order_with_cancelled_fulfillment,
):
    query = """
    query User($id: ID!) {
        user(id: $id) {
            orders (first: 1) {
                edges {
                    node {
                        id
                        fulfillments {
                            status
                        }
                    }
                }
            }
        }
    }
    """
    user_id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"id": user_id}
    staff_api_client.user.user_permissions.add(
        permission_manage_users, permission_manage_orders
    )
    response = staff_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    order_id = graphene.Node.to_global_id(
        "Order", fulfilled_order_with_cancelled_fulfillment.id
    )
    data = content["data"]["user"]
    order = data["orders"]["edges"][0]["node"]
    assert order["id"] == order_id
    fulfillments = order["fulfillments"]
    assert len(fulfillments) == 2
    assert fulfillments[0]["status"] == FulfillmentStatus.FULFILLED.upper()
    assert fulfillments[1]["status"] == FulfillmentStatus.CANCELED.upper()


ACCOUNT_REGISTER_MUTATION = """
    mutation RegisterAccount(
        $password: String!,
        $email: String!,
        $firstName: String,
        $lastName: String,
        $redirectUrl: String,
        $languageCode: LanguageCodeEnum
        $metadata: [MetadataInput!],
        $channel: String
    ) {
        accountRegister(
            input: {
                password: $password,
                email: $email,
                firstName: $firstName,
                lastName: $lastName,
                redirectUrl: $redirectUrl,
                languageCode: $languageCode,
                metadata: $metadata,
                channel: $channel
            }
        ) {
            errors {
                field
                message
                code
            }
            user {
                id
                email
            }
        }
    }
"""


@override_settings(
    ENABLE_ACCOUNT_CONFIRMATION_BY_EMAIL=True, ALLOWED_CLIENT_HOSTS=["localhost"]
)
@patch("saleor.account.notifications.default_token_generator.make_token")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_customer_register(
    mocked_notify,
    mocked_generator,
    api_client,
    channel_PLN,
    order,
    site_settings,
):
    mocked_generator.return_value = "token"
    email = "customer@example.com"

    redirect_url = "http://localhost:3000"
    variables = {
        "email": email,
        "password": "Password",
        "redirectUrl": redirect_url,
        "firstName": "saleor",
        "lastName": "rocks",
        "languageCode": "PL",
        "metadata": [{"key": "meta", "value": "data"}],
        "channel": channel_PLN.slug,
    }
    query = ACCOUNT_REGISTER_MUTATION
    mutation_name = "accountRegister"

    response = api_client.post_graphql(query, variables)

    new_user = User.objects.get(email=email)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    params = urlencode({"email": email, "token": "token"})
    confirm_url = prepare_url(params, redirect_url)

    expected_payload = {
        "user": get_default_user_payload(new_user),
        "token": "token",
        "confirm_url": confirm_url,
        "recipient_email": new_user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }
    assert new_user.metadata == {"meta": "data"}
    assert new_user.language_code == "pl"
    assert new_user.first_name == variables["firstName"]
    assert new_user.last_name == variables["lastName"]
    assert new_user.search_document == generate_user_fields_search_document_value(
        new_user
    )
    assert not data["errors"]
    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_CONFIRMATION,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )

    response = api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert data["errors"]
    assert data["errors"][0]["field"] == "email"
    assert data["errors"][0]["code"] == AccountErrorCode.UNIQUE.name

    customer_creation_event = account_events.CustomerEvent.objects.get()
    assert customer_creation_event.type == account_events.CustomerEvents.ACCOUNT_CREATED
    assert customer_creation_event.user == new_user


@override_settings(ENABLE_ACCOUNT_CONFIRMATION_BY_EMAIL=False)
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_customer_register_disabled_email_confirmation(mocked_notify, api_client):
    email = "customer@example.com"
    variables = {"email": email, "password": "Password"}
    response = api_client.post_graphql(ACCOUNT_REGISTER_MUTATION, variables)
    errors = response.json()["data"]["accountRegister"]["errors"]

    assert errors == []
    created_user = User.objects.get()
    expected_payload = get_default_user_payload(created_user)
    expected_payload["token"] = "token"
    expected_payload["redirect_url"] = "http://localhost:3000"
    mocked_notify.assert_not_called()


@override_settings(ENABLE_ACCOUNT_CONFIRMATION_BY_EMAIL=True)
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_customer_register_no_redirect_url(mocked_notify, api_client):
    variables = {"email": "customer@example.com", "password": "Password"}
    response = api_client.post_graphql(ACCOUNT_REGISTER_MUTATION, variables)
    errors = response.json()["data"]["accountRegister"]["errors"]
    assert "redirectUrl" in map(lambda error: error["field"], errors)
    mocked_notify.assert_not_called()


@override_settings(ENABLE_ACCOUNT_CONFIRMATION_BY_EMAIL=False)
def test_customer_register_upper_case_email(api_client):
    # given
    email = "CUSTOMER@example.com"
    variables = {"email": email, "password": "Password"}

    # when
    response = api_client.post_graphql(ACCOUNT_REGISTER_MUTATION, variables)
    content = get_graphql_content(response)

    # then
    data = content["data"]["accountRegister"]
    assert not data["errors"]
    assert data["user"]["email"].lower()


CUSTOMER_CREATE_MUTATION = """
    mutation CreateCustomer(
        $email: String, $firstName: String, $lastName: String, $channel: String
        $note: String, $billing: AddressInput, $shipping: AddressInput,
        $redirect_url: String, $languageCode: LanguageCodeEnum,
        $externalReference: String
    ) {
        customerCreate(input: {
            email: $email,
            firstName: $firstName,
            lastName: $lastName,
            note: $note,
            defaultShippingAddress: $shipping,
            defaultBillingAddress: $billing,
            redirectUrl: $redirect_url,
            languageCode: $languageCode,
            channel: $channel,
            externalReference: $externalReference
        }) {
            errors {
                field
                code
                message
            }
            user {
                id
                defaultBillingAddress {
                    id
                }
                defaultShippingAddress {
                    id
                }
                languageCode
                email
                firstName
                lastName
                isActive
                isStaff
                note
                externalReference
            }
        }
    }
"""


@patch("saleor.account.notifications.default_token_generator.make_token")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_customer_create(
    mocked_notify,
    mocked_generator,
    staff_api_client,
    address,
    permission_manage_users,
    channel_PLN,
    site_settings,
):
    mocked_generator.return_value = "token"
    email = "api_user@example.com"
    first_name = "api_first_name"
    last_name = "api_last_name"
    note = "Test user"
    address_data = convert_dict_keys_to_camel_case(address.as_data())
    address_data.pop("metadata")
    address_data.pop("privateMetadata")

    redirect_url = "https://www.example.com"
    external_reference = "test-ext-ref"
    variables = {
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "note": note,
        "shipping": address_data,
        "billing": address_data,
        "redirect_url": redirect_url,
        "languageCode": "PL",
        "channel": channel_PLN.slug,
        "externalReference": external_reference,
    }

    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    new_customer = User.objects.get(email=email)

    shipping_address, billing_address = (
        new_customer.default_shipping_address,
        new_customer.default_billing_address,
    )
    assert shipping_address == address
    assert billing_address == address
    assert shipping_address.pk != billing_address.pk

    data = content["data"]["customerCreate"]
    assert data["errors"] == []
    assert data["user"]["email"] == email
    assert data["user"]["firstName"] == first_name
    assert data["user"]["lastName"] == last_name
    assert data["user"]["note"] == note
    assert data["user"]["languageCode"] == "PL"
    assert data["user"]["externalReference"] == external_reference
    assert not data["user"]["isStaff"]
    assert data["user"]["isActive"]

    new_user = User.objects.get(email=email)
    assert (
        generate_user_fields_search_document_value(new_user) in new_user.search_document
    )
    assert generate_address_search_document_value(address) in new_user.search_document
    params = urlencode({"email": new_user.email, "token": "token"})
    password_set_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(new_user),
        "token": "token",
        "password_set_url": password_set_url,
        "recipient_email": new_user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }
    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_SET_CUSTOMER_PASSWORD,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )

    assert set([shipping_address, billing_address]) == set(new_user.addresses.all())
    customer_creation_event = account_events.CustomerEvent.objects.get()
    assert customer_creation_event.type == account_events.CustomerEvents.ACCOUNT_CREATED
    assert customer_creation_event.user == new_customer


@patch("saleor.account.notifications.default_token_generator.make_token")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_customer_create_send_password_with_url(
    mocked_notify,
    mocked_generator,
    staff_api_client,
    permission_manage_users,
    channel_PLN,
    site_settings,
):
    mocked_generator.return_value = "token"
    email = "api_user@example.com"
    variables = {
        "email": email,
        "redirect_url": "https://www.example.com",
        "channel": channel_PLN.slug,
    }

    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerCreate"]
    assert not data["errors"]

    new_customer = User.objects.get(email=email)
    assert new_customer
    redirect_url = "https://www.example.com"
    params = urlencode({"email": email, "token": "token"})
    password_set_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(new_customer),
        "password_set_url": password_set_url,
        "token": "token",
        "recipient_email": new_customer.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }
    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_SET_CUSTOMER_PASSWORD,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


def test_customer_create_without_send_password(
    staff_api_client, permission_manage_users
):
    email = "api_user@example.com"
    variables = {"email": email}
    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerCreate"]
    assert not data["errors"]
    User.objects.get(email=email)


def test_customer_create_with_invalid_url(staff_api_client, permission_manage_users):
    email = "api_user@example.com"
    variables = {"email": email, "redirect_url": "invalid"}
    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerCreate"]
    assert data["errors"][0] == {
        "field": "redirectUrl",
        "code": AccountErrorCode.INVALID.name,
        "message": ANY,
    }
    staff_user = User.objects.filter(email=email)
    assert not staff_user


def test_customer_create_with_not_allowed_url(
    staff_api_client, permission_manage_users
):
    email = "api_user@example.com"
    variables = {"email": email, "redirect_url": "https://www.fake.com"}
    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerCreate"]
    assert data["errors"][0] == {
        "field": "redirectUrl",
        "code": AccountErrorCode.INVALID.name,
        "message": ANY,
    }
    staff_user = User.objects.filter(email=email)
    assert not staff_user


def test_customer_create_with_upper_case_email(
    staff_api_client, permission_manage_users
):
    # given
    email = "UPPERCASE@example.com"
    variables = {"email": email}

    # when
    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["customerCreate"]
    assert not data["errors"]
    assert data["user"]["email"] == email.lower()


def test_customer_create_with_non_unique_external_reference(
    staff_api_client, permission_manage_users, customer_user
):
    # given
    ext_ref = "test-ext-ref"
    customer_user.external_reference = ext_ref
    customer_user.save(update_fields=["external_reference"])

    variables = {"email": "mail.test@exampale.com", "externalReference": ext_ref}

    # when
    response = staff_api_client.post_graphql(
        CUSTOMER_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    error = content["data"]["customerCreate"]["errors"][0]
    assert error["field"] == "externalReference"
    assert error["code"] == AccountErrorCode.UNIQUE.name
    assert error["message"] == "User with this External reference already exists."


def test_customer_update(
    staff_api_client, staff_user, customer_user, address, permission_manage_users
):
    query = """
    mutation UpdateCustomer(
            $id: ID!, $firstName: String, $lastName: String,
            $isActive: Boolean, $note: String, $billing: AddressInput,
            $shipping: AddressInput, $languageCode: LanguageCodeEnum,
            $externalReference: String
        ) {
        customerUpdate(
            id: $id,
            input: {
                isActive: $isActive,
                firstName: $firstName,
                lastName: $lastName,
                note: $note,
                defaultBillingAddress: $billing
                defaultShippingAddress: $shipping,
                languageCode: $languageCode,
                externalReference: $externalReference
                }
            ) {
            errors {
                field
                message
            }
            user {
                id
                firstName
                lastName
                defaultBillingAddress {
                    id
                }
                defaultShippingAddress {
                    id
                }
                languageCode
                isActive
                note
                externalReference
            }
        }
    }
    """

    # this test requires addresses to be set and checks whether new address
    # instances weren't created, but the existing ones got updated
    assert customer_user.default_billing_address
    assert customer_user.default_shipping_address
    billing_address_pk = customer_user.default_billing_address.pk
    shipping_address_pk = customer_user.default_shipping_address.pk

    user_id = graphene.Node.to_global_id("User", customer_user.id)
    first_name = "new_first_name"
    last_name = "new_last_name"
    note = "Test update note"
    external_reference = "test-ext-ref"
    address_data = convert_dict_keys_to_camel_case(address.as_data())
    address_data.pop("metadata")
    address_data.pop("privateMetadata")

    new_street_address = "Updated street address"
    address_data["streetAddress1"] = new_street_address

    variables = {
        "id": user_id,
        "firstName": first_name,
        "lastName": last_name,
        "isActive": False,
        "note": note,
        "billing": address_data,
        "shipping": address_data,
        "languageCode": "PL",
        "externalReference": external_reference,
    }
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    customer = User.objects.get(email=customer_user.email)

    # check that existing instances are updated
    shipping_address, billing_address = (
        customer.default_shipping_address,
        customer.default_billing_address,
    )
    assert billing_address.pk == billing_address_pk
    assert shipping_address.pk == shipping_address_pk

    assert billing_address.street_address_1 == new_street_address
    assert shipping_address.street_address_1 == new_street_address

    data = content["data"]["customerUpdate"]
    assert data["errors"] == []
    assert data["user"]["firstName"] == first_name
    assert data["user"]["lastName"] == last_name
    assert data["user"]["note"] == note
    assert data["user"]["languageCode"] == "PL"
    assert data["user"]["externalReference"] == external_reference
    assert not data["user"]["isActive"]

    (
        name_changed_event,
        deactivated_event,
    ) = account_events.CustomerEvent.objects.order_by("pk")

    assert name_changed_event.type == account_events.CustomerEvents.NAME_ASSIGNED
    assert name_changed_event.user.pk == staff_user.pk
    assert name_changed_event.parameters == {"message": customer.get_full_name()}

    assert deactivated_event.type == account_events.CustomerEvents.ACCOUNT_DEACTIVATED
    assert deactivated_event.user.pk == staff_user.pk
    assert deactivated_event.parameters == {"account_id": customer_user.id}

    customer_user.refresh_from_db()
    assert (
        generate_address_search_document_value(billing_address)
        in customer_user.search_document
    )
    assert (
        generate_address_search_document_value(shipping_address)
        in customer_user.search_document
    )


UPDATE_CUSTOMER_BY_EXTERNAL_REFERENCE = """
    mutation UpdateCustomer(
        $id: ID, $externalReference: String, $input: CustomerInput!
    ) {
        customerUpdate(id: $id, externalReference: $externalReference, input: $input) {
            errors {
                field
                message
                code
            }
            user {
                id
                externalReference
                firstName
            }
        }
    }
    """


def test_customer_update_by_external_reference(
    staff_api_client, customer_user, permission_manage_users
):
    # given
    query = UPDATE_CUSTOMER_BY_EXTERNAL_REFERENCE
    user = customer_user
    new_name = "updated name"
    ext_ref = "test-ext-ref"
    user.external_reference = ext_ref
    user.save(update_fields=["external_reference"])

    variables = {
        "externalReference": ext_ref,
        "input": {"firstName": new_name},
    }

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    user.refresh_from_db()
    data = content["data"]["customerUpdate"]
    assert not data["errors"]
    assert data["user"]["firstName"] == new_name == user.first_name
    assert data["user"]["id"] == graphene.Node.to_global_id("User", user.id)
    assert data["user"]["externalReference"] == ext_ref


def test_update_customer_by_both_id_and_external_reference(
    staff_api_client, customer_user, permission_manage_users
):
    # given
    query = UPDATE_CUSTOMER_BY_EXTERNAL_REFERENCE
    variables = {"input": {}, "externalReference": "whatever", "id": "whatever"}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["customerUpdate"]
    assert not data["user"]
    assert (
        data["errors"][0]["message"]
        == "Argument 'id' cannot be combined with 'external_reference'"
    )


def test_update_customer_by_external_reference_not_existing(
    staff_api_client, customer_user, permission_manage_users
):
    # given
    query = UPDATE_CUSTOMER_BY_EXTERNAL_REFERENCE
    ext_ref = "non-existing-ext-ref"
    variables = {
        "input": {},
        "externalReference": ext_ref,
    }

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["customerUpdate"]
    assert not data["user"]
    assert data["errors"][0]["message"] == f"Couldn't resolve to a node: {ext_ref}"
    assert data["errors"][0]["field"] == "externalReference"


def test_update_customer_with_non_unique_external_reference(
    staff_api_client, permission_manage_users, user_list
):
    # given
    query = UPDATE_CUSTOMER_BY_EXTERNAL_REFERENCE

    ext_ref = "test-ext-ref"
    user_1 = user_list[0]
    user_1.external_reference = ext_ref
    user_1.save(update_fields=["external_reference"])
    user_2_id = graphene.Node.to_global_id("User", user_list[1].id)

    variables = {"input": {"externalReference": ext_ref}, "id": user_2_id}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    error = content["data"]["customerUpdate"]["errors"][0]
    assert error["field"] == "externalReference"
    assert error["code"] == AccountErrorCode.UNIQUE.name
    assert error["message"] == "User with this External reference already exists."


UPDATE_CUSTOMER_EMAIL_MUTATION = """
    mutation UpdateCustomer(
            $id: ID!, $firstName: String, $lastName: String, $email: String) {
        customerUpdate(id: $id, input: {
            firstName: $firstName,
            lastName: $lastName,
            email: $email
        }) {
            errors {
                field
                message
            }
        }
    }
"""


def test_customer_update_generates_event_when_changing_email(
    staff_api_client, staff_user, customer_user, address, permission_manage_users
):
    query = UPDATE_CUSTOMER_EMAIL_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)
    address_data = convert_dict_keys_to_camel_case(address.as_data())

    new_street_address = "Updated street address"
    address_data["streetAddress1"] = new_street_address

    variables = {
        "id": user_id,
        "firstName": customer_user.first_name,
        "lastName": customer_user.last_name,
        "email": "mirumee@example.com",
    }
    staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    # The email was changed, an event should have been triggered
    email_changed_event = account_events.CustomerEvent.objects.get()
    assert email_changed_event.type == account_events.CustomerEvents.EMAIL_ASSIGNED
    assert email_changed_event.user.pk == staff_user.pk
    assert email_changed_event.parameters == {"message": "mirumee@example.com"}


UPDATE_CUSTOMER_IS_ACTIVE_MUTATION = """
    mutation UpdateCustomer(
        $id: ID!, $isActive: Boolean) {
            customerUpdate(id: $id, input: {
            isActive: $isActive,
        }) {
            errors {
                field
                message
            }
        }
    }
"""


def test_customer_update_generates_event_when_deactivating(
    staff_api_client, staff_user, customer_user, address, permission_manage_users
):
    query = UPDATE_CUSTOMER_IS_ACTIVE_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)

    variables = {"id": user_id, "isActive": False}
    staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    account_deactivated_event = account_events.CustomerEvent.objects.get()
    assert (
        account_deactivated_event.type
        == account_events.CustomerEvents.ACCOUNT_DEACTIVATED
    )
    assert account_deactivated_event.user.pk == staff_user.pk
    assert account_deactivated_event.parameters == {"account_id": customer_user.id}


def test_customer_update_generates_event_when_activating(
    staff_api_client, staff_user, customer_user, address, permission_manage_users
):
    customer_user.is_active = False
    customer_user.save(update_fields=["is_active"])

    query = UPDATE_CUSTOMER_IS_ACTIVE_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)

    variables = {"id": user_id, "isActive": True}
    staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    account_activated_event = account_events.CustomerEvent.objects.get()
    assert (
        account_activated_event.type == account_events.CustomerEvents.ACCOUNT_ACTIVATED
    )
    assert account_activated_event.user.pk == staff_user.pk
    assert account_activated_event.parameters == {"account_id": customer_user.id}


def test_customer_update_generates_event_when_deactivating_as_app(
    app_api_client, staff_user, customer_user, address, permission_manage_users
):
    query = UPDATE_CUSTOMER_IS_ACTIVE_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)

    variables = {"id": user_id, "isActive": False}
    app_api_client.post_graphql(query, variables, permissions=[permission_manage_users])

    account_deactivated_event = account_events.CustomerEvent.objects.get()
    assert (
        account_deactivated_event.type
        == account_events.CustomerEvents.ACCOUNT_DEACTIVATED
    )
    assert account_deactivated_event.user is None
    assert account_deactivated_event.app.pk == app_api_client.app.pk
    assert account_deactivated_event.parameters == {"account_id": customer_user.id}


def test_customer_update_generates_event_when_activating_as_app(
    app_api_client, staff_user, customer_user, address, permission_manage_users
):
    customer_user.is_active = False
    customer_user.save(update_fields=["is_active"])

    query = UPDATE_CUSTOMER_IS_ACTIVE_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)

    variables = {"id": user_id, "isActive": True}
    app_api_client.post_graphql(query, variables, permissions=[permission_manage_users])

    account_activated_event = account_events.CustomerEvent.objects.get()
    assert (
        account_activated_event.type == account_events.CustomerEvents.ACCOUNT_ACTIVATED
    )
    assert account_activated_event.user is None
    assert account_activated_event.app.pk == app_api_client.app.pk
    assert account_activated_event.parameters == {"account_id": customer_user.id}


def test_customer_update_without_any_changes_generates_no_event(
    staff_api_client, customer_user, address, permission_manage_users
):
    query = UPDATE_CUSTOMER_EMAIL_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)
    address_data = convert_dict_keys_to_camel_case(address.as_data())

    new_street_address = "Updated street address"
    address_data["streetAddress1"] = new_street_address

    variables = {
        "id": user_id,
        "firstName": customer_user.first_name,
        "lastName": customer_user.last_name,
        "email": customer_user.email,
    }
    staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    # No event should have been generated
    assert not account_events.CustomerEvent.objects.exists()


def test_customer_update_generates_event_when_changing_email_by_app(
    app_api_client, staff_user, customer_user, address, permission_manage_users
):
    query = UPDATE_CUSTOMER_EMAIL_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)
    address_data = convert_dict_keys_to_camel_case(address.as_data())

    new_street_address = "Updated street address"
    address_data["streetAddress1"] = new_street_address

    variables = {
        "id": user_id,
        "firstName": customer_user.first_name,
        "lastName": customer_user.last_name,
        "email": "mirumee@example.com",
    }
    app_api_client.post_graphql(query, variables, permissions=[permission_manage_users])

    # The email was changed, an event should have been triggered
    email_changed_event = account_events.CustomerEvent.objects.get()
    assert email_changed_event.type == account_events.CustomerEvents.EMAIL_ASSIGNED
    assert email_changed_event.user is None
    assert email_changed_event.parameters == {"message": "mirumee@example.com"}


def test_customer_update_assign_gift_cards_and_orders(
    staff_api_client,
    staff_user,
    customer_user,
    address,
    gift_card,
    order,
    permission_manage_users,
):
    # given
    query = UPDATE_CUSTOMER_EMAIL_MUTATION

    user_id = graphene.Node.to_global_id("User", customer_user.id)
    address_data = convert_dict_keys_to_camel_case(address.as_data())

    new_street_address = "Updated street address"
    address_data["streetAddress1"] = new_street_address
    new_email = "mirumee@example.com"

    gift_card.created_by = None
    gift_card.created_by_email = new_email
    gift_card.save(update_fields=["created_by", "created_by_email"])

    order.user = None
    order.user_email = new_email
    order.save(update_fields=["user_email", "user"])

    variables = {
        "id": user_id,
        "firstName": customer_user.first_name,
        "lastName": customer_user.last_name,
        "email": new_email,
    }

    # when
    staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    # then
    email_changed_event = account_events.CustomerEvent.objects.get()
    assert email_changed_event.type == account_events.CustomerEvents.EMAIL_ASSIGNED
    gift_card.refresh_from_db()
    customer_user.refresh_from_db()
    assert gift_card.created_by == customer_user
    assert gift_card.created_by_email == customer_user.email
    order.refresh_from_db()
    assert order.user == customer_user


ACCOUNT_UPDATE_QUERY = """
    mutation accountUpdate(
        $billing: AddressInput
        $shipping: AddressInput
        $firstName: String,
        $lastName: String
        $languageCode: LanguageCodeEnum
    ) {
        accountUpdate(
          input: {
            defaultBillingAddress: $billing,
            defaultShippingAddress: $shipping,
            firstName: $firstName,
            lastName: $lastName,
            languageCode: $languageCode
        }) {
            errors {
                field
                code
                message
                addressType
            }
            user {
                firstName
                lastName
                email
                defaultBillingAddress {
                    id
                }
                defaultShippingAddress {
                    id
                }
                languageCode
            }
        }
    }
"""


def test_logged_customer_updates_language_code(user_api_client):
    language_code = "PL"
    user = user_api_client.user
    assert user.language_code != language_code
    variables = {"languageCode": language_code}

    response = user_api_client.post_graphql(ACCOUNT_UPDATE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountUpdate"]

    assert not data["errors"]
    assert data["user"]["languageCode"] == language_code
    user.refresh_from_db()
    assert user.language_code == language_code.lower()
    assert user.search_document


def test_logged_customer_update_names(user_api_client):
    first_name = "first"
    last_name = "last"
    user = user_api_client.user
    assert user.first_name != first_name
    assert user.last_name != last_name

    variables = {"firstName": first_name, "lastName": last_name}
    response = user_api_client.post_graphql(ACCOUNT_UPDATE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountUpdate"]

    user.refresh_from_db()
    assert not data["errors"]
    assert user.first_name == first_name
    assert user.last_name == last_name


def test_logged_customer_update_addresses(user_api_client, graphql_address_data):
    # this test requires addresses to be set and checks whether new address
    # instances weren't created, but the existing ones got updated
    user = user_api_client.user
    new_first_name = graphql_address_data["firstName"]
    assert user.default_billing_address
    assert user.default_shipping_address
    assert user.default_billing_address.first_name != new_first_name
    assert user.default_shipping_address.first_name != new_first_name

    query = ACCOUNT_UPDATE_QUERY
    mutation_name = "accountUpdate"
    variables = {"billing": graphql_address_data, "shipping": graphql_address_data}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert not data["errors"]

    # check that existing instances are updated
    billing_address_pk = user.default_billing_address.pk
    shipping_address_pk = user.default_shipping_address.pk
    user = User.objects.get(email=user.email)
    assert user.default_billing_address.pk == billing_address_pk
    assert user.default_shipping_address.pk == shipping_address_pk

    assert user.default_billing_address.first_name == new_first_name
    assert user.default_shipping_address.first_name == new_first_name
    assert user.search_document


def test_logged_customer_update_addresses_invalid_shipping_address(
    user_api_client, graphql_address_data
):
    shipping_address = graphql_address_data.copy()
    del shipping_address["country"]

    query = ACCOUNT_UPDATE_QUERY
    mutation_name = "accountUpdate"
    variables = {"billing": graphql_address_data, "shipping": shipping_address}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert len(data["errors"]) == 1
    errors = data["errors"]
    assert errors[0]["field"] == "country"
    assert errors[0]["code"] == AccountErrorCode.REQUIRED.name
    assert errors[0]["addressType"] == AddressType.SHIPPING.upper()


def test_logged_customer_update_addresses_invalid_billing_address(
    user_api_client, graphql_address_data
):
    billing_address = graphql_address_data.copy()
    del billing_address["country"]

    query = ACCOUNT_UPDATE_QUERY
    mutation_name = "accountUpdate"
    variables = {"billing": billing_address, "shipping": graphql_address_data}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert len(data["errors"]) == 1
    errors = data["errors"]
    assert errors[0]["field"] == "country"
    assert errors[0]["code"] == AccountErrorCode.REQUIRED.name
    assert errors[0]["addressType"] == AddressType.BILLING.upper()


def test_logged_customer_update_anonymous_user(api_client):
    query = ACCOUNT_UPDATE_QUERY
    response = api_client.post_graphql(query, {})
    assert_no_permission(response)


ACCOUNT_REQUEST_DELETION_MUTATION = """
    mutation accountRequestDeletion($redirectUrl: String!, $channel: String) {
        accountRequestDeletion(redirectUrl: $redirectUrl, channel: $channel) {
            errors {
                field
                code
                message
            }
        }
    }
"""


@patch("saleor.account.notifications.account_delete_token_generator.make_token")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_deletion(
    mocked_notify, mocked_token, user_api_client, channel_PLN, site_settings
):
    mocked_token.return_value = "token"
    user = user_api_client.user
    redirect_url = "https://www.example.com"
    variables = {"redirectUrl": redirect_url, "channel": channel_PLN.slug}
    response = user_api_client.post_graphql(
        ACCOUNT_REQUEST_DELETION_MUTATION, variables
    )
    content = get_graphql_content(response)
    data = content["data"]["accountRequestDeletion"]
    assert not data["errors"]
    params = urlencode({"token": "token"})
    delete_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(user),
        "delete_url": delete_url,
        "token": "token",
        "recipient_email": user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_DELETE,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_deletion_token_validation(
    mocked_notify, user_api_client, channel_PLN, site_settings
):
    user = user_api_client.user
    token = account_delete_token_generator.make_token(user)
    redirect_url = "https://www.example.com"
    variables = {"redirectUrl": redirect_url, "channel": channel_PLN.slug}
    response = user_api_client.post_graphql(
        ACCOUNT_REQUEST_DELETION_MUTATION, variables
    )
    content = get_graphql_content(response)
    data = content["data"]["accountRequestDeletion"]
    assert not data["errors"]
    params = urlencode({"token": token})
    delete_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(user),
        "delete_url": delete_url,
        "token": token,
        "recipient_email": user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_DELETE,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_deletion_anonymous_user(mocked_notify, api_client):
    variables = {"redirectUrl": "https://www.example.com"}
    response = api_client.post_graphql(ACCOUNT_REQUEST_DELETION_MUTATION, variables)
    assert_no_permission(response)
    mocked_notify.assert_not_called()


@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_deletion_storefront_hosts_not_allowed(
    mocked_notify, user_api_client
):
    variables = {"redirectUrl": "https://www.fake.com"}
    response = user_api_client.post_graphql(
        ACCOUNT_REQUEST_DELETION_MUTATION, variables
    )
    content = get_graphql_content(response)
    data = content["data"]["accountRequestDeletion"]
    assert len(data["errors"]) == 1
    assert data["errors"][0] == {
        "field": "redirectUrl",
        "code": AccountErrorCode.INVALID.name,
        "message": ANY,
    }
    mocked_notify.assert_not_called()


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_deletion_all_storefront_hosts_allowed(
    mocked_notify, user_api_client, settings, channel_PLN, site_settings
):
    user = user_api_client.user
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    token = account_delete_token_generator.make_token(user)
    settings.ALLOWED_CLIENT_HOSTS = ["*"]
    redirect_url = "https://www.test.com"
    variables = {"redirectUrl": redirect_url, "channel": channel_PLN.slug}
    response = user_api_client.post_graphql(
        ACCOUNT_REQUEST_DELETION_MUTATION, variables
    )
    content = get_graphql_content(response)
    data = content["data"]["accountRequestDeletion"]
    assert not data["errors"]

    params = urlencode({"token": token})
    delete_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(user),
        "delete_url": delete_url,
        "token": token,
        "recipient_email": user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_DELETE,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_deletion_subdomain(
    mocked_notify, user_api_client, settings, channel_PLN, site_settings
):
    user = user_api_client.user
    token = account_delete_token_generator.make_token(user)
    settings.ALLOWED_CLIENT_HOSTS = [".example.com"]
    redirect_url = "https://sub.example.com"
    variables = {"redirectUrl": redirect_url, "channel": channel_PLN.slug}
    response = user_api_client.post_graphql(
        ACCOUNT_REQUEST_DELETION_MUTATION, variables
    )
    content = get_graphql_content(response)
    data = content["data"]["accountRequestDeletion"]
    assert not data["errors"]
    params = urlencode({"token": token})
    delete_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(user),
        "delete_url": delete_url,
        "token": token,
        "recipient_email": user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_DELETE,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


ACCOUNT_DELETE_MUTATION = """
    mutation AccountDelete($token: String!){
        accountDelete(token: $token){
            errors{
                field
                message
            }
        }
    }
"""


@patch("saleor.core.tasks.delete_from_storage_task.delay")
@freeze_time("2018-05-31 12:00:01")
def test_account_delete(delete_from_storage_task_mock, user_api_client, media_root):
    # given
    thumbnail_mock = MagicMock(spec=File)
    thumbnail_mock.name = "image.jpg"

    user = user_api_client.user
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    user_id = user.id

    # create thumbnail
    thumbnail = Thumbnail.objects.create(user=user, size=128, image=thumbnail_mock)
    assert user.thumbnails.all()
    img_path = thumbnail.image.name

    token = account_delete_token_generator.make_token(user)
    variables = {"token": token}

    # when
    response = user_api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)

    # then
    content = get_graphql_content(response)
    data = content["data"]["accountDelete"]
    assert not data["errors"]
    assert not User.objects.filter(pk=user.id).exists()
    # ensure all related thumbnails have been deleted
    assert not Thumbnail.objects.filter(user_id=user_id).exists()
    delete_from_storage_task_mock.assert_called_once_with(img_path)


@freeze_time("2018-05-31 12:00:01")
def test_account_delete_user_never_log_in(user_api_client):
    user = user_api_client.user
    token = account_delete_token_generator.make_token(user)
    variables = {"token": token}

    response = user_api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountDelete"]
    assert not data["errors"]
    assert not User.objects.filter(pk=user.id).exists()


@freeze_time("2018-05-31 12:00:01")
def test_account_delete_log_out_after_deletion_request(user_api_client):
    user = user_api_client.user
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    token = account_delete_token_generator.make_token(user)

    # simulate re-login
    user.last_login = timezone.now() + datetime.timedelta(hours=1)
    user.save(update_fields=["last_login"])

    variables = {"token": token}

    response = user_api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountDelete"]
    assert not data["errors"]
    assert not User.objects.filter(pk=user.id).exists()


def test_account_delete_invalid_token(user_api_client):
    user = user_api_client.user
    variables = {"token": "invalid"}

    response = user_api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountDelete"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["message"] == "Invalid or expired token."
    assert User.objects.filter(pk=user.id).exists()


def test_account_delete_anonymous_user(api_client):
    variables = {"token": "invalid"}

    response = api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)
    assert_no_permission(response)


def test_account_delete_staff_user(staff_api_client):
    user = staff_api_client.user
    variables = {"token": "invalid"}

    response = staff_api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountDelete"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["message"] == "Cannot delete a staff account."
    assert User.objects.filter(pk=user.id).exists()


@freeze_time("2018-05-31 12:00:01")
def test_account_delete_other_customer_token(user_api_client):
    user = user_api_client.user
    other_user = User.objects.create(email="temp@example.com")
    token = account_delete_token_generator.make_token(other_user)
    variables = {"token": token}

    response = user_api_client.post_graphql(ACCOUNT_DELETE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountDelete"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["message"] == "Invalid or expired token."
    assert User.objects.filter(pk=user.id).exists()
    assert User.objects.filter(pk=other_user.id).exists()


CUSTOMER_DELETE_MUTATION = """
    mutation CustomerDelete($id: ID, $externalReference: String) {
        customerDelete(id: $id, externalReference: $externalReference) {
            errors {
                field
                message
            }
            user {
                id
                externalReference
            }
        }
    }
"""


@patch("saleor.account.signals.delete_from_storage_task.delay")
@patch("saleor.graphql.account.utils.account_events.customer_deleted_event")
def test_customer_delete(
    mocked_deletion_event,
    delete_from_storage_task_mock,
    staff_api_client,
    staff_user,
    customer_user,
    image,
    permission_manage_users,
    media_root,
):
    """Ensure deleting a customer actually deletes the customer and creates proper
    related events"""

    query = CUSTOMER_DELETE_MUTATION
    customer_id = graphene.Node.to_global_id("User", customer_user.pk)
    customer_user.avatar = image
    customer_user.save(update_fields=["avatar"])
    variables = {"id": customer_id}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerDelete"]
    assert data["errors"] == []
    assert data["user"]["id"] == customer_id

    # Ensure the customer was properly deleted
    # and any related event was properly triggered
    mocked_deletion_event.assert_called_once_with(
        staff_user=staff_user, app=None, deleted_count=1
    )
    delete_from_storage_task_mock.assert_called_once_with(customer_user.avatar.name)


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_customer_delete_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    customer_user,
    permission_manage_users,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    customer_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {"id": customer_id}

    # when
    response = staff_api_client.post_graphql(
        CUSTOMER_DELETE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerDelete"]

    # then
    assert data["errors"] == []
    assert data["user"]["id"] == customer_id
    mocked_webhook_trigger.assert_called_once_with(
        generate_customer_payload(customer_user, staff_api_client.user),
        WebhookEventAsyncType.CUSTOMER_DELETED,
        [any_webhook],
        customer_user,
        SimpleLazyObject(lambda: staff_api_client.user),
    )


@patch("saleor.account.signals.delete_from_storage_task.delay")
@patch("saleor.graphql.account.utils.account_events.customer_deleted_event")
def test_customer_delete_by_app(
    mocked_deletion_event,
    delete_from_storage_task_mock,
    app_api_client,
    app,
    customer_user,
    image,
    permission_manage_users,
    media_root,
):
    """Ensure deleting a customer actually deletes the customer and creates proper
    related events"""

    query = CUSTOMER_DELETE_MUTATION
    customer_id = graphene.Node.to_global_id("User", customer_user.pk)
    customer_user.avatar = image
    customer_user.save(update_fields=["avatar"])
    variables = {"id": customer_id}
    response = app_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["customerDelete"]
    assert data["errors"] == []
    assert data["user"]["id"] == customer_id

    # Ensure the customer was properly deleted
    # and any related event was properly triggered
    assert mocked_deletion_event.call_count == 1
    args, kwargs = mocked_deletion_event.call_args
    assert kwargs["deleted_count"] == 1
    assert kwargs["staff_user"] is None
    assert kwargs["app"] == app
    delete_from_storage_task_mock.assert_called_once_with(customer_user.avatar.name)


def test_customer_delete_errors(customer_user, admin_user, staff_user):
    info = Mock(context=Mock(user=admin_user))
    with pytest.raises(ValidationError) as e:
        CustomerDelete.clean_instance(info, staff_user)

    msg = "Cannot delete a staff account."
    assert e.value.error_dict["id"][0].message == msg

    # should not raise any errors
    CustomerDelete.clean_instance(info, customer_user)


def test_customer_delete_by_external_reference(
    staff_api_client, customer_user, permission_manage_users
):
    # given
    user = customer_user
    query = CUSTOMER_DELETE_MUTATION
    ext_ref = "test-ext-ref"
    user.external_reference = ext_ref
    user.save(update_fields=["external_reference"])
    variables = {"externalReference": ext_ref}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["customerDelete"]
    with pytest.raises(user._meta.model.DoesNotExist):
        user.refresh_from_db()
    assert not data["errors"]
    assert data["user"]["externalReference"] == ext_ref
    assert data["user"]["id"] == graphene.Node.to_global_id("User", user.id)


def test_delete_customer_by_both_id_and_external_reference(
    staff_api_client, customer_user, permission_manage_users
):
    # given
    query = CUSTOMER_DELETE_MUTATION
    variables = {"externalReference": "whatever", "id": "whatever"}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    errors = content["data"]["customerDelete"]["errors"]
    assert (
        errors[0]["message"]
        == "Argument 'id' cannot be combined with 'external_reference'"
    )


def test_delete_customer_by_external_reference_not_existing(
    staff_api_client, customer_user, permission_manage_users
):
    # given
    query = CUSTOMER_DELETE_MUTATION
    ext_ref = "non-existing-ext-ref"
    variables = {"externalReference": ext_ref}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    errors = content["data"]["customerDelete"]["errors"]
    assert errors[0]["message"] == f"Couldn't resolve to a node: {ext_ref}"


STAFF_CREATE_MUTATION = """
    mutation CreateStaff(
            $email: String, $redirect_url: String, $add_groups: [ID!]
        ) {
        staffCreate(input: {email: $email, redirectUrl: $redirect_url,
            addGroups: $add_groups}
        ) {
            errors {
                field
                code
                permissions
                groups
            }
            user {
                id
                email
                isStaff
                isActive
                userPermissions {
                    code
                }
                permissionGroups {
                    name
                    permissions {
                        code
                    }
                }
                avatar {
                    url
                }
            }
        }
    }
"""


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_staff_create(
    mocked_notify,
    staff_api_client,
    staff_user,
    media_root,
    permission_group_manage_users,
    permission_manage_products,
    permission_manage_staff,
    permission_manage_users,
    channel_PLN,
    site_settings,
):
    group = permission_group_manage_users
    group.permissions.add(permission_manage_products)
    staff_user.user_permissions.add(permission_manage_products, permission_manage_users)
    email = "api_user@example.com"
    redirect_url = "https://www.example.com"
    variables = {
        "email": email,
        "redirect_url": redirect_url,
        "add_groups": [graphene.Node.to_global_id("Group", group.pk)],
    }

    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    assert data["errors"] == []
    assert data["user"]["email"] == email
    assert data["user"]["isStaff"]
    assert data["user"]["isActive"]

    expected_perms = {
        permission_manage_products.codename,
        permission_manage_users.codename,
    }
    permissions = data["user"]["userPermissions"]
    assert {perm["code"].lower() for perm in permissions} == expected_perms

    staff_user = User.objects.get(email=email)

    assert staff_user.is_staff
    assert staff_user.search_document == f"{email}\n".lower()

    groups = data["user"]["permissionGroups"]
    assert len(groups) == 1
    assert {perm["code"].lower() for perm in groups[0]["permissions"]} == expected_perms

    token = default_token_generator.make_token(staff_user)
    params = urlencode({"email": email, "token": token})
    password_set_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(staff_user),
        "password_set_url": password_set_url,
        "token": token,
        "recipient_email": staff_user.email,
        "channel_slug": None,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_SET_STAFF_PASSWORD,
        payload=expected_payload,
        channel_slug=None,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_promote_customer_to_staff_user(
    mocked_notify,
    staff_api_client,
    staff_user,
    customer_user,
    media_root,
    permission_group_manage_users,
    permission_manage_products,
    permission_manage_staff,
    permission_manage_users,
    channel_PLN,
):
    group = permission_group_manage_users
    group.permissions.add(permission_manage_products)
    staff_user.user_permissions.add(permission_manage_products, permission_manage_users)
    redirect_url = "https://www.example.com"
    email = customer_user.email
    variables = {
        "email": email,
        "redirect_url": redirect_url,
        "add_groups": [graphene.Node.to_global_id("Group", group.pk)],
    }

    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    assert data["errors"] == []
    assert data["user"]["email"] == email
    assert data["user"]["isStaff"]
    assert data["user"]["isActive"]

    expected_perms = {
        permission_manage_products.codename,
        permission_manage_users.codename,
    }
    permissions = data["user"]["userPermissions"]
    assert {perm["code"].lower() for perm in permissions} == expected_perms

    staff_user = User.objects.get(email=email)

    assert staff_user.is_staff

    groups = data["user"]["permissionGroups"]
    assert len(groups) == 1
    assert {perm["code"].lower() for perm in groups[0]["permissions"]} == expected_perms

    mocked_notify.assert_not_called()


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_staff_create_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    staff_user,
    permission_group_manage_users,
    permission_manage_staff,
    permission_manage_users,
    channel_PLN,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    staff_user.user_permissions.add(permission_manage_users)
    email = "api_user@example.com"
    redirect_url = "https://www.example.com"
    variables = {
        "email": email,
        "redirect_url": redirect_url,
        "add_groups": [
            graphene.Node.to_global_id("Group", permission_group_manage_users.pk)
        ],
    }

    # when
    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    new_staff_user = User.objects.get(email=email)

    # then
    assert not data["errors"]
    assert data["user"]
    expected_call = call(
        json.dumps(
            {
                "id": graphene.Node.to_global_id("User", new_staff_user.id),
                "email": email,
                "meta": generate_meta(
                    requestor_data=generate_requestor(
                        SimpleLazyObject(lambda: staff_api_client.user)
                    )
                ),
            },
            cls=CustomJsonEncoder,
        ),
        WebhookEventAsyncType.STAFF_CREATED,
        [any_webhook],
        new_staff_user,
        SimpleLazyObject(lambda: staff_api_client.user),
    )

    assert expected_call in mocked_webhook_trigger.call_args_list


def test_staff_create_app_no_permission(
    app_api_client,
    staff_user,
    media_root,
    permission_group_manage_users,
    permission_manage_products,
    permission_manage_staff,
    permission_manage_users,
):
    group = permission_group_manage_users
    group.permissions.add(permission_manage_products)
    staff_user.user_permissions.add(permission_manage_products, permission_manage_users)
    email = "api_user@example.com"
    variables = {
        "email": email,
        "redirect_url": "https://www.example.com",
        "add_groups": [graphene.Node.to_global_id("Group", group.pk)],
    }

    response = app_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )

    assert_no_permission(response)


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_staff_create_out_of_scope_group(
    mocked_notify,
    staff_api_client,
    superuser_api_client,
    media_root,
    permission_manage_staff,
    permission_manage_users,
    permission_group_manage_users,
    channel_PLN,
    site_settings,
):
    """Ensure user can't create staff with groups which are out of user scope.
    Ensure superuser pass restrictions.
    """
    group = permission_group_manage_users
    group2 = Group.objects.create(name="second group")
    group2.permissions.add(permission_manage_staff)
    email = "api_user@example.com"
    redirect_url = "https://www.example.com"
    variables = {
        "email": email,
        "redirect_url": redirect_url,
        "add_groups": [
            graphene.Node.to_global_id("Group", gr.pk) for gr in [group, group2]
        ],
    }

    # for staff user
    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    errors = data["errors"]
    assert not data["user"]
    assert len(errors) == 1

    expected_error = {
        "field": "addGroups",
        "code": AccountErrorCode.OUT_OF_SCOPE_GROUP.name,
        "permissions": None,
        "groups": [graphene.Node.to_global_id("Group", group.pk)],
    }

    assert errors[0] == expected_error

    mocked_notify.assert_not_called()

    # for superuser
    response = superuser_api_client.post_graphql(STAFF_CREATE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]

    assert data["errors"] == []
    assert data["user"]["email"] == email
    assert data["user"]["isStaff"]
    assert data["user"]["isActive"]
    expected_perms = {
        permission_manage_staff.codename,
        permission_manage_users.codename,
    }
    permissions = data["user"]["userPermissions"]
    assert {perm["code"].lower() for perm in permissions} == expected_perms

    staff_user = User.objects.get(email=email)

    assert staff_user.is_staff

    expected_groups = [
        {
            "name": group.name,
            "permissions": [{"code": permission_manage_users.codename.upper()}],
        },
        {
            "name": group2.name,
            "permissions": [{"code": permission_manage_staff.codename.upper()}],
        },
    ]
    groups = data["user"]["permissionGroups"]
    assert len(groups) == 2
    for group in expected_groups:
        assert group in groups
    token = default_token_generator.make_token(staff_user)
    params = urlencode({"email": email, "token": token})
    password_set_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(staff_user),
        "password_set_url": password_set_url,
        "token": token,
        "recipient_email": staff_user.email,
        "channel_slug": None,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_SET_STAFF_PASSWORD,
        payload=expected_payload,
        channel_slug=None,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_staff_create_send_password_with_url(
    mocked_notify, staff_api_client, media_root, permission_manage_staff, site_settings
):
    email = "api_user@example.com"
    redirect_url = "https://www.example.com"
    variables = {"email": email, "redirect_url": redirect_url}

    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    assert not data["errors"]

    staff_user = User.objects.get(email=email)
    assert staff_user.is_staff

    token = default_token_generator.make_token(staff_user)
    params = urlencode({"email": email, "token": token})
    password_set_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(staff_user),
        "password_set_url": password_set_url,
        "token": token,
        "recipient_email": staff_user.email,
        "channel_slug": None,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_SET_STAFF_PASSWORD,
        payload=expected_payload,
        channel_slug=None,
    )


def test_staff_create_without_send_password(
    staff_api_client, media_root, permission_manage_staff
):
    email = "api_user@example.com"
    variables = {"email": email}
    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    assert not data["errors"]
    User.objects.get(email=email)


def test_staff_create_with_invalid_url(
    staff_api_client, media_root, permission_manage_staff
):
    email = "api_user@example.com"
    variables = {"email": email, "redirect_url": "invalid"}
    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    assert data["errors"][0] == {
        "field": "redirectUrl",
        "code": AccountErrorCode.INVALID.name,
        "permissions": None,
        "groups": None,
    }
    staff_user = User.objects.filter(email=email)
    assert not staff_user


def test_staff_create_with_not_allowed_url(
    staff_api_client, media_root, permission_manage_staff
):
    email = "api_userrr@example.com"
    variables = {"email": email, "redirect_url": "https://www.fake.com"}
    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffCreate"]
    assert data["errors"][0] == {
        "field": "redirectUrl",
        "code": AccountErrorCode.INVALID.name,
        "permissions": None,
        "groups": None,
    }
    staff_user = User.objects.filter(email=email)
    assert not staff_user


def test_staff_create_with_upper_case_email(
    staff_api_client, media_root, permission_manage_staff
):
    # given
    email = "api_user@example.com"
    variables = {"email": email}

    # when
    response = staff_api_client.post_graphql(
        STAFF_CREATE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["staffCreate"]
    assert not data["errors"]
    assert data["user"]["email"] == email.lower()


STAFF_UPDATE_MUTATIONS = """
    mutation UpdateStaff(
            $id: ID!, $input: StaffUpdateInput!) {
        staffUpdate(
                id: $id,
                input: $input) {
            errors {
                field
                code
                message
                permissions
                groups
            }
            user {
                userPermissions {
                    code
                }
                permissionGroups {
                    name
                }
                isActive
                email
            }
        }
    }
"""


def test_staff_update(staff_api_client, permission_manage_staff, media_root):
    query = STAFF_UPDATE_MUTATIONS
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    assert not staff_user.search_document
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": id, "input": {"isActive": False}}

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )

    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["errors"] == []
    assert data["user"]["userPermissions"] == []
    assert not data["user"]["isActive"]
    staff_user.refresh_from_db()
    assert not staff_user.search_document


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_staff_update_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    permission_manage_staff,
    media_root,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    assert not staff_user.search_document
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": id, "input": {"isActive": False}}

    # when
    response = staff_api_client.post_graphql(
        STAFF_UPDATE_MUTATIONS, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]

    # then
    assert not data["errors"]
    assert data["user"]
    mocked_webhook_trigger.assert_called_once_with(
        json.dumps(
            {
                "id": graphene.Node.to_global_id("User", staff_user.id),
                "email": staff_user.email,
                "meta": generate_meta(
                    requestor_data=generate_requestor(
                        SimpleLazyObject(lambda: staff_api_client.user)
                    )
                ),
            },
            cls=CustomJsonEncoder,
        ),
        WebhookEventAsyncType.STAFF_UPDATED,
        [any_webhook],
        staff_user,
        SimpleLazyObject(lambda: staff_api_client.user),
    )


def test_staff_update_email(staff_api_client, permission_manage_staff, media_root):
    query = STAFF_UPDATE_MUTATIONS
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    assert not staff_user.search_document
    id = graphene.Node.to_global_id("User", staff_user.id)
    new_email = "test@email.com"
    variables = {"id": id, "input": {"email": new_email}}

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )

    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["errors"] == []
    assert data["user"]["userPermissions"] == []
    assert data["user"]["isActive"]
    staff_user.refresh_from_db()
    assert staff_user.search_document == f"{new_email}\n"


@pytest.mark.parametrize("field", ["firstName", "lastName"])
def test_staff_update_name_field(
    field, staff_api_client, permission_manage_staff, media_root
):
    query = STAFF_UPDATE_MUTATIONS
    email = "staffuser@example.com"
    staff_user = User.objects.create(email=email, is_staff=True)
    assert not staff_user.search_document
    id = graphene.Node.to_global_id("User", staff_user.id)
    value = "Name"
    variables = {"id": id, "input": {field: value}}

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )

    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["errors"] == []
    assert data["user"]["userPermissions"] == []
    assert data["user"]["isActive"]
    staff_user.refresh_from_db()
    assert staff_user.search_document == f"{email}\n{value.lower()}\n"


def test_staff_update_app_no_permission(
    app_api_client, permission_manage_staff, media_root
):
    query = STAFF_UPDATE_MUTATIONS
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": id, "input": {"isActive": False}}

    response = app_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )

    assert_no_permission(response)


def test_staff_update_groups_and_permissions(
    staff_api_client,
    media_root,
    permission_manage_staff,
    permission_manage_users,
    permission_manage_orders,
    permission_manage_products,
):
    query = STAFF_UPDATE_MUTATIONS
    groups = Group.objects.bulk_create(
        [Group(name="manage users"), Group(name="manage orders"), Group(name="empty")]
    )
    group1, group2, group3 = groups
    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_orders)

    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    staff_user.groups.add(group1)

    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {
        "id": id,
        "input": {
            "addGroups": [
                graphene.Node.to_global_id("Group", gr.pk) for gr in [group2, group3]
            ],
            "removeGroups": [graphene.Node.to_global_id("Group", group1.pk)],
        },
    }

    staff_api_client.user.user_permissions.add(
        permission_manage_users, permission_manage_orders, permission_manage_products
    )

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["errors"] == []
    assert {perm["code"].lower() for perm in data["user"]["userPermissions"]} == {
        permission_manage_orders.codename,
    }
    assert {group["name"] for group in data["user"]["permissionGroups"]} == {
        group2.name,
        group3.name,
    }


def test_staff_update_out_of_scope_user(
    staff_api_client,
    superuser_api_client,
    permission_manage_staff,
    permission_manage_orders,
    media_root,
):
    """Ensure that staff user cannot update user with wider scope of permission.
    Ensure superuser pass restrictions.
    """
    query = STAFF_UPDATE_MUTATIONS
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    staff_user.user_permissions.add(permission_manage_orders)
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": id, "input": {"isActive": False}}

    # for staff user
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert not data["user"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["field"] == "id"
    assert data["errors"][0]["code"] == AccountErrorCode.OUT_OF_SCOPE_USER.name

    # for superuser
    response = superuser_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["user"]["email"] == staff_user.email
    assert data["user"]["isActive"] is False
    assert not data["errors"]


def test_staff_update_out_of_scope_groups(
    staff_api_client,
    superuser_api_client,
    permission_manage_staff,
    media_root,
    permission_manage_users,
    permission_manage_orders,
    permission_manage_products,
):
    """Ensure that staff user cannot add to groups which permission scope is wider
    than user's scope.
    Ensure superuser pass restrictions.
    """
    query = STAFF_UPDATE_MUTATIONS

    groups = Group.objects.bulk_create(
        [
            Group(name="manage users"),
            Group(name="manage orders"),
            Group(name="manage products"),
        ]
    )
    group1, group2, group3 = groups

    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_orders)
    group3.permissions.add(permission_manage_products)

    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    staff_api_client.user.user_permissions.add(permission_manage_orders)
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {
        "id": id,
        "input": {
            "isActive": False,
            "addGroups": [
                graphene.Node.to_global_id("Group", gr.pk) for gr in [group1, group2]
            ],
            "removeGroups": [graphene.Node.to_global_id("Group", group3.pk)],
        },
    }

    # for staff user
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    errors = data["errors"]
    assert not data["user"]
    assert len(errors) == 2

    expected_errors = [
        {
            "field": "addGroups",
            "code": AccountErrorCode.OUT_OF_SCOPE_GROUP.name,
            "permissions": None,
            "groups": [graphene.Node.to_global_id("Group", group1.pk)],
        },
        {
            "field": "removeGroups",
            "code": AccountErrorCode.OUT_OF_SCOPE_GROUP.name,
            "permissions": None,
            "groups": [graphene.Node.to_global_id("Group", group3.pk)],
        },
    ]
    for error in errors:
        error.pop("message")
        assert error in expected_errors

    # for superuser
    response = superuser_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    errors = data["errors"]
    assert not errors
    assert data["user"]["email"] == staff_user.email
    assert {group["name"] for group in data["user"]["permissionGroups"]} == {
        group1.name,
        group2.name,
    }


def test_staff_update_duplicated_input_items(
    staff_api_client,
    permission_manage_staff,
    media_root,
    permission_manage_orders,
    permission_manage_users,
):
    query = STAFF_UPDATE_MUTATIONS

    groups = Group.objects.bulk_create(
        [Group(name="manage users"), Group(name="manage orders"), Group(name="empty")]
    )
    group1, group2, group3 = groups

    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_orders)

    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    staff_api_client.user.user_permissions.add(
        permission_manage_orders, permission_manage_users
    )
    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {
        "id": id,
        "input": {
            "addGroups": [
                graphene.Node.to_global_id("Group", gr.pk) for gr in [group1, group2]
            ],
            "removeGroups": [
                graphene.Node.to_global_id("Group", gr.pk)
                for gr in [group1, group2, group3]
            ],
        },
    }

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    errors = data["errors"]

    assert len(errors) == 1
    assert errors[0]["field"] is None
    assert errors[0]["code"] == AccountErrorCode.DUPLICATED_INPUT_ITEM.name
    assert set(errors[0]["groups"]) == {
        graphene.Node.to_global_id("Group", gr.pk) for gr in [group1, group2]
    }
    assert errors[0]["permissions"] is None


def test_staff_update_doesnt_change_existing_avatar(
    staff_api_client,
    permission_manage_staff,
    media_root,
    staff_users,
):
    query = STAFF_UPDATE_MUTATIONS

    mock_file = MagicMock(spec=File)
    mock_file.name = "image.jpg"

    staff_user, staff_user1, _ = staff_users

    id = graphene.Node.to_global_id("User", staff_user1.id)
    variables = {"id": id, "input": {"isActive": False}}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["errors"] == []

    staff_user.refresh_from_db()
    assert not staff_user.avatar


def test_staff_update_deactivate_with_manage_staff_left_not_manageable_perms(
    staff_api_client,
    superuser_api_client,
    staff_users,
    permission_manage_users,
    permission_manage_staff,
    permission_manage_orders,
    media_root,
):
    """Ensure that staff user can't and superuser can deactivate user where some
    permissions will be not manageable.
    """
    query = STAFF_UPDATE_MUTATIONS
    groups = Group.objects.bulk_create(
        [
            Group(name="manage users"),
            Group(name="manage staff"),
            Group(name="manage orders"),
        ]
    )
    group1, group2, group3 = groups

    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_staff)
    group3.permissions.add(permission_manage_orders)

    staff_user, staff_user1, staff_user2 = staff_users
    group1.user_set.add(staff_user1)
    group2.user_set.add(staff_user2, staff_user1)
    group3.user_set.add(staff_user2)

    staff_user.user_permissions.add(permission_manage_users, permission_manage_orders)

    id = graphene.Node.to_global_id("User", staff_user1.id)
    variables = {"id": id, "input": {"isActive": False}}

    # for staff user
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    errors = data["errors"]

    assert not data["user"]
    assert len(errors) == 1
    assert errors[0]["field"] == "isActive"
    assert errors[0]["code"] == AccountErrorCode.LEFT_NOT_MANAGEABLE_PERMISSION.name
    assert len(errors[0]["permissions"]) == 1
    assert errors[0]["permissions"][0] == AccountPermissions.MANAGE_USERS.name

    # for superuser
    response = superuser_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    errors = data["errors"]

    staff_user1.refresh_from_db()
    assert data["user"]["email"] == staff_user1.email
    assert data["user"]["isActive"] is False
    assert not errors
    assert not staff_user1.is_active


def test_staff_update_deactivate_with_manage_staff_all_perms_manageable(
    staff_api_client,
    staff_users,
    permission_manage_users,
    permission_manage_staff,
    permission_manage_orders,
    media_root,
):
    query = STAFF_UPDATE_MUTATIONS
    groups = Group.objects.bulk_create(
        [
            Group(name="manage users"),
            Group(name="manage staff"),
            Group(name="manage orders"),
        ]
    )
    group1, group2, group3 = groups

    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_staff)
    group3.permissions.add(permission_manage_orders)

    staff_user, staff_user1, staff_user2 = staff_users
    group1.user_set.add(staff_user1, staff_user2)
    group2.user_set.add(staff_user2, staff_user1)
    group3.user_set.add(staff_user2)

    staff_user.user_permissions.add(permission_manage_users, permission_manage_orders)

    id = graphene.Node.to_global_id("User", staff_user1.id)
    variables = {"id": id, "input": {"isActive": False}}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    errors = data["errors"]

    staff_user1.refresh_from_db()
    assert not errors
    assert staff_user1.is_active is False


def test_staff_update_update_email_assign_gift_cards_and_orders(
    staff_api_client, permission_manage_staff, gift_card, order
):
    # given
    query = STAFF_UPDATE_MUTATIONS
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)

    new_email = "testuser@example.com"

    gift_card.created_by = None
    gift_card.created_by_email = new_email
    gift_card.save(update_fields=["created_by", "created_by_email"])

    order.user = None
    order.user_email = new_email
    order.save(update_fields=["user_email", "user"])

    id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": id, "input": {"email": new_email}}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["staffUpdate"]
    assert data["errors"] == []
    assert data["user"]["userPermissions"] == []
    assert data["user"]["email"] == new_email
    gift_card.refresh_from_db()
    staff_user.refresh_from_db()
    assert gift_card.created_by == staff_user
    assert gift_card.created_by_email == staff_user.email
    order.refresh_from_db()
    assert order.user == staff_user


STAFF_DELETE_MUTATION = """
        mutation DeleteStaff($id: ID!) {
            staffDelete(id: $id) {
                errors {
                    field
                    code
                    message
                    permissions
                }
                user {
                    id
                }
            }
        }
    """


def test_staff_delete(staff_api_client, permission_manage_staff):
    query = STAFF_DELETE_MUTATION
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    user_id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": user_id}

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]
    assert data["errors"] == []
    assert not User.objects.filter(pk=staff_user.id).exists()


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_staff_delete_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    permission_manage_staff,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    user_id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": user_id}

    # when
    response = staff_api_client.post_graphql(
        STAFF_DELETE_MUTATION, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]

    # then
    assert not data["errors"]
    assert not User.objects.filter(pk=staff_user.id).exists()
    mocked_webhook_trigger.assert_called_once_with(
        json.dumps(
            {
                "id": graphene.Node.to_global_id("User", staff_user.id),
                "email": staff_user.email,
                "meta": generate_meta(
                    requestor_data=generate_requestor(
                        SimpleLazyObject(lambda: staff_api_client.user)
                    )
                ),
            },
            cls=CustomJsonEncoder,
        ),
        WebhookEventAsyncType.STAFF_DELETED,
        [any_webhook],
        staff_user,
        SimpleLazyObject(lambda: staff_api_client.user),
    )


@patch("saleor.account.signals.delete_from_storage_task.delay")
def test_staff_delete_with_avatar(
    delete_from_storage_task_mock,
    staff_api_client,
    image,
    permission_manage_staff,
    media_root,
):
    query = STAFF_DELETE_MUTATION
    staff_user = User.objects.create(
        email="staffuser@example.com", avatar=image, is_staff=True
    )
    user_id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": user_id}

    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]
    assert data["errors"] == []
    assert not User.objects.filter(pk=staff_user.id).exists()
    delete_from_storage_task_mock.assert_called_once_with(staff_user.avatar.name)


def test_staff_delete_app_no_permission(app_api_client, permission_manage_staff):
    query = STAFF_DELETE_MUTATION
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    user_id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": user_id}

    response = app_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )

    assert_no_permission(response)


def test_staff_delete_out_of_scope_user(
    staff_api_client,
    superuser_api_client,
    permission_manage_staff,
    permission_manage_products,
):
    """Ensure staff user cannot delete users even when some of user permissions are
    out of requestor scope.
    Ensure superuser pass restrictions.
    """
    query = STAFF_DELETE_MUTATION
    staff_user = User.objects.create(email="staffuser@example.com", is_staff=True)
    staff_user.user_permissions.add(permission_manage_products)
    user_id = graphene.Node.to_global_id("User", staff_user.id)
    variables = {"id": user_id}

    # for staff user
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]
    assert not data["user"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["field"] == "id"
    assert data["errors"][0]["code"] == AccountErrorCode.OUT_OF_SCOPE_USER.name

    # for superuser
    response = superuser_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]

    assert data["errors"] == []
    assert not User.objects.filter(pk=staff_user.id).exists()


def test_staff_delete_left_not_manageable_permissions(
    staff_api_client,
    superuser_api_client,
    staff_users,
    permission_manage_staff,
    permission_manage_users,
    permission_manage_orders,
):
    """Ensure staff user can't and superuser can delete staff user when some of
    permissions will be not manageable.
    """
    query = STAFF_DELETE_MUTATION
    groups = Group.objects.bulk_create(
        [
            Group(name="manage users"),
            Group(name="manage staff"),
            Group(name="manage orders"),
        ]
    )
    group1, group2, group3 = groups

    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_staff)
    group3.permissions.add(permission_manage_orders)

    staff_user, staff_user1, staff_user2 = staff_users
    group1.user_set.add(staff_user1)
    group2.user_set.add(staff_user2, staff_user1)
    group3.user_set.add(staff_user1)

    user_id = graphene.Node.to_global_id("User", staff_user1.id)
    variables = {"id": user_id}

    # for staff user
    staff_user.user_permissions.add(permission_manage_users, permission_manage_orders)
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]
    errors = data["errors"]

    assert len(errors) == 1
    assert errors[0]["field"] == "id"
    assert errors[0]["code"] == AccountErrorCode.LEFT_NOT_MANAGEABLE_PERMISSION.name
    assert set(errors[0]["permissions"]) == {
        AccountPermissions.MANAGE_USERS.name,
        OrderPermissions.MANAGE_ORDERS.name,
    }
    assert User.objects.filter(pk=staff_user1.id).exists()

    # for superuser
    staff_user.user_permissions.add(permission_manage_users, permission_manage_orders)
    response = superuser_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]
    errors = data["errors"]

    assert not errors
    assert not User.objects.filter(pk=staff_user1.id).exists()


def test_staff_delete_all_permissions_manageable(
    staff_api_client,
    staff_users,
    permission_manage_staff,
    permission_manage_users,
    permission_manage_orders,
):
    query = STAFF_DELETE_MUTATION
    groups = Group.objects.bulk_create(
        [
            Group(name="manage users"),
            Group(name="manage staff"),
            Group(name="manage users and orders"),
        ]
    )
    group1, group2, group3 = groups

    group1.permissions.add(permission_manage_users)
    group2.permissions.add(permission_manage_staff)
    group3.permissions.add(permission_manage_users, permission_manage_orders)

    staff_user, staff_user1, staff_user2 = staff_users
    group1.user_set.add(staff_user1)
    group2.user_set.add(staff_user2)
    group3.user_set.add(staff_user1)

    user_id = graphene.Node.to_global_id("User", staff_user1.id)
    variables = {"id": user_id}

    staff_user.user_permissions.add(permission_manage_users, permission_manage_orders)
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    data = content["data"]["staffDelete"]
    errors = data["errors"]

    assert len(errors) == 0
    assert not User.objects.filter(pk=staff_user1.id).exists()


def test_user_delete_errors(staff_user, admin_user):
    info = Mock(context=Mock(user=staff_user))
    with pytest.raises(ValidationError) as e:
        UserDelete.clean_instance(info, staff_user)

    msg = "You cannot delete your own account."
    assert e.value.error_dict["id"][0].message == msg

    info = Mock(context=Mock(user=staff_user))
    with pytest.raises(ValidationError) as e:
        UserDelete.clean_instance(info, admin_user)

    msg = "Cannot delete this account."
    assert e.value.error_dict["id"][0].message == msg


def test_staff_delete_errors(staff_user, customer_user, admin_user):
    info = Mock(context=Mock(user=staff_user, app=None))
    with pytest.raises(ValidationError) as e:
        StaffDelete.clean_instance(info, customer_user)
    msg = "Cannot delete a non-staff users."
    assert e.value.error_dict["id"][0].message == msg

    # should not raise any errors
    info = Mock(context=Mock(user=admin_user, app=None))
    StaffDelete.clean_instance(info, staff_user)


def test_staff_update_errors(staff_user, customer_user, admin_user):
    errors = defaultdict(list)
    input = {"is_active": None}
    StaffUpdate.clean_is_active(input, customer_user, staff_user, errors)
    assert not errors["is_active"]

    input["is_active"] = False
    StaffUpdate.clean_is_active(input, staff_user, staff_user, errors)
    assert len(errors["is_active"]) == 1
    assert (
        errors["is_active"][0].code.upper()
        == AccountErrorCode.DEACTIVATE_OWN_ACCOUNT.name
    )

    errors = defaultdict(list)
    StaffUpdate.clean_is_active(input, admin_user, staff_user, errors)
    assert len(errors["is_active"]) == 2
    assert {error.code.upper() for error in errors["is_active"]} == {
        AccountErrorCode.DEACTIVATE_SUPERUSER_ACCOUNT.name,
        AccountErrorCode.LEFT_NOT_MANAGEABLE_PERMISSION.name,
    }

    errors = defaultdict(list)
    # should not raise any errors
    StaffUpdate.clean_is_active(input, customer_user, staff_user, errors)
    assert not errors["is_active"]


SET_PASSWORD_MUTATION = """
    mutation SetPassword($email: String!, $token: String!, $password: String!) {
        setPassword(email: $email, token: $token, password: $password) {
            errors {
                field
                message
            }
            errors {
                field
                message
                code
            }
            user {
                id
            }
            token
            refreshToken
        }
    }
"""


@freeze_time("2018-05-31 12:00:01")
def test_set_password(user_api_client, customer_user):
    token = default_token_generator.make_token(customer_user)
    password = "spanish-inquisition"

    variables = {"email": customer_user.email, "password": password, "token": token}
    response = user_api_client.post_graphql(SET_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["setPassword"]
    assert data["user"]["id"]
    assert data["token"]

    customer_user.refresh_from_db()
    assert customer_user.check_password(password)

    password_resent_event = account_events.CustomerEvent.objects.get()
    assert password_resent_event.type == account_events.CustomerEvents.PASSWORD_RESET
    assert password_resent_event.user == customer_user


def test_set_password_invalid_token(user_api_client, customer_user):
    variables = {"email": customer_user.email, "password": "pass", "token": "token"}
    response = user_api_client.post_graphql(SET_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    errors = content["data"]["setPassword"]["errors"]
    assert errors[0]["message"] == INVALID_TOKEN

    account_errors = content["data"]["setPassword"]["errors"]
    assert account_errors[0]["message"] == INVALID_TOKEN
    assert account_errors[0]["code"] == AccountErrorCode.INVALID.name


def test_set_password_invalid_email(user_api_client):
    variables = {"email": "fake@example.com", "password": "pass", "token": "token"}
    response = user_api_client.post_graphql(SET_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    errors = content["data"]["setPassword"]["errors"]
    assert len(errors) == 1
    assert errors[0]["field"] == "email"

    account_errors = content["data"]["setPassword"]["errors"]
    assert len(account_errors) == 1
    assert account_errors[0]["field"] == "email"
    assert account_errors[0]["code"] == AccountErrorCode.NOT_FOUND.name


@freeze_time("2018-05-31 12:00:01")
def test_set_password_invalid_password(user_api_client, customer_user, settings):
    settings.AUTH_PASSWORD_VALIDATORS = [
        {
            "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
            "OPTIONS": {"min_length": 5},
        },
        {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    ]

    token = default_token_generator.make_token(customer_user)
    variables = {"email": customer_user.email, "password": "1234", "token": token}
    response = user_api_client.post_graphql(SET_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    errors = content["data"]["setPassword"]["errors"]
    assert len(errors) == 2
    assert (
        errors[0]["message"]
        == "This password is too short. It must contain at least 5 characters."
    )
    assert errors[1]["message"] == "This password is entirely numeric."

    account_errors = content["data"]["setPassword"]["errors"]
    assert account_errors[0]["code"] == str_to_enum("password_too_short")
    assert account_errors[1]["code"] == str_to_enum("password_entirely_numeric")


CHANGE_PASSWORD_MUTATION = """
    mutation PasswordChange($oldPassword: String, $newPassword: String!) {
        passwordChange(oldPassword: $oldPassword, newPassword: $newPassword) {
            errors {
                field
                message
            }
            user {
                email
            }
        }
    }
"""


def test_password_change(user_api_client):
    customer_user = user_api_client.user
    new_password = "spanish-inquisition"

    variables = {"oldPassword": "password", "newPassword": new_password}
    response = user_api_client.post_graphql(CHANGE_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["passwordChange"]
    assert not data["errors"]
    assert data["user"]["email"] == customer_user.email

    customer_user.refresh_from_db()
    assert customer_user.check_password(new_password)

    password_change_event = account_events.CustomerEvent.objects.get()
    assert password_change_event.type == account_events.CustomerEvents.PASSWORD_CHANGED
    assert password_change_event.user == customer_user


def test_password_change_incorrect_old_password(user_api_client):
    customer_user = user_api_client.user
    variables = {"oldPassword": "incorrect", "newPassword": ""}
    response = user_api_client.post_graphql(CHANGE_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["passwordChange"]
    customer_user.refresh_from_db()
    assert customer_user.check_password("password")
    assert data["errors"]
    assert data["errors"][0]["field"] == "oldPassword"


def test_password_change_invalid_new_password(user_api_client, settings):
    settings.AUTH_PASSWORD_VALIDATORS = [
        {
            "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
            "OPTIONS": {"min_length": 5},
        },
        {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    ]

    customer_user = user_api_client.user
    variables = {"oldPassword": "password", "newPassword": "1234"}
    response = user_api_client.post_graphql(CHANGE_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    errors = content["data"]["passwordChange"]["errors"]
    customer_user.refresh_from_db()
    assert customer_user.check_password("password")
    assert len(errors) == 2
    assert errors[1]["field"] == "newPassword"
    assert (
        errors[0]["message"]
        == "This password is too short. It must contain at least 5 characters."
    )
    assert errors[1]["field"] == "newPassword"
    assert errors[1]["message"] == "This password is entirely numeric."


def test_password_change_user_unusable_password_fails_if_old_password_is_set(
    user_api_client,
):
    customer_user = user_api_client.user
    customer_user.set_unusable_password()
    customer_user.save()

    new_password = "spanish-inquisition"

    variables = {"oldPassword": "password", "newPassword": new_password}
    response = user_api_client.post_graphql(CHANGE_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["passwordChange"]
    assert data["errors"]
    assert data["errors"][0]["field"] == "oldPassword"

    customer_user.refresh_from_db()
    assert not customer_user.has_usable_password()


def test_password_change_user_unusable_password_if_old_password_is_omitted(
    user_api_client,
):
    customer_user = user_api_client.user
    customer_user.set_unusable_password()
    customer_user.save()

    new_password = "spanish-inquisition"

    variables = {"oldPassword": None, "newPassword": new_password}
    response = user_api_client.post_graphql(CHANGE_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["passwordChange"]
    assert not data["errors"]
    assert data["user"]["email"] == customer_user.email

    customer_user.refresh_from_db()
    assert customer_user.check_password(new_password)

    password_change_event = account_events.CustomerEvent.objects.get()
    assert password_change_event.type == account_events.CustomerEvents.PASSWORD_CHANGED
    assert password_change_event.user == customer_user


def test_password_change_user_usable_password_fails_if_old_password_is_omitted(
    user_api_client,
):
    customer_user = user_api_client.user

    new_password = "spanish-inquisition"

    variables = {"newPassword": new_password}
    response = user_api_client.post_graphql(CHANGE_PASSWORD_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["passwordChange"]
    assert data["errors"]
    assert data["errors"][0]["field"] == "oldPassword"

    customer_user.refresh_from_db()
    assert customer_user.has_usable_password()
    assert not customer_user.check_password(new_password)


ADDRESS_CREATE_MUTATION = """
    mutation CreateUserAddress($user: ID!, $address: AddressInput!) {
        addressCreate(userId: $user, input: $address) {
            errors {
                field
                message
            }
            address {
                id
                city
                country {
                    code
                }
            }
            user {
                id
            }
        }
    }
"""


def test_create_address_mutation(
    staff_api_client, customer_user, permission_manage_users, graphql_address_data
):
    # given
    query = ADDRESS_CREATE_MUTATION
    graphql_address_data["city"] = "Dummy"
    user_id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"user": user_id, "address": graphql_address_data}
    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    # then
    assert content["data"]["addressCreate"]["errors"] == []
    data = content["data"]["addressCreate"]
    assert data["address"]["city"] == "DUMMY"
    assert data["address"]["country"]["code"] == "PL"
    address_obj = Address.objects.get(city="DUMMY")
    assert address_obj.user_addresses.first() == customer_user
    assert data["user"]["id"] == user_id

    customer_user.refresh_from_db()
    for field in ["city", "country"]:
        assert variables["address"][field].lower() in customer_user.search_document


@freeze_time("2022-05-12 12:00:00")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_create_address_mutation_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    customer_user,
    permission_manage_users,
    settings,
    graphql_address_data,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    user_id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"user": user_id, "address": graphql_address_data}

    # when
    response = staff_api_client.post_graphql(
        ADDRESS_CREATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    address = Address.objects.last()

    # then
    assert not content["data"]["addressCreate"]["errors"]
    assert content["data"]["addressCreate"]

    mocked_webhook_trigger.assert_called_once_with(
        *generate_address_webhook_call_args(
            address,
            WebhookEventAsyncType.ADDRESS_CREATED,
            staff_api_client.user,
            any_webhook,
        )
    )


@override_settings(MAX_USER_ADDRESSES=2)
def test_create_address_mutation_the_oldest_address_is_deleted(
    staff_api_client,
    customer_user,
    address,
    permission_manage_users,
    graphql_address_data,
):
    # given
    same_address = Address.objects.create(**address.as_data())
    customer_user.addresses.set([address, same_address])

    user_addresses_count = customer_user.addresses.count()
    graphql_address_data["city"] = "Dummy"
    query = ADDRESS_CREATE_MUTATION
    user_id = graphene.Node.to_global_id("User", customer_user.id)
    variables = {"user": user_id, "address": graphql_address_data}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    assert content["data"]["addressCreate"]["errors"] == []
    data = content["data"]["addressCreate"]
    assert data["address"]["city"] == "DUMMY"
    assert data["address"]["country"]["code"] == "PL"
    address_obj = Address.objects.get(city="DUMMY")
    assert address_obj.user_addresses.first() == customer_user
    assert data["user"]["id"] == user_id

    customer_user.refresh_from_db()
    assert customer_user.addresses.count() == user_addresses_count

    with pytest.raises(address._meta.model.DoesNotExist):
        address.refresh_from_db()


def test_create_address_validation_fails(
    staff_api_client,
    customer_user,
    graphql_address_data,
    permission_manage_users,
    address,
):
    # given
    query = ADDRESS_CREATE_MUTATION
    address_data = graphql_address_data
    user_id = graphene.Node.to_global_id("User", customer_user.id)
    address_data["postalCode"] = "wrong postal code"
    variables = {"user": user_id, "address": graphql_address_data}

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    data = content["data"]["addressCreate"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["field"] == "postalCode"
    assert data["address"] is None


ADDRESS_UPDATE_MUTATION = """
    mutation updateUserAddress($addressId: ID!, $address: AddressInput!) {
        addressUpdate(id: $addressId, input: $address) {
            address {
                city
            }
            user {
                id
            }
        }
    }
"""


def test_address_update_mutation(
    staff_api_client, customer_user, permission_manage_users, graphql_address_data
):
    query = ADDRESS_UPDATE_MUTATION
    address_obj = customer_user.addresses.first()
    assert staff_api_client.user not in address_obj.user_addresses.all()
    variables = {
        "addressId": graphene.Node.to_global_id("Address", address_obj.id),
        "address": graphql_address_data,
    }
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["addressUpdate"]
    assert data["address"]["city"] == graphql_address_data["city"].upper()
    address_obj.refresh_from_db()
    assert address_obj.city == graphql_address_data["city"].upper()
    customer_user.refresh_from_db()
    assert (
        generate_address_search_document_value(address_obj)
        in customer_user.search_document
    )


@freeze_time("2022-05-12 12:00:00")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_address_update_mutation_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    customer_user,
    permission_manage_users,
    graphql_address_data,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    address = customer_user.addresses.first()
    assert staff_api_client.user not in address.user_addresses.all()
    variables = {
        "addressId": graphene.Node.to_global_id("Address", address.id),
        "address": graphql_address_data,
    }

    # when
    response = staff_api_client.post_graphql(
        ADDRESS_UPDATE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    address.refresh_from_db()

    # then
    assert content["data"]["addressUpdate"]
    mocked_webhook_trigger.assert_called_with(
        *generate_address_webhook_call_args(
            address,
            WebhookEventAsyncType.ADDRESS_UPDATED,
            staff_api_client.user,
            any_webhook,
        )
    )


@patch("saleor.graphql.account.mutations.base.prepare_user_search_document_value")
def test_address_update_mutation_no_user_assigned(
    prepare_user_search_document_value_mock,
    staff_api_client,
    address,
    permission_manage_users,
    graphql_address_data,
):
    # given
    query = ADDRESS_UPDATE_MUTATION

    variables = {
        "addressId": graphene.Node.to_global_id("Address", address.id),
        "address": graphql_address_data,
    }

    # when
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )

    # then
    content = get_graphql_content(response)
    data = content["data"]["addressUpdate"]
    assert data["address"]["city"] == graphql_address_data["city"].upper()
    prepare_user_search_document_value_mock.assert_not_called()


ACCOUNT_ADDRESS_UPDATE_MUTATION = """
    mutation updateAccountAddress($addressId: ID!, $address: AddressInput!) {
        accountAddressUpdate(id: $addressId, input: $address) {
            address {
                city
            }
            user {
                id
            }
        }
    }
"""


def test_customer_update_own_address(
    user_api_client, customer_user, graphql_address_data
):
    query = ACCOUNT_ADDRESS_UPDATE_MUTATION
    address_obj = customer_user.addresses.first()
    address_data = graphql_address_data
    address_data["city"] = "Poznań"
    assert address_data["city"] != address_obj.city
    user = user_api_client.user

    variables = {
        "addressId": graphene.Node.to_global_id("Address", address_obj.id),
        "address": address_data,
    }
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountAddressUpdate"]
    assert data["address"]["city"] == address_data["city"].upper()
    address_obj.refresh_from_db()
    assert address_obj.city == address_data["city"].upper()
    user.refresh_from_db()
    assert generate_address_search_document_value(address_obj) in user.search_document


@freeze_time("2022-05-12 12:00:00")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_customer_address_update_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    user_api_client,
    customer_user,
    graphql_address_data,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    address = customer_user.addresses.first()
    address_data = graphql_address_data
    address_data["city"] = "Poznań"
    assert address_data["city"] != address.city

    variables = {
        "addressId": graphene.Node.to_global_id("Address", address.id),
        "address": graphql_address_data,
    }

    # when
    response = user_api_client.post_graphql(ACCOUNT_ADDRESS_UPDATE_MUTATION, variables)
    content = get_graphql_content(response)
    address.refresh_from_db()

    # then
    assert content["data"]["accountAddressUpdate"]
    mocked_webhook_trigger.assert_called_once_with(
        *generate_address_webhook_call_args(
            address,
            WebhookEventAsyncType.ADDRESS_UPDATED,
            user_api_client.user,
            any_webhook,
        )
    )


def test_update_address_as_anonymous_user(
    api_client, customer_user, graphql_address_data
):
    query = ACCOUNT_ADDRESS_UPDATE_MUTATION
    address_obj = customer_user.addresses.first()

    variables = {
        "addressId": graphene.Node.to_global_id("Address", address_obj.id),
        "address": graphql_address_data,
    }
    response = api_client.post_graphql(query, variables)
    assert_no_permission(response)


def test_customer_update_own_address_not_updated_when_validation_fails(
    user_api_client, customer_user, graphql_address_data
):
    query = ACCOUNT_ADDRESS_UPDATE_MUTATION
    address_obj = customer_user.addresses.first()
    address_data = graphql_address_data
    address_data["city"] = "Poznań"
    address_data["postalCode"] = "wrong postal code"
    assert address_data["city"] != address_obj.city

    variables = {
        "addressId": graphene.Node.to_global_id("Address", address_obj.id),
        "address": address_data,
    }
    user_api_client.post_graphql(query, variables)
    address_obj.refresh_from_db()
    assert address_obj.city != address_data["city"]
    assert address_obj.postal_code != address_data["postalCode"]


@pytest.mark.parametrize(
    "query", [ADDRESS_UPDATE_MUTATION, ACCOUNT_ADDRESS_UPDATE_MUTATION]
)
def test_customer_update_address_for_other(
    user_api_client, customer_user, address_other_country, graphql_address_data, query
):
    address_obj = address_other_country
    assert customer_user not in address_obj.user_addresses.all()

    address_data = graphql_address_data
    variables = {
        "addressId": graphene.Node.to_global_id("Address", address_obj.id),
        "address": address_data,
    }
    response = user_api_client.post_graphql(query, variables)
    assert_no_permission(response)


ADDRESS_DELETE_MUTATION = """
    mutation deleteUserAddress($id: ID!) {
        addressDelete(id: $id) {
            address {
                city
            }
            user {
                id
            }
        }
    }
"""


def test_address_delete_mutation(
    staff_api_client, customer_user, permission_manage_users
):
    query = ADDRESS_DELETE_MUTATION
    address_obj = customer_user.addresses.first()
    variables = {"id": graphene.Node.to_global_id("Address", address_obj.id)}
    response = staff_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["addressDelete"]
    assert data["address"]["city"] == address_obj.city
    assert data["user"]["id"] == graphene.Node.to_global_id("User", customer_user.pk)
    with pytest.raises(address_obj._meta.model.DoesNotExist):
        address_obj.refresh_from_db()

    customer_user.refresh_from_db()
    assert (
        generate_address_search_document_value(address_obj)
        not in customer_user.search_document
    )


@freeze_time("2022-05-12 12:00:00")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_address_delete_mutation_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    staff_api_client,
    customer_user,
    permission_manage_users,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    address = customer_user.addresses.first()
    variables = {"id": graphene.Node.to_global_id("Address", address.id)}

    # when
    response = staff_api_client.post_graphql(
        ADDRESS_DELETE_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    assert content["data"]["addressDelete"]
    mocked_webhook_trigger.assert_called_once_with(
        *generate_address_webhook_call_args(
            address,
            WebhookEventAsyncType.ADDRESS_DELETED,
            staff_api_client.user,
            any_webhook,
        )
    )


def test_address_delete_mutation_as_app(
    app_api_client, customer_user, permission_manage_users
):
    query = ADDRESS_DELETE_MUTATION
    address_obj = customer_user.addresses.first()
    variables = {"id": graphene.Node.to_global_id("Address", address_obj.id)}
    response = app_api_client.post_graphql(
        query, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["addressDelete"]
    assert data["address"]["city"] == address_obj.city
    assert data["user"]["id"] == graphene.Node.to_global_id("User", customer_user.pk)
    with pytest.raises(address_obj._meta.model.DoesNotExist):
        address_obj.refresh_from_db()


ACCOUNT_ADDRESS_DELETE_MUTATION = """
    mutation deleteUserAddress($id: ID!) {
        accountAddressDelete(id: $id) {
            address {
                city
            }
            user {
                id
            }
        }
    }
"""


def test_customer_delete_own_address(user_api_client, customer_user):
    query = ACCOUNT_ADDRESS_DELETE_MUTATION
    address_obj = customer_user.addresses.first()
    user = user_api_client.user
    variables = {"id": graphene.Node.to_global_id("Address", address_obj.id)}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountAddressDelete"]
    assert data["address"]["city"] == address_obj.city
    with pytest.raises(address_obj._meta.model.DoesNotExist):
        address_obj.refresh_from_db()
    user.refresh_from_db()
    assert (
        generate_address_search_document_value(address_obj) not in user.search_document
    )


@freeze_time("2022-05-12 12:00:00")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_customer_delete_address_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    user_api_client,
    customer_user,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    address = customer_user.addresses.first()
    variables = {"id": graphene.Node.to_global_id("Address", address.id)}

    # when
    response = user_api_client.post_graphql(ACCOUNT_ADDRESS_DELETE_MUTATION, variables)
    content = get_graphql_content(response)

    # then
    assert content["data"]["accountAddressDelete"]
    mocked_webhook_trigger.assert_called_with(
        *generate_address_webhook_call_args(
            address,
            WebhookEventAsyncType.ADDRESS_DELETED,
            user_api_client.user,
            any_webhook,
        )
    )


@pytest.mark.parametrize(
    "query", [ADDRESS_DELETE_MUTATION, ACCOUNT_ADDRESS_DELETE_MUTATION]
)
def test_customer_delete_address_for_other(
    user_api_client, customer_user, address_other_country, query
):
    address_obj = address_other_country
    assert customer_user not in address_obj.user_addresses.all()
    variables = {"id": graphene.Node.to_global_id("Address", address_obj.id)}
    response = user_api_client.post_graphql(query, variables)
    assert_no_permission(response)
    address_obj.refresh_from_db()


SET_DEFAULT_ADDRESS_MUTATION = """
mutation($address_id: ID!, $user_id: ID!, $type: AddressTypeEnum!) {
  addressSetDefault(addressId: $address_id, userId: $user_id, type: $type) {
    errors {
      field
      message
    }
    user {
      defaultBillingAddress {
        id
      }
      defaultShippingAddress {
        id
      }
    }
  }
}
"""


def test_set_default_address(
    staff_api_client, address_other_country, customer_user, permission_manage_users
):
    customer_user.default_billing_address = None
    customer_user.default_shipping_address = None
    customer_user.save()

    # try to set an address that doesn't belong to that user
    address = address_other_country

    variables = {
        "address_id": graphene.Node.to_global_id("Address", address.id),
        "user_id": graphene.Node.to_global_id("User", customer_user.id),
        "type": AddressType.SHIPPING.upper(),
    }

    response = staff_api_client.post_graphql(
        SET_DEFAULT_ADDRESS_MUTATION, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["addressSetDefault"]
    assert data["errors"][0]["field"] == "addressId"

    # try to set a new billing address using one of user's addresses
    address = customer_user.addresses.first()
    address_id = graphene.Node.to_global_id("Address", address.id)

    variables["address_id"] = address_id
    response = staff_api_client.post_graphql(SET_DEFAULT_ADDRESS_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["addressSetDefault"]
    assert data["user"]["defaultShippingAddress"]["id"] == address_id


GET_ADDRESS_VALIDATION_RULES_QUERY = """
    query getValidator(
        $country_code: CountryCode!, $country_area: String, $city_area: String) {
        addressValidationRules(
                countryCode: $country_code,
                countryArea: $country_area,
                cityArea: $city_area) {
            countryCode
            countryName
            addressFormat
            addressLatinFormat
            allowedFields
            requiredFields
            upperFields
            countryAreaType
            countryAreaChoices {
                verbose
                raw
            }
            cityType
            cityChoices {
                raw
                verbose
            }
            cityAreaType
            cityAreaChoices {
                raw
                verbose
            }
            postalCodeType
            postalCodeMatchers
            postalCodeExamples
            postalCodePrefix
        }
    }
"""


def test_address_validation_rules(user_api_client):
    query = GET_ADDRESS_VALIDATION_RULES_QUERY
    variables = {"country_code": "PL", "country_area": None, "city_area": None}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["addressValidationRules"]
    assert data["countryCode"] == "PL"
    assert data["countryName"] == "POLAND"
    assert data["addressFormat"] is not None
    assert data["addressLatinFormat"] is not None
    assert data["cityType"] == "city"
    assert data["cityAreaType"] == "suburb"
    matcher = data["postalCodeMatchers"][0]
    matcher = re.compile(matcher)
    assert matcher.match("00-123")
    assert not data["cityAreaChoices"]
    assert not data["cityChoices"]
    assert not data["countryAreaChoices"]
    assert data["postalCodeExamples"]
    assert data["postalCodeType"] == "postal"
    assert set(data["allowedFields"]) == {
        "companyName",
        "city",
        "postalCode",
        "streetAddress1",
        "name",
        "streetAddress2",
    }
    assert set(data["requiredFields"]) == {"postalCode", "streetAddress1", "city"}
    assert set(data["upperFields"]) == {"city"}


def test_address_validation_rules_with_country_area(user_api_client):
    query = GET_ADDRESS_VALIDATION_RULES_QUERY
    variables = {
        "country_code": "CN",
        "country_area": "Fujian Sheng",
        "city_area": None,
    }
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["addressValidationRules"]
    assert data["countryCode"] == "CN"
    assert data["countryName"] == "CHINA"
    assert data["countryAreaType"] == "province"
    assert data["countryAreaChoices"]
    assert data["cityType"] == "city"
    assert data["cityChoices"]
    assert data["cityAreaType"] == "district"
    assert not data["cityAreaChoices"]
    assert data["cityChoices"]
    assert data["countryAreaChoices"]
    assert data["postalCodeExamples"]
    assert data["postalCodeType"] == "postal"
    assert set(data["allowedFields"]) == {
        "city",
        "postalCode",
        "streetAddress1",
        "name",
        "streetAddress2",
        "countryArea",
        "companyName",
        "cityArea",
    }
    assert set(data["requiredFields"]) == {
        "postalCode",
        "streetAddress1",
        "city",
        "countryArea",
    }
    assert set(data["upperFields"]) == {"countryArea"}


def test_address_validation_rules_fields_in_camel_case(user_api_client):
    query = """
    query getValidator(
        $country_code: CountryCode!) {
        addressValidationRules(countryCode: $country_code) {
            requiredFields
            allowedFields
        }
    }
    """
    variables = {"country_code": "PL"}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"]["addressValidationRules"]
    required_fields = data["requiredFields"]
    allowed_fields = data["allowedFields"]
    assert "streetAddress1" in required_fields
    assert "streetAddress2" not in required_fields
    assert "streetAddress1" in allowed_fields
    assert "streetAddress2" in allowed_fields


REQUEST_PASSWORD_RESET_MUTATION = """
    mutation RequestPasswordReset(
        $email: String!, $redirectUrl: String!, $channel: String) {
        requestPasswordReset(
            email: $email, redirectUrl: $redirectUrl, channel: $channel) {
            errors {
                field
                message
            }
        }
    }
"""

CONFIRM_ACCOUNT_MUTATION = """
    mutation ConfirmAccount($email: String!, $token: String!) {
        confirmAccount(email: $email, token: $token) {
            errors {
                field
                code
            }
            user {
                id
                email
            }
        }
    }
"""


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password(
    mocked_notify,
    user_api_client,
    customer_user,
    channel_PLN,
    channel_USD,
    site_settings,
):
    redirect_url = "https://www.example.com"
    variables = {
        "email": customer_user.email,
        "redirectUrl": redirect_url,
        "channel": channel_PLN.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert not data["errors"]
    token = default_token_generator.make_token(customer_user)
    params = urlencode({"email": customer_user.email, "token": token})
    reset_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(customer_user),
        "reset_url": reset_url,
        "token": token,
        "recipient_email": customer_user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_PASSWORD_RESET,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password_with_upper_case_email(
    mocked_notify,
    user_api_client,
    customer_user,
    channel_PLN,
    channel_USD,
    site_settings,
):
    redirect_url = "https://www.example.com"
    variables = {
        "email": customer_user.email.upper(),
        "redirectUrl": redirect_url,
        "channel": channel_PLN.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert not data["errors"]
    token = default_token_generator.make_token(customer_user)
    params = urlencode({"email": customer_user.email, "token": token})
    reset_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(customer_user),
        "reset_url": reset_url,
        "token": token,
        "recipient_email": customer_user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_PASSWORD_RESET,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.graphql.account.mutations.base.assign_user_gift_cards")
@patch("saleor.graphql.account.mutations.base.match_orders_with_new_user")
def test_account_confirmation(
    match_orders_with_new_user_mock,
    assign_gift_cards_mock,
    api_client,
    customer_user,
    channel_USD,
):
    customer_user.is_active = False
    customer_user.save()

    variables = {
        "email": customer_user.email,
        "token": default_token_generator.make_token(customer_user),
        "channel": channel_USD.slug,
    }
    response = api_client.post_graphql(CONFIRM_ACCOUNT_MUTATION, variables)
    content = get_graphql_content(response)
    assert not content["data"]["confirmAccount"]["errors"]
    assert content["data"]["confirmAccount"]["user"]["email"] == customer_user.email
    customer_user.refresh_from_db()
    match_orders_with_new_user_mock.assert_called_once_with(customer_user)
    assign_gift_cards_mock.assert_called_once_with(customer_user)
    assert customer_user.is_active is True


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.graphql.account.mutations.base.assign_user_gift_cards")
@patch("saleor.graphql.account.mutations.base.match_orders_with_new_user")
def test_account_confirmation_invalid_user(
    match_orders_with_new_user_mock,
    assign_gift_cards_mock,
    user_api_client,
    customer_user,
    channel_USD,
):
    variables = {
        "email": "non-existing@example.com",
        "token": default_token_generator.make_token(customer_user),
        "channel": channel_USD.slug,
    }
    response = user_api_client.post_graphql(CONFIRM_ACCOUNT_MUTATION, variables)
    content = get_graphql_content(response)
    assert content["data"]["confirmAccount"]["errors"][0]["field"] == "email"
    assert (
        content["data"]["confirmAccount"]["errors"][0]["code"]
        == AccountErrorCode.NOT_FOUND.name
    )
    match_orders_with_new_user_mock.assert_not_called()
    assign_gift_cards_mock.assert_not_called()


@patch("saleor.graphql.account.mutations.base.assign_user_gift_cards")
@patch("saleor.graphql.account.mutations.base.match_orders_with_new_user")
def test_account_confirmation_invalid_token(
    match_orders_with_new_user_mock,
    assign_gift_cards_mock,
    user_api_client,
    customer_user,
    channel_USD,
):
    variables = {
        "email": customer_user.email,
        "token": "invalid_token",
        "channel": channel_USD.slug,
    }
    response = user_api_client.post_graphql(CONFIRM_ACCOUNT_MUTATION, variables)
    content = get_graphql_content(response)
    assert content["data"]["confirmAccount"]["errors"][0]["field"] == "token"
    assert (
        content["data"]["confirmAccount"]["errors"][0]["code"]
        == AccountErrorCode.INVALID.name
    )
    match_orders_with_new_user_mock.assert_not_called()
    assign_gift_cards_mock.assert_not_called()


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_request_password_reset_email_for_staff(
    mocked_notify, staff_api_client, channel_USD, site_settings
):
    redirect_url = "https://www.example.com"
    variables = {"email": staff_api_client.user.email, "redirectUrl": redirect_url}
    response = staff_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert not data["errors"]
    token = default_token_generator.make_token(staff_api_client.user)
    params = urlencode({"email": staff_api_client.user.email, "token": token})
    reset_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(staff_api_client.user),
        "reset_url": reset_url,
        "token": token,
        "recipient_email": staff_api_client.user.email,
        "channel_slug": None,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_STAFF_RESET_PASSWORD,
        payload=expected_payload,
        channel_slug=None,
    )


@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password_invalid_email(
    mocked_notify, user_api_client, channel_USD
):
    variables = {
        "email": "non-existing-email@email.com",
        "redirectUrl": "https://www.example.com",
        "channel": channel_USD.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert len(data["errors"]) == 1
    mocked_notify.assert_not_called()


@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password_user_is_inactive(
    mocked_notify, user_api_client, customer_user, channel_USD
):
    user = customer_user
    user.is_active = False
    user.save()

    variables = {
        "email": customer_user.email,
        "redirectUrl": "https://www.example.com",
        "channel": channel_USD.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    results = response.json()
    assert "errors" in results
    assert (
        results["errors"][0]["message"]
        == "Invalid token. User does not exist or is inactive."
    )
    assert not mocked_notify.called


@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password_storefront_hosts_not_allowed(
    mocked_notify, user_api_client, customer_user, channel_USD
):
    variables = {
        "email": customer_user.email,
        "redirectUrl": "https://www.fake.com",
        "channel": channel_USD.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["field"] == "redirectUrl"
    mocked_notify.assert_not_called()


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password_all_storefront_hosts_allowed(
    mocked_notify,
    user_api_client,
    customer_user,
    settings,
    channel_PLN,
    channel_USD,
    site_settings,
):
    settings.ALLOWED_CLIENT_HOSTS = ["*"]
    redirect_url = "https://www.test.com"
    variables = {
        "email": customer_user.email,
        "redirectUrl": redirect_url,
        "channel": channel_PLN.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert not data["errors"]

    token = default_token_generator.make_token(customer_user)
    params = urlencode({"email": customer_user.email, "token": token})
    reset_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(customer_user),
        "reset_url": reset_url,
        "token": token,
        "recipient_email": customer_user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_PASSWORD_RESET,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_reset_password_subdomain(
    mocked_notify, user_api_client, customer_user, settings, channel_PLN, site_settings
):
    settings.ALLOWED_CLIENT_HOSTS = [".example.com"]
    redirect_url = "https://sub.example.com"
    variables = {
        "email": customer_user.email,
        "redirectUrl": redirect_url,
        "channel": channel_PLN.slug,
    }
    response = user_api_client.post_graphql(REQUEST_PASSWORD_RESET_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestPasswordReset"]
    assert not data["errors"]

    token = default_token_generator.make_token(customer_user)
    params = urlencode({"email": customer_user.email, "token": token})
    reset_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(customer_user),
        "reset_url": reset_url,
        "token": token,
        "recipient_email": customer_user.email,
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_PASSWORD_RESET,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


ACCOUNT_ADDRESS_CREATE_MUTATION = """
mutation($addressInput: AddressInput!, $addressType: AddressTypeEnum) {
  accountAddressCreate(input: $addressInput, type: $addressType) {
    address {
        id,
        city
    }
    user {
        email
    }
    errors {
        code
        field
        addressType
    }
  }
}
"""


def test_customer_create_address(user_api_client, graphql_address_data):
    user = user_api_client.user
    user_addresses_count = user.addresses.count()

    query = ACCOUNT_ADDRESS_CREATE_MUTATION
    mutation_name = "accountAddressCreate"

    variables = {"addressInput": graphql_address_data}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]

    assert data["address"]["city"] == graphql_address_data["city"].upper()

    user.refresh_from_db()
    assert user.addresses.count() == user_addresses_count + 1
    assert (
        generate_address_search_document_value(user.addresses.last())
        in user.search_document
    )


@freeze_time("2022-05-12 12:00:00")
@patch("saleor.plugins.webhook.plugin.get_webhooks_for_event")
@patch("saleor.plugins.webhook.plugin.trigger_webhooks_async")
def test_customer_create_address_trigger_webhook(
    mocked_webhook_trigger,
    mocked_get_webhooks_for_event,
    any_webhook,
    user_api_client,
    graphql_address_data,
    settings,
):
    # given
    mocked_get_webhooks_for_event.return_value = [any_webhook]
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]

    variables = {"addressInput": graphql_address_data}

    # when
    response = user_api_client.post_graphql(ACCOUNT_ADDRESS_CREATE_MUTATION, variables)
    content = get_graphql_content(response)
    address = Address.objects.last()

    # then
    assert content["data"]["accountAddressCreate"]
    mocked_webhook_trigger.assert_called_with(
        *generate_address_webhook_call_args(
            address,
            WebhookEventAsyncType.ADDRESS_CREATED,
            user_api_client.user,
            any_webhook,
        )
    )


def test_account_address_create_return_user(user_api_client, graphql_address_data):
    user = user_api_client.user
    variables = {"addressInput": graphql_address_data}
    response = user_api_client.post_graphql(ACCOUNT_ADDRESS_CREATE_MUTATION, variables)
    content = get_graphql_content(response)
    data = content["data"]["accountAddressCreate"]["user"]
    assert data["email"] == user.email


def test_customer_create_default_address(user_api_client, graphql_address_data):
    user = user_api_client.user
    user_addresses_count = user.addresses.count()

    query = ACCOUNT_ADDRESS_CREATE_MUTATION
    mutation_name = "accountAddressCreate"

    address_type = AddressType.SHIPPING.upper()
    variables = {"addressInput": graphql_address_data, "addressType": address_type}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert data["address"]["city"] == graphql_address_data["city"].upper()

    user.refresh_from_db()
    assert user.addresses.count() == user_addresses_count + 1
    assert user.default_shipping_address.id == int(
        graphene.Node.from_global_id(data["address"]["id"])[1]
    )

    address_type = AddressType.BILLING.upper()
    variables = {"addressInput": graphql_address_data, "addressType": address_type}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert data["address"]["city"] == graphql_address_data["city"].upper()

    user.refresh_from_db()
    assert user.addresses.count() == user_addresses_count + 2
    assert user.default_billing_address.id == int(
        graphene.Node.from_global_id(data["address"]["id"])[1]
    )


@override_settings(MAX_USER_ADDRESSES=2)
def test_customer_create_address_the_oldest_address_is_deleted(
    user_api_client, graphql_address_data, address
):
    """Ensure that when mew address it added to user with max amount of addressess,
    the oldest address will be removed."""
    user = user_api_client.user
    same_address = Address.objects.create(**address.as_data())
    user.addresses.set([address, same_address])

    user_addresses_count = user.addresses.count()

    query = ACCOUNT_ADDRESS_CREATE_MUTATION
    mutation_name = "accountAddressCreate"

    variables = {"addressInput": graphql_address_data}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]

    assert data["address"]["city"] == graphql_address_data["city"].upper()

    user.refresh_from_db()
    assert user.addresses.count() == user_addresses_count

    with pytest.raises(address._meta.model.DoesNotExist):
        address.refresh_from_db()


def test_anonymous_user_create_address(api_client, graphql_address_data):
    query = ACCOUNT_ADDRESS_CREATE_MUTATION
    variables = {"addressInput": graphql_address_data}
    response = api_client.post_graphql(query, variables)
    assert_no_permission(response)


def test_address_not_created_after_validation_fails(
    user_api_client, graphql_address_data
):
    user = user_api_client.user
    user_addresses_count = user.addresses.count()

    query = ACCOUNT_ADDRESS_CREATE_MUTATION

    graphql_address_data["postalCode"] = "wrong postal code"

    address_type = AddressType.SHIPPING.upper()
    variables = {"addressInput": graphql_address_data, "addressType": address_type}
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)

    data = content["data"]["accountAddressCreate"]
    assert not data["address"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == AccountErrorCode.INVALID.name
    assert data["errors"][0]["field"] == "postalCode"
    assert data["errors"][0]["addressType"] == address_type
    user.refresh_from_db()
    assert user.addresses.count() == user_addresses_count


ACCOUNT_SET_DEFAULT_ADDRESS_MUTATION = """
mutation($id: ID!, $type: AddressTypeEnum!) {
  accountSetDefaultAddress(id: $id, type: $type) {
    errors {
      field,
      message
    }
  }
}
"""


def test_customer_set_address_as_default(user_api_client):
    user = user_api_client.user
    user.default_billing_address = None
    user.default_shipping_address = None
    user.save()
    assert not user.default_billing_address
    assert not user.default_shipping_address
    assert user.addresses.exists()

    address = user.addresses.first()
    query = ACCOUNT_SET_DEFAULT_ADDRESS_MUTATION
    mutation_name = "accountSetDefaultAddress"

    variables = {
        "id": graphene.Node.to_global_id("Address", address.id),
        "type": AddressType.SHIPPING.upper(),
    }
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert not data["errors"]

    user.refresh_from_db()
    assert user.default_shipping_address == address

    variables["type"] = AddressType.BILLING.upper()
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert not data["errors"]

    user.refresh_from_db()
    assert user.default_billing_address == address


def test_customer_change_default_address(user_api_client, address_other_country):
    user = user_api_client.user
    assert user.default_billing_address
    assert user.default_billing_address
    address = user.default_shipping_address
    assert address in user.addresses.all()
    assert address_other_country not in user.addresses.all()

    user.default_shipping_address = address_other_country
    user.save()
    user.refresh_from_db()
    assert address_other_country not in user.addresses.all()

    query = ACCOUNT_SET_DEFAULT_ADDRESS_MUTATION
    mutation_name = "accountSetDefaultAddress"

    variables = {
        "id": graphene.Node.to_global_id("Address", address.id),
        "type": AddressType.SHIPPING.upper(),
    }
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    data = content["data"][mutation_name]
    assert not data["errors"]

    user.refresh_from_db()
    assert user.default_shipping_address == address
    assert address_other_country in user.addresses.all()


def test_customer_change_default_address_invalid_address(
    user_api_client, address_other_country
):
    user = user_api_client.user
    assert address_other_country not in user.addresses.all()

    query = ACCOUNT_SET_DEFAULT_ADDRESS_MUTATION
    mutation_name = "accountSetDefaultAddress"

    variables = {
        "id": graphene.Node.to_global_id("Address", address_other_country.id),
        "type": AddressType.SHIPPING.upper(),
    }
    response = user_api_client.post_graphql(query, variables)
    content = get_graphql_content(response)
    assert content["data"][mutation_name]["errors"][0]["field"] == "id"


USER_AVATAR_UPDATE_MUTATION = """
    mutation userAvatarUpdate($image: Upload!) {
        userAvatarUpdate(image: $image) {
            user {
                avatar(size: 0) {
                    url
                }
            }
        }
    }
"""


def test_user_avatar_update_mutation_permission(api_client):
    """Should raise error if user is not staff."""

    query = USER_AVATAR_UPDATE_MUTATION

    image_file, image_name = create_image("avatar")
    variables = {"image": image_name}
    body = get_multipart_request_body(query, variables, image_file, image_name)
    response = api_client.post_multipart(body)

    assert_no_permission(response)


def test_user_avatar_update_mutation(
    monkeypatch, staff_api_client, media_root, site_settings
):
    query = USER_AVATAR_UPDATE_MUTATION

    user = staff_api_client.user

    image_file, image_name = create_image("avatar")
    variables = {"image": image_name}
    body = get_multipart_request_body(query, variables, image_file, image_name)

    # when
    response = staff_api_client.post_multipart(body)

    # then
    content = get_graphql_content(response)

    data = content["data"]["userAvatarUpdate"]
    user.refresh_from_db()

    assert user.avatar
    assert data["user"]["avatar"]["url"].startswith(
        f"http://{site_settings.site.domain}/media/user-avatars/avatar"
    )
    img_name, format = os.path.splitext(image_file._name)
    file_name = user.avatar.name
    assert file_name != image_file._name
    assert file_name.startswith(f"user-avatars/{img_name}")
    assert file_name.endswith(format)


def test_user_avatar_update_mutation_image_exists(
    staff_api_client, media_root, site_settings
):
    query = USER_AVATAR_UPDATE_MUTATION

    user = staff_api_client.user
    avatar_mock = MagicMock(spec=File)
    avatar_mock.name = "image.jpg"
    user.avatar = avatar_mock
    user.save()

    # create thumbnail for old avatar
    Thumbnail.objects.create(user=staff_api_client.user, size=128)
    assert user.thumbnails.exists()

    image_file, image_name = create_image("new_image")
    variables = {"image": image_name}
    body = get_multipart_request_body(query, variables, image_file, image_name)

    # when
    response = staff_api_client.post_multipart(body)

    # then
    content = get_graphql_content(response)

    data = content["data"]["userAvatarUpdate"]
    user.refresh_from_db()

    assert user.avatar != avatar_mock
    assert data["user"]["avatar"]["url"].startswith(
        f"http://{site_settings.site.domain}/media/user-avatars/new_image"
    )
    assert not user.thumbnails.exists()


USER_AVATAR_DELETE_MUTATION = """
    mutation userAvatarDelete {
        userAvatarDelete {
            user {
                avatar {
                    url
                }
            }
        }
    }
"""


def test_user_avatar_delete_mutation_permission(api_client):
    """Should raise error if user is not staff."""

    query = USER_AVATAR_DELETE_MUTATION

    response = api_client.post_graphql(query)

    assert_no_permission(response)


def test_user_avatar_delete_mutation(staff_api_client):
    # given
    query = USER_AVATAR_DELETE_MUTATION

    user = staff_api_client.user
    Thumbnail.objects.create(user=staff_api_client.user, size=128)
    assert user.thumbnails.all()

    # when
    response = staff_api_client.post_graphql(query)
    content = get_graphql_content(response)

    # then
    user.refresh_from_db()

    assert not user.avatar
    assert not content["data"]["userAvatarDelete"]["user"]["avatar"]
    assert not user.thumbnails.exists()


@pytest.mark.parametrize(
    "customer_filter, count",
    [
        ({"placedOrders": {"gte": "2019-04-18"}}, 1),
        ({"placedOrders": {"lte": "2012-01-14"}}, 1),
        ({"placedOrders": {"lte": "2012-01-14", "gte": "2012-01-13"}}, 1),
        ({"placedOrders": {"gte": "2012-01-14"}}, 2),
    ],
)
def test_query_customers_with_filter_placed_orders(
    customer_filter,
    count,
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_user,
    channel_USD,
):
    Order.objects.create(user=customer_user, channel=channel_USD)
    second_customer = User.objects.create(email="second_example@example.com")
    with freeze_time("2012-01-14 11:00:00"):
        Order.objects.create(user=second_customer, channel=channel_USD)
    variables = {"filter": customer_filter}
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]

    assert len(users) == count


@pytest.mark.parametrize(
    "customer_filter, count",
    [
        ({"dateJoined": {"gte": "2019-04-18"}}, 1),
        ({"dateJoined": {"lte": "2012-01-14"}}, 1),
        ({"dateJoined": {"lte": "2012-01-14", "gte": "2012-01-13"}}, 1),
        ({"dateJoined": {"gte": "2012-01-14"}}, 2),
        ({"updatedAt": {"gte": "2012-01-14T10:59:00+00:00"}}, 2),
        ({"updatedAt": {"gte": "2012-01-14T11:01:00+00:00"}}, 1),
        ({"updatedAt": {"lte": "2012-01-14T12:00:00+00:00"}}, 1),
        ({"updatedAt": {"lte": "2011-01-14T10:59:00+00:00"}}, 0),
        (
            {
                "updatedAt": {
                    "lte": "2012-01-14T12:00:00+00:00",
                    "gte": "2012-01-14T10:00:00+00:00",
                }
            },
            1,
        ),
        ({"updatedAt": {"gte": "2012-01-14T10:00:00+00:00"}}, 2),
    ],
)
def test_query_customers_with_filter_date_joined_and_updated_at(
    customer_filter,
    count,
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_user,
):
    with freeze_time("2012-01-14 11:00:00"):
        User.objects.create(email="second_example@example.com")
    variables = {"filter": customer_filter}
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]
    assert len(users) == count


@pytest.mark.parametrize(
    "customer_filter, count",
    [
        ({"numberOfOrders": {"gte": 0, "lte": 1}}, 1),
        ({"numberOfOrders": {"gte": 1, "lte": 3}}, 2),
        ({"numberOfOrders": {"gte": 0}}, 2),
        ({"numberOfOrders": {"lte": 3}}, 2),
    ],
)
def test_query_customers_with_filter_placed_orders_(
    customer_filter,
    count,
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_user,
    channel_USD,
):
    Order.objects.bulk_create(
        [
            Order(user=customer_user, channel=channel_USD),
            Order(user=customer_user, channel=channel_USD),
            Order(user=customer_user, channel=channel_USD),
        ]
    )
    second_customer = User.objects.create(email="second_example@example.com")
    with freeze_time("2012-01-14 11:00:00"):
        Order.objects.create(user=second_customer, channel=channel_USD)
    variables = {"filter": customer_filter}
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]

    assert len(users) == count


def test_query_customers_with_filter_metadata(
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_user,
    channel_USD,
):
    second_customer = User.objects.create(email="second_example@example.com")
    second_customer.store_value_in_metadata({"metakey": "metavalue"})
    second_customer.save()

    variables = {"filter": {"metadata": [{"key": "metakey", "value": "metavalue"}]}}
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]
    assert len(users) == 1
    user = users[0]
    _, user_id = graphene.Node.from_global_id(user["node"]["id"])
    assert second_customer.id == int(user_id)


def test_query_customers_search_without_duplications(
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    permission_manage_orders,
):
    customer = User.objects.create(email="david@example.com")
    customer.addresses.create(first_name="David")
    customer.addresses.create(first_name="David")
    customer.search_document = prepare_user_search_document_value(customer)
    customer.save(update_fields=["search_document"])

    variables = {"filter": {"search": "David"}}
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]
    assert len(users) == 1

    response = staff_api_client.post_graphql(
        query_customer_with_filter,
        variables,
        permissions=[permission_manage_orders],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]
    assert len(users) == 1


def test_query_customers_with_permission_manage_orders(
    query_customer_with_filter,
    customer_user,
    staff_api_client,
    permission_manage_orders,
):
    variables = {"filter": {}}

    response = staff_api_client.post_graphql(
        query_customer_with_filter,
        variables,
        permissions=[permission_manage_orders],
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["totalCount"]
    assert users == 1


QUERY_CUSTOMERS_WITH_SORT = """
    query ($sort_by: UserSortingInput!) {
        customers(first:5, sortBy: $sort_by) {
                edges{
                    node{
                        firstName
                    }
                }
            }
        }
"""


@pytest.mark.parametrize(
    "customer_sort, result_order",
    [
        ({"field": "FIRST_NAME", "direction": "ASC"}, ["Joe", "John", "Leslie"]),
        ({"field": "FIRST_NAME", "direction": "DESC"}, ["Leslie", "John", "Joe"]),
        ({"field": "LAST_NAME", "direction": "ASC"}, ["John", "Joe", "Leslie"]),
        ({"field": "LAST_NAME", "direction": "DESC"}, ["Leslie", "Joe", "John"]),
        ({"field": "EMAIL", "direction": "ASC"}, ["John", "Leslie", "Joe"]),
        ({"field": "EMAIL", "direction": "DESC"}, ["Joe", "Leslie", "John"]),
        ({"field": "ORDER_COUNT", "direction": "ASC"}, ["John", "Leslie", "Joe"]),
        ({"field": "ORDER_COUNT", "direction": "DESC"}, ["Joe", "Leslie", "John"]),
        ({"field": "CREATED_AT", "direction": "ASC"}, ["John", "Joe", "Leslie"]),
        ({"field": "CREATED_AT", "direction": "DESC"}, ["Leslie", "Joe", "John"]),
        ({"field": "LAST_MODIFIED_AT", "direction": "ASC"}, ["Leslie", "John", "Joe"]),
        ({"field": "LAST_MODIFIED_AT", "direction": "DESC"}, ["Joe", "John", "Leslie"]),
    ],
)
def test_query_customers_with_sort(
    customer_sort, result_order, staff_api_client, permission_manage_users, channel_USD
):
    users = User.objects.bulk_create(
        [
            User(
                first_name="John",
                last_name="Allen",
                email="allen@example.com",
                is_staff=False,
                is_active=True,
            ),
            User(
                first_name="Joe",
                last_name="Doe",
                email="zordon01@example.com",
                is_staff=False,
                is_active=True,
            ),
            User(
                first_name="Leslie",
                last_name="Wade",
                email="leslie@example.com",
                is_staff=False,
                is_active=True,
            ),
        ]
    )

    users[2].save()
    users[0].save()
    users[1].save()

    Order.objects.create(
        user=User.objects.get(email="zordon01@example.com"), channel=channel_USD
    )

    variables = {"sort_by": customer_sort}
    staff_api_client.user.user_permissions.add(permission_manage_users)
    response = staff_api_client.post_graphql(QUERY_CUSTOMERS_WITH_SORT, variables)
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]

    for order, user_first_name in enumerate(result_order):
        assert users[order]["node"]["firstName"] == user_first_name


@pytest.mark.parametrize(
    "customer_filter, count",
    [
        ({"search": "mirumee.com"}, 2),
        ({"search": "Alice"}, 1),
        ({"search": "Kowalski"}, 1),
        ({"search": "John"}, 1),  # first_name
        ({"search": "Doe"}, 1),  # last_name
        ({"search": "wroc"}, 1),  # city
        ({"search": "pl"}, 1),  # country
        ({"search": "+48713988102"}, 1),
        ({"search": "alice Kowalski"}, 1),
        ({"search": "kowalski alice"}, 1),
        ({"search": "John doe"}, 1),
        ({"search": "Alice Doe"}, 0),
    ],
)
def test_query_customer_members_with_filter_search(
    customer_filter,
    count,
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    address,
    staff_user,
):
    users = User.objects.bulk_create(
        [
            User(
                email="second@mirumee.com",
                first_name="Alice",
                last_name="Kowalski",
                is_active=False,
            ),
            User(
                email="third@mirumee.com",
                is_active=True,
            ),
        ]
    )
    users[1].addresses.set([address])

    for user in users:
        user.search_document = prepare_user_search_document_value(user)
    User.objects.bulk_update(users, ["search_document"])

    variables = {"filter": customer_filter}
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    users = content["data"]["customers"]["edges"]

    assert len(users) == count


def test_query_customers_with_filter_by_one_id(
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_users,
):
    # given
    search_user = customer_users[0]

    variables = {
        "filter": {
            "ids": [graphene.Node.to_global_id("User", search_user.pk)],
        }
    }

    # when
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    result_user = content["data"]["customers"]["edges"][0]
    _, id = graphene.Node.from_global_id(result_user["node"]["id"])
    assert id == str(search_user.pk)


def test_query_customers_with_filter_by_multiple_ids(
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_users,
):
    # given
    search_users = [customer_users[0], customer_users[1]]
    search_users_ids = [
        graphene.Node.to_global_id("User", user.pk) for user in search_users
    ]

    variables = {"filter": {"ids": search_users_ids}}

    # when
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    result_users = content["data"]["customers"]["edges"]
    expected_ids = [str(user.pk) for user in customer_users]

    assert len(result_users) == len(search_users)
    for result_user in result_users:
        _, id = graphene.Node.from_global_id(result_user["node"]["id"])
        assert id in expected_ids


def test_query_customers_with_filter_by_empty_list(
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_users,
):
    # given
    variables = {"filter": {"ids": []}}

    # when
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    result_users = content["data"]["customers"]["edges"]
    expected_ids = [str(user.pk) for user in customer_users]

    assert len(result_users) == len(customer_users)
    for result_user in result_users:
        _, id = graphene.Node.from_global_id(result_user["node"]["id"])
        assert id in expected_ids


def test_query_customers_with_filter_by_not_existing_id(
    query_customer_with_filter,
    staff_api_client,
    permission_manage_users,
    customer_users,
):
    # given
    search_pk = max([user.pk for user in customer_users]) + 1
    search_id = graphene.Node.to_global_id("User", search_pk)
    variables = {"filter": {"ids": [search_id]}}

    # when
    response = staff_api_client.post_graphql(
        query_customer_with_filter, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)

    # then
    result_users = content["data"]["customers"]["edges"]

    assert len(result_users) == 0


@pytest.mark.parametrize(
    "staff_member_filter, count",
    [({"status": "DEACTIVATED"}, 1), ({"status": "ACTIVE"}, 2)],
)
def test_query_staff_members_with_filter_status(
    staff_member_filter,
    count,
    query_staff_users_with_filter,
    staff_api_client,
    permission_manage_staff,
    staff_user,
):
    User.objects.bulk_create(
        [
            User(email="second@example.com", is_staff=True, is_active=False),
            User(email="third@example.com", is_staff=True, is_active=True),
        ]
    )

    variables = {"filter": staff_member_filter}
    response = staff_api_client.post_graphql(
        query_staff_users_with_filter, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    users = content["data"]["staffUsers"]["edges"]

    assert len(users) == count


def test_query_staff_members_with_filter_by_ids(
    query_staff_users_with_filter,
    staff_api_client,
    permission_manage_staff,
    staff_user,
):
    # given
    variables = {
        "filter": {
            "ids": [graphene.Node.to_global_id("User", staff_user.pk)],
        }
    }

    # when
    response = staff_api_client.post_graphql(
        query_staff_users_with_filter, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)

    # then
    users = content["data"]["staffUsers"]["edges"]
    assert len(users) == 1


@pytest.mark.parametrize(
    "staff_member_filter, count",
    [
        ({"search": "mirumee.com"}, 2),
        ({"search": "alice"}, 1),
        ({"search": "kowalski"}, 1),
        ({"search": "John"}, 1),  # first_name
        ({"search": "Doe"}, 1),  # last_name
        ({"search": "irv"}, 1),  # city
        ({"search": "us"}, 1),  # country
        ({"search": "Alice Kowalski"}, 1),
        ({"search": "Kowalski Alice"}, 1),
        ({"search": "john doe"}, 1),
        ({"search": "Alice Doe"}, 0),
    ],
)
def test_query_staff_members_with_filter_search(
    staff_member_filter,
    count,
    query_staff_users_with_filter,
    staff_api_client,
    permission_manage_staff,
    address_usa,
    staff_user,
):
    users = User.objects.bulk_create(
        [
            User(
                email="second@mirumee.com",
                first_name="Alice",
                last_name="Kowalski",
                is_staff=True,
                is_active=False,
            ),
            User(
                email="third@mirumee.com",
                is_staff=True,
                is_active=True,
            ),
            User(
                email="customer@mirumee.com",
                first_name="Alice",
                last_name="Kowalski",
                is_staff=False,
                is_active=True,
            ),
        ]
    )
    users[1].addresses.set([address_usa])
    for user in users:
        user.search_document = prepare_user_search_document_value(user)
    User.objects.bulk_update(users, ["search_document"])

    variables = {"filter": staff_member_filter}
    response = staff_api_client.post_graphql(
        query_staff_users_with_filter, variables, permissions=[permission_manage_staff]
    )
    content = get_graphql_content(response)
    users = content["data"]["staffUsers"]["edges"]

    assert len(users) == count


QUERY_STAFF_USERS_WITH_SORT = """
    query ($sort_by: UserSortingInput!) {
        staffUsers(first:5, sortBy: $sort_by) {
                edges{
                    node{
                        firstName
                    }
                }
            }
        }
"""


@pytest.mark.parametrize(
    "customer_sort, result_order",
    [
        # Empty string in result is first_name for staff_api_client.
        ({"field": "FIRST_NAME", "direction": "ASC"}, ["", "Joe", "John", "Leslie"]),
        ({"field": "FIRST_NAME", "direction": "DESC"}, ["Leslie", "John", "Joe", ""]),
        ({"field": "LAST_NAME", "direction": "ASC"}, ["", "John", "Joe", "Leslie"]),
        ({"field": "LAST_NAME", "direction": "DESC"}, ["Leslie", "Joe", "John", ""]),
        ({"field": "EMAIL", "direction": "ASC"}, ["John", "Leslie", "", "Joe"]),
        ({"field": "EMAIL", "direction": "DESC"}, ["Joe", "", "Leslie", "John"]),
        ({"field": "ORDER_COUNT", "direction": "ASC"}, ["John", "Leslie", "", "Joe"]),
        ({"field": "ORDER_COUNT", "direction": "DESC"}, ["Joe", "", "Leslie", "John"]),
    ],
)
def test_query_staff_members_with_sort(
    customer_sort, result_order, staff_api_client, permission_manage_staff, channel_USD
):
    User.objects.bulk_create(
        [
            User(
                first_name="John",
                last_name="Allen",
                email="allen@example.com",
                is_staff=True,
                is_active=True,
            ),
            User(
                first_name="Joe",
                last_name="Doe",
                email="zordon01@example.com",
                is_staff=True,
                is_active=True,
            ),
            User(
                first_name="Leslie",
                last_name="Wade",
                email="leslie@example.com",
                is_staff=True,
                is_active=True,
            ),
        ]
    )
    Order.objects.create(
        user=User.objects.get(email="zordon01@example.com"), channel=channel_USD
    )
    variables = {"sort_by": customer_sort}
    staff_api_client.user.user_permissions.add(permission_manage_staff)
    response = staff_api_client.post_graphql(QUERY_STAFF_USERS_WITH_SORT, variables)
    content = get_graphql_content(response)
    users = content["data"]["staffUsers"]["edges"]

    for order, user_first_name in enumerate(result_order):
        assert users[order]["node"]["firstName"] == user_first_name


USER_CHANGE_ACTIVE_STATUS_MUTATION = """
    mutation userChangeActiveStatus($ids: [ID!]!, $is_active: Boolean!) {
        userBulkSetActive(ids: $ids, isActive: $is_active) {
            count
            errors {
                field
                message
            }
        }
    }
    """


def test_staff_bulk_set_active(
    staff_api_client, user_list_not_active, permission_manage_users
):
    users = user_list_not_active
    active_status = True
    variables = {
        "ids": [graphene.Node.to_global_id("User", user.id) for user in users],
        "is_active": active_status,
    }
    response = staff_api_client.post_graphql(
        USER_CHANGE_ACTIVE_STATUS_MUTATION,
        variables,
        permissions=[permission_manage_users],
    )
    content = get_graphql_content(response)
    data = content["data"]["userBulkSetActive"]
    assert data["count"] == users.count()
    users = User.objects.filter(pk__in=[user.pk for user in users])
    assert all(user.is_active for user in users)


def test_staff_bulk_set_not_active(
    staff_api_client, user_list, permission_manage_users
):
    users = user_list
    active_status = False
    variables = {
        "ids": [graphene.Node.to_global_id("User", user.id) for user in users],
        "is_active": active_status,
    }
    response = staff_api_client.post_graphql(
        USER_CHANGE_ACTIVE_STATUS_MUTATION,
        variables,
        permissions=[permission_manage_users],
    )
    content = get_graphql_content(response)
    data = content["data"]["userBulkSetActive"]
    assert data["count"] == len(users)
    users = User.objects.filter(pk__in=[user.pk for user in users])
    assert not any(user.is_active for user in users)


def test_change_active_status_for_superuser(
    staff_api_client, superuser, permission_manage_users
):
    users = [superuser]
    superuser_id = graphene.Node.to_global_id("User", superuser.id)
    active_status = False
    variables = {
        "ids": [graphene.Node.to_global_id("User", user.id) for user in users],
        "is_active": active_status,
    }
    response = staff_api_client.post_graphql(
        USER_CHANGE_ACTIVE_STATUS_MUTATION,
        variables,
        permissions=[permission_manage_users],
    )
    content = get_graphql_content(response)
    data = content["data"]["userBulkSetActive"]
    assert data["errors"][0]["field"] == superuser_id
    assert (
        data["errors"][0]["message"] == "Cannot activate or deactivate "
        "superuser's account."
    )


def test_change_active_status_for_himself(staff_api_client, permission_manage_users):
    users = [staff_api_client.user]
    user_id = graphene.Node.to_global_id("User", staff_api_client.user.id)
    active_status = False
    variables = {
        "ids": [graphene.Node.to_global_id("User", user.id) for user in users],
        "is_active": active_status,
    }
    response = staff_api_client.post_graphql(
        USER_CHANGE_ACTIVE_STATUS_MUTATION,
        variables,
        permissions=[permission_manage_users],
    )
    content = get_graphql_content(response)
    data = content["data"]["userBulkSetActive"]
    assert data["errors"][0]["field"] == user_id
    assert (
        data["errors"][0]["message"] == "Cannot activate or deactivate "
        "your own account."
    )


ADDRESS_QUERY = """
query address($id: ID!) {
    address(id: $id) {
        postalCode
        lastName
        firstName
        city
        country {
          code
        }
    }
}
"""


def test_address_query_as_owner(user_api_client, customer_user):
    address = customer_user.addresses.first()
    variables = {"id": graphene.Node.to_global_id("Address", address.pk)}
    response = user_api_client.post_graphql(ADDRESS_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["address"]
    assert data["country"]["code"] == address.country.code


def test_address_query_as_not_owner(
    user_api_client, customer_user, address_other_country
):
    variables = {"id": graphene.Node.to_global_id("Address", address_other_country.pk)}
    response = user_api_client.post_graphql(ADDRESS_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["address"]
    assert not data


def test_address_query_as_app_with_permission(
    app_api_client,
    address_other_country,
    permission_manage_users,
):
    variables = {"id": graphene.Node.to_global_id("Address", address_other_country.pk)}
    response = app_api_client.post_graphql(
        ADDRESS_QUERY, variables, permissions=[permission_manage_users]
    )
    content = get_graphql_content(response)
    data = content["data"]["address"]
    assert data["country"]["code"] == address_other_country.country.code


def test_address_query_as_app_without_permission(
    app_api_client, app, address_other_country
):
    variables = {"id": graphene.Node.to_global_id("Address", address_other_country.pk)}
    response = app_api_client.post_graphql(ADDRESS_QUERY, variables)
    assert_no_permission(response)


def test_address_query_as_anonymous_user(api_client, address_other_country):
    variables = {"id": graphene.Node.to_global_id("Address", address_other_country.pk)}
    response = api_client.post_graphql(ADDRESS_QUERY, variables)
    assert_no_permission(response)


def test_address_query_invalid_id(
    staff_api_client,
    address_other_country,
):
    id = "..afs"
    variables = {"id": id}
    response = staff_api_client.post_graphql(ADDRESS_QUERY, variables)
    content = get_graphql_content_from_response(response)
    assert len(content["errors"]) == 1
    assert content["errors"][0]["message"] == f"Couldn't resolve id: {id}."
    assert content["data"]["address"] is None


def test_address_query_with_invalid_object_type(
    staff_api_client,
    address_other_country,
):
    variables = {"id": graphene.Node.to_global_id("Order", address_other_country.pk)}
    response = staff_api_client.post_graphql(ADDRESS_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["address"] is None


REQUEST_EMAIL_CHANGE_QUERY = """
mutation requestEmailChange(
    $password: String!, $new_email: String!, $redirect_url: String!, $channel:String
) {
    requestEmailChange(
        password: $password,
        newEmail: $new_email,
        redirectUrl: $redirect_url,
        channel: $channel
    ) {
        user {
            email
        }
        errors {
            code
            message
            field
        }
  }
}
"""


@freeze_time("2018-05-31 12:00:01")
@patch("saleor.plugins.manager.PluginsManager.notify")
def test_account_request_email_change_with_upper_case_email(
    mocked_notify,
    user_api_client,
    customer_user,
    site_settings,
    channel_PLN,
):
    # given
    new_email = "NEW_EMAIL@example.com"
    redirect_url = "https://www.example.com"
    variables = {
        "new_email": new_email,
        "redirect_url": redirect_url,
        "password": "password",
        "channel": channel_PLN.slug,
    }
    token_payload = {
        "old_email": customer_user.email,
        "new_email": new_email.lower(),
        "user_pk": customer_user.pk,
    }
    token = create_token(token_payload, settings.JWT_TTL_REQUEST_EMAIL_CHANGE)

    # when
    response = user_api_client.post_graphql(REQUEST_EMAIL_CHANGE_QUERY, variables)
    content = get_graphql_content(response)

    # then
    data = content["data"]["requestEmailChange"]
    assert not data["errors"]

    params = urlencode({"token": token})
    redirect_url = prepare_url(params, redirect_url)
    expected_payload = {
        "user": get_default_user_payload(customer_user),
        "recipient_email": new_email.lower(),
        "token": token,
        "redirect_url": redirect_url,
        "old_email": customer_user.email,
        "new_email": new_email.lower(),
        "channel_slug": channel_PLN.slug,
        **get_site_context_payload(site_settings.site),
    }

    mocked_notify.assert_called_once_with(
        NotifyEventType.ACCOUNT_CHANGE_EMAIL_REQUEST,
        payload=expected_payload,
        channel_slug=channel_PLN.slug,
    )


def test_request_email_change(user_api_client, customer_user, channel_PLN):
    variables = {
        "password": "password",
        "new_email": "new_email@example.com",
        "redirect_url": "http://www.example.com",
        "channel": channel_PLN.slug,
    }

    response = user_api_client.post_graphql(REQUEST_EMAIL_CHANGE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestEmailChange"]
    assert data["user"]["email"] == customer_user.email


def test_request_email_change_to_existing_email(
    user_api_client, customer_user, staff_user
):
    variables = {
        "password": "password",
        "new_email": staff_user.email,
        "redirect_url": "http://www.example.com",
    }

    response = user_api_client.post_graphql(REQUEST_EMAIL_CHANGE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestEmailChange"]
    assert not data["user"]
    assert data["errors"] == [
        {
            "code": "UNIQUE",
            "message": "Email is used by other user.",
            "field": "newEmail",
        }
    ]


def test_request_email_change_with_invalid_redirect_url(
    user_api_client, customer_user, staff_user
):
    variables = {
        "password": "password",
        "new_email": "new_email@example.com",
        "redirect_url": "www.example.com",
    }

    response = user_api_client.post_graphql(REQUEST_EMAIL_CHANGE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestEmailChange"]
    assert not data["user"]
    assert data["errors"] == [
        {
            "code": "INVALID",
            "message": "Invalid URL. Please check if URL is in RFC 1808 format.",
            "field": "redirectUrl",
        }
    ]


def test_request_email_change_with_invalid_password(user_api_client, customer_user):
    variables = {
        "password": "spanishinquisition",
        "new_email": "new_email@example.com",
        "redirect_url": "http://www.example.com",
    }
    response = user_api_client.post_graphql(REQUEST_EMAIL_CHANGE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["requestEmailChange"]
    assert not data["user"]
    assert data["errors"][0]["code"] == AccountErrorCode.INVALID_CREDENTIALS.name
    assert data["errors"][0]["field"] == "password"


EMAIL_UPDATE_QUERY = """
mutation emailUpdate($token: String!, $channel: String) {
    confirmEmailChange(token: $token, channel: $channel){
        user {
            email
        }
        errors {
            code
            message
            field
        }
  }
}
"""


@patch("saleor.graphql.account.mutations.account.match_orders_with_new_user")
@patch("saleor.graphql.account.mutations.account.assign_user_gift_cards")
def test_email_update(
    assign_gift_cards_mock,
    assign_orders_mock,
    user_api_client,
    customer_user,
    channel_PLN,
):
    new_email = "new_email@example.com"
    payload = {
        "old_email": customer_user.email,
        "new_email": new_email,
        "user_pk": customer_user.pk,
    }
    user = user_api_client.user

    token = create_token(payload, timedelta(hours=1))
    variables = {"token": token, "channel": channel_PLN.slug}

    response = user_api_client.post_graphql(EMAIL_UPDATE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["confirmEmailChange"]
    assert data["user"]["email"] == new_email
    user.refresh_from_db()
    assert new_email in user.search_document
    assign_gift_cards_mock.assert_called_once_with(customer_user)
    assign_orders_mock.assert_called_once_with(customer_user)


def test_email_update_to_existing_email(user_api_client, customer_user, staff_user):
    payload = {
        "old_email": customer_user.email,
        "new_email": staff_user.email,
        "user_pk": customer_user.pk,
    }
    token = create_token(payload, timedelta(hours=1))
    variables = {"token": token}

    response = user_api_client.post_graphql(EMAIL_UPDATE_QUERY, variables)
    content = get_graphql_content(response)
    data = content["data"]["confirmEmailChange"]
    assert not data["user"]
    assert data["errors"] == [
        {
            "code": "UNIQUE",
            "message": "Email is used by other user.",
            "field": "newEmail",
        }
    ]


USER_FEDERATION_QUERY = """
  query GetUserInFederation($representations: [_Any]) {
    _entities(representations: $representations) {
      __typename
      ... on User {
        id
        email
      }
    }
  }
"""


def test_staff_query_user_by_id_for_federation(
    staff_api_client, customer_user, permission_manage_users
):
    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "id": customer_user_id,
            },
        ],
    }

    response = staff_api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
        permissions=[permission_manage_users],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "User",
            "id": customer_user_id,
            "email": customer_user.email,
        }
    ]


def test_staff_query_user_by_email_for_federation(
    staff_api_client, customer_user, permission_manage_users
):
    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "email": customer_user.email,
            },
        ],
    }

    response = staff_api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
        permissions=[permission_manage_users],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "User",
            "id": customer_user_id,
            "email": customer_user.email,
        }
    ]


def test_staff_query_user_by_id_without_permission_for_federation(
    staff_api_client, customer_user
):
    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "id": customer_user_id,
            },
        ],
    }

    response = staff_api_client.post_graphql(USER_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_staff_query_user_by_email_without_permission_for_federation(
    staff_api_client, customer_user
):
    variables = {
        "representations": [
            {
                "__typename": "User",
                "email": customer_user.email,
            },
        ],
    }

    response = staff_api_client.post_graphql(USER_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_customer_query_self_by_id_for_federation(user_api_client, customer_user):
    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "id": customer_user_id,
            },
        ],
    }

    response = user_api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "User",
            "id": customer_user_id,
            "email": customer_user.email,
        }
    ]


def test_customer_query_self_by_email_for_federation(user_api_client, customer_user):
    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "email": customer_user.email,
            },
        ],
    }

    response = user_api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "User",
            "id": customer_user_id,
            "email": customer_user.email,
        }
    ]


def test_customer_query_user_by_id_for_federation(
    user_api_client, customer_user, staff_user
):
    staff_user_id = graphene.Node.to_global_id("User", staff_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "id": staff_user_id,
            },
        ],
    }

    response = user_api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_customer_query_user_by_email_for_federation(
    user_api_client, customer_user, staff_user
):
    variables = {
        "representations": [
            {
                "__typename": "User",
                "email": staff_user.email,
            },
        ],
    }

    response = user_api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_unauthenticated_query_user_by_id_for_federation(api_client, customer_user):
    customer_user_id = graphene.Node.to_global_id("User", customer_user.pk)
    variables = {
        "representations": [
            {
                "__typename": "User",
                "id": customer_user_id,
            },
        ],
    }

    response = api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_unauthenticated_query_user_by_email_for_federation(api_client, customer_user):
    variables = {
        "representations": [
            {
                "__typename": "User",
                "email": customer_user.email,
            },
        ],
    }

    response = api_client.post_graphql(
        USER_FEDERATION_QUERY,
        variables,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


ADDRESS_FEDERATION_QUERY = """
  query GetUserInFederation($representations: [_Any]) {
    _entities(representations: $representations) {
      __typename
      ... on Address {
        id
        city
      }
    }
  }
"""


def test_customer_query_address_federation(user_api_client, customer_user, address):
    customer_user.addresses.add(address)

    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = user_api_client.post_graphql(ADDRESS_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "Address",
            "id": address_id,
            "city": address.city,
        }
    ]


def test_customer_query_other_user_address_federation(
    user_api_client, staff_user, customer_user, address
):
    staff_user.addresses.add(address)

    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = user_api_client.post_graphql(ADDRESS_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_staff_query_other_user_address_federation(
    staff_api_client, customer_user, address
):
    customer_user.addresses.add(address)

    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = staff_api_client.post_graphql(ADDRESS_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_staff_query_other_user_address_with_permission_federation(
    staff_api_client, customer_user, address, permission_manage_users
):
    customer_user.addresses.add(address)

    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = staff_api_client.post_graphql(
        ADDRESS_FEDERATION_QUERY,
        variables,
        permissions=[permission_manage_users],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_app_query_address_federation(app_api_client, address, permission_manage_users):
    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = app_api_client.post_graphql(
        ADDRESS_FEDERATION_QUERY,
        variables,
        permissions=[permission_manage_users],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "Address",
            "id": address_id,
            "city": address.city,
        }
    ]


def test_app_no_permission_query_address_federation(app_api_client, address):
    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = app_api_client.post_graphql(ADDRESS_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_unauthenticated_query_address_federation(api_client, address):
    address_id = graphene.Node.to_global_id("Address", address.pk)
    variables = {
        "representations": [
            {
                "__typename": "Address",
                "id": address_id,
            },
        ],
    }

    response = api_client.post_graphql(ADDRESS_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


GROUP_FEDERATION_QUERY = """
  query GetGroupInFederation($representations: [_Any]) {
    _entities(representations: $representations) {
      __typename
      ... on Group {
        id
        name
      }
    }
  }
"""


def test_staff_query_group_federation(staff_api_client, permission_manage_staff):
    group = Group.objects.create(name="empty group")
    group_id = graphene.Node.to_global_id("Group", group.pk)
    variables = {
        "representations": [
            {
                "__typename": "Group",
                "id": group_id,
            },
        ],
    }

    response = staff_api_client.post_graphql(
        GROUP_FEDERATION_QUERY,
        variables,
        permissions=[permission_manage_staff],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "Group",
            "id": group_id,
            "name": group.name,
        }
    ]


def test_app_query_group_federation(app_api_client, permission_manage_staff):
    group = Group.objects.create(name="empty group")
    group_id = graphene.Node.to_global_id("Group", group.pk)
    variables = {
        "representations": [
            {
                "__typename": "Group",
                "id": group_id,
            },
        ],
    }

    response = app_api_client.post_graphql(
        GROUP_FEDERATION_QUERY,
        variables,
        permissions=[permission_manage_staff],
        check_no_permissions=False,
    )
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [
        {
            "__typename": "Group",
            "id": group_id,
            "name": group.name,
        }
    ]


def test_app_no_permission_query_group_federation(app_api_client):
    group = Group.objects.create(name="empty group")
    group_id = graphene.Node.to_global_id("Group", group.pk)
    variables = {
        "representations": [
            {
                "__typename": "Group",
                "id": group_id,
            },
        ],
    }

    response = app_api_client.post_graphql(GROUP_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_client_query_group_federation(user_api_client):
    group = Group.objects.create(name="empty group")

    group_id = graphene.Node.to_global_id("Group", group.pk)
    variables = {
        "representations": [
            {
                "__typename": "Group",
                "id": group_id,
            },
        ],
    }

    response = user_api_client.post_graphql(GROUP_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]


def test_unauthenticated_query_group_federation(api_client):
    group = Group.objects.create(name="empty group")

    group_id = graphene.Node.to_global_id("Group", group.pk)
    variables = {
        "representations": [
            {
                "__typename": "Group",
                "id": group_id,
            },
        ],
    }

    response = api_client.post_graphql(GROUP_FEDERATION_QUERY, variables)
    content = get_graphql_content(response)
    assert content["data"]["_entities"] == [None]
