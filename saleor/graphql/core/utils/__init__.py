import binascii
import os
import secrets
from typing import Literal, Tuple, Type, Union, overload

import graphene
from django.core.exceptions import ValidationError
from graphene import ObjectType
from graphql.error import GraphQLError

from ....plugins.webhook.utils import APP_ID_PREFIX
from ..validators import validate_if_int_or_uuid


def snake_to_camel_case(name):
    """Convert snake_case variable name to camelCase."""
    if isinstance(name, str):
        split_name = name.split("_")
        return split_name[0] + "".join(map(str.capitalize, split_name[1:]))
    return name


def str_to_enum(name):
    """Create an enum value from a string."""
    return name.replace(" ", "_").replace("-", "_").upper()


def get_duplicates_items(first_list, second_list):
    """Return items that appear on both provided lists."""
    if first_list and second_list:
        return set(first_list) & set(second_list)
    return []


def get_duplicated_values(values):
    """Return set of duplicated values."""
    return {value for value in values if values.count(value) > 1}


@overload
def from_global_id_or_error(
    global_id: str,
    only_type: Union[ObjectType, str, None] = None,
    raise_error: Literal[True] = True,
) -> Tuple[str, str]:
    ...


@overload
def from_global_id_or_error(
    global_id: str,
    only_type: Union[Type[ObjectType], str, None] = None,
    raise_error: bool = False,
) -> Union[Tuple[str, str], Tuple[str, None]]:
    ...


def from_global_id_or_error(
    global_id: str,
    only_type: Union[Type[ObjectType], str, None] = None,
    raise_error: bool = False,
):
    """Resolve global ID or raise GraphQLError.

    Validates if given ID is a proper ID handled by Saleor.
    Valid IDs formats, base64 encoded:
    'app:<int>:<str>' : External app ID with 'app' prefix
    '<type>:<int>' : Internal ID containing object type and ID as integer
    '<type>:<UUID>' : Internal ID containing object type and UUID
    Optionally validate the object type, if `only_type` is provided,
    raise GraphQLError when `raise_error` is set to True.

    Returns tuple: (type, id).
    """
    try:
        type_, id_ = graphene.Node.from_global_id(global_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise GraphQLError(f"Couldn't resolve id: {global_id}.")
    if type_ == APP_ID_PREFIX:
        id_ = global_id
    else:
        if not validate_if_int_or_uuid(id_):
            raise GraphQLError(f"Error occurred during ID - {global_id} validation.")

    if only_type and str(type_) != str(only_type):
        if not raise_error:
            return type_, None
        raise GraphQLError(f"Must receive a {only_type} id.")
    return type_, id_


def from_global_id_or_none(
    global_id, only_type: Union[ObjectType, str, None] = None, raise_error: bool = False
):
    if not global_id:
        return None

    return from_global_id_or_error(global_id, only_type, raise_error)[1]


def to_global_id_or_none(instance):
    class_name = instance.__class__.__name__
    if instance is None or instance.pk is None:
        return None
    return graphene.Node.to_global_id(class_name, instance.pk)


def add_hash_to_file_name(file):
    """Add unique text fragment to the file name to prevent file overriding."""
    file_name, format = os.path.splitext(file._name)
    hash = secrets.token_hex(nbytes=4)
    new_name = f"{file_name}_{hash}{format}"
    file._name = new_name


def raise_validation_error(field=None, message=None, code=None):
    raise ValidationError({field: ValidationError(message, code=code)})


def ext_ref_to_global_id_or_error(model, external_reference):
    """Convert external reference to graphen global id."""
    internal_id = (
        model.objects.filter(external_reference=external_reference)
        .values_list("id", flat=True)
        .first()
    )
    if internal_id:
        return graphene.Node.to_global_id(model.__name__, internal_id)
    else:
        raise_validation_error(
            field="externalReference",
            message=f"Couldn't resolve to a node: {external_reference}",
            code="not_found",
        )
