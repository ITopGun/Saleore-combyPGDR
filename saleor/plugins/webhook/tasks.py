import json
import logging
from dataclasses import dataclass
from enum import Enum
from json import JSONDecodeError
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, TypeVar
from urllib.parse import unquote, urlparse, urlunparse

import boto3
import requests
from botocore.exceptions import ClientError
from celery import group
from celery.exceptions import MaxRetriesExceededError, Retry
from celery.utils.log import get_task_logger
from django.conf import settings
from django.urls import reverse
from google.cloud import pubsub_v1
from requests.exceptions import RequestException

from ...app.headers import AppHeaders, DeprecatedAppHeaders
from ...celeryconf import app
from ...core import EventDeliveryStatus
from ...core.models import EventDelivery, EventPayload
from ...core.tracing import webhooks_opentracing_trace
from ...core.utils import build_absolute_uri
from ...graphql.webhook.subscription_payload import (
    generate_payload_from_subscription,
    initialize_request,
)
from ...graphql.webhook.subscription_types import WEBHOOK_TYPES_MAP
from ...payment import PaymentError
from ...site.models import Site
from ...webhook import observability
from ...webhook.event_types import WebhookEventAsyncType, WebhookEventSyncType
from ...webhook.observability import WebhookData
from ...webhook.utils import get_webhooks_for_event
from . import signature_for_payload
from .utils import (
    attempt_update,
    catch_duration_time,
    clear_successful_delivery,
    create_attempt,
    create_event_delivery_list_for_webhooks,
    delivery_update,
)

if TYPE_CHECKING:
    from ...webhook.models import Webhook

logger = logging.getLogger(__name__)
task_logger = get_task_logger(__name__)


class WebhookSchemes(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    AWS_SQS = "awssqs"
    GOOGLE_CLOUD_PUBSUB = "gcpubsub"


@dataclass
class WebhookResponse:
    content: str
    request_headers: Optional[Dict] = None
    response_headers: Optional[Dict] = None
    response_status_code: Optional[int] = None
    status: str = EventDeliveryStatus.SUCCESS
    duration: float = 0.0


def create_deliveries_for_subscriptions(
    event_type, subscribable_object, webhooks, requestor=None
) -> List[EventDelivery]:
    """Create a list of event deliveries with payloads based on subscription query.

    It uses a subscription query, defined for webhook to explicitly determine
    what fields should be included in the payload.

    :param event_type: event type which should be triggered.
    :param subscribable_object: subscribable object to process via subscription query.
    :param webhooks: sequence of async webhooks.
    :param requestor: used in subscription webhooks to generate meta data for payload.
    :return: List of event deliveries to send via webhook tasks.
    """
    if event_type not in WEBHOOK_TYPES_MAP:
        logger.info(
            "Skipping subscription webhook. Event %s is not subscribable.", event_type
        )
        return []

    event_payloads = []
    event_deliveries = []
    for webhook in webhooks:
        data = generate_payload_from_subscription(
            event_type=event_type,
            subscribable_object=subscribable_object,
            subscription_query=webhook.subscription_query,
            request=initialize_request(
                requestor, event_type in WebhookEventSyncType.ALL
            ),
            app=webhook.app,
        )
        if not data:
            logger.warning(
                "No payload was generated with subscription for event: %s" % event_type
            )
            continue

        event_payload = EventPayload(payload=json.dumps({**data}))
        event_payloads.append(event_payload)
        event_deliveries.append(
            EventDelivery(
                status=EventDeliveryStatus.PENDING,
                event_type=event_type,
                payload=event_payload,
                webhook=webhook,
            )
        )

    EventPayload.objects.bulk_create(event_payloads)
    return EventDelivery.objects.bulk_create(event_deliveries)


def create_delivery_for_subscription_sync_event(
    event_type, subscribable_object, webhook, requestor=None, request=None
) -> Optional[EventDelivery]:
    """Generate webhook payload based on subscription query and create delivery object.

    It uses a defined subscription query, defined for webhook to explicitly determine
    what fields should be included in the payload.

    :param event_type: event type which should be triggered.
    :param subscribable_object: subscribable object to process via subscription query.
    :param webhook: webhook object for which delivery will be created.
    :param requestor: used in subscription webhooks to generate meta data for payload.
    :param request: used to share context between sync event calls
    :return: List of event deliveries to send via webhook tasks.
    """
    if event_type not in WEBHOOK_TYPES_MAP:
        logger.info(
            "Skipping subscription webhook. Event %s is not subscribable.", event_type
        )
        return None

    if not request:
        request = initialize_request(requestor, event_type in WebhookEventSyncType.ALL)

    data = generate_payload_from_subscription(
        event_type=event_type,
        subscribable_object=subscribable_object,
        subscription_query=webhook.subscription_query,
        request=request,
        app=webhook.app,
    )
    if not data:
        # PaymentError is a temporary exception type. New type will be implemented
        # in separate PR to ensure proper handling for all sync events.
        # It was implemented when sync webhooks were handling payment events only.
        raise PaymentError(
            f"No payload was generated with subscription for event: {event_type}"
        )
    event_payload = EventPayload.objects.create(payload=json.dumps({**data}))
    event_delivery = EventDelivery.objects.create(
        status=EventDeliveryStatus.PENDING,
        event_type=event_type,
        payload=event_payload,
        webhook=webhook,
    )
    return event_delivery


def trigger_webhooks_async(
    data, event_type, webhooks, subscribable_object=None, requestor=None
):
    """Trigger async webhooks - both regular and subscription.

    :param data: used as payload in regular webhooks.
    :param event_type: used in both webhook types as event type.
    :param webhooks: used in both webhook types, queryset of async webhooks.
    :param subscribable_object: subscribable object used in subscription webhooks.
    :param requestor: used in subscription webhooks to generate meta data for payload.
    """
    regular_webhooks, subscription_webhooks = group_webhooks_by_subscription(webhooks)
    deliveries = []

    if regular_webhooks:
        payload = EventPayload.objects.create(payload=data)
        deliveries.extend(
            create_event_delivery_list_for_webhooks(
                webhooks=regular_webhooks,
                event_payload=payload,
                event_type=event_type,
            )
        )
    if subscription_webhooks:
        deliveries.extend(
            create_deliveries_for_subscriptions(
                event_type=event_type,
                subscribable_object=subscribable_object,
                webhooks=subscription_webhooks,
                requestor=requestor,
            )
        )

    for delivery in deliveries:
        send_webhook_request_async.delay(delivery.id)


def group_webhooks_by_subscription(webhooks):
    subscription = [webhook for webhook in webhooks if webhook.subscription_query]
    regular = [webhook for webhook in webhooks if not webhook.subscription_query]

    return regular, subscription


def trigger_webhook_sync(
    event_type: str,
    data: str,
    webhook: Optional["Webhook"],
    subscribable_object=None,
    timeout=None,
) -> Optional[Dict[Any, Any]]:
    """Send a synchronous webhook request."""
    if not webhook:
        raise PaymentError(f"No payment webhook found for event: {event_type}.")
    if webhook.subscription_query:
        delivery = create_delivery_for_subscription_sync_event(
            event_type=event_type,
            subscribable_object=subscribable_object,
            webhook=webhook,
        )
        if not delivery:
            return None
    else:
        event_payload = EventPayload.objects.create(payload=data)
        delivery = EventDelivery.objects.create(
            status=EventDeliveryStatus.PENDING,
            event_type=event_type,
            payload=event_payload,
            webhook=webhook,
        )
    kwargs = {}
    if timeout:
        kwargs = {"timeout": timeout}
    return send_webhook_request_sync(webhook.app.name, delivery, **kwargs)


R = TypeVar("R")


def trigger_all_webhooks_sync(
    event_type: str,
    generate_payload: Callable,
    parse_response: Callable[[Any], Optional[R]],
    subscribable_object=None,
    requestor=None,
) -> Optional[R]:
    """Send all synchronous webhook request for given event type.

    Requests are send sequentially.
    If the current webhook does not return expected response,
    the next one is send.
    If no webhook responds with expected response,
    this function returns None.
    """
    webhooks = get_webhooks_for_event(event_type)
    request_context = None
    event_payload = None
    for webhook in webhooks:
        if webhook.subscription_query:
            if request_context is None:
                request_context = initialize_request(
                    requestor, event_type in WebhookEventSyncType.ALL
                )
            delivery = create_delivery_for_subscription_sync_event(
                event_type=event_type,
                subscribable_object=subscribable_object,
                webhook=webhook,
                request=request_context,
                requestor=requestor,
            )
            if not delivery:
                return None
        else:
            if event_payload is None:
                event_payload = EventPayload.objects.create(payload=generate_payload())
            delivery = EventDelivery.objects.create(
                status=EventDeliveryStatus.PENDING,
                event_type=event_type,
                payload=event_payload,
                webhook=webhook,
            )

        response_data = send_webhook_request_sync(webhook.app.name, delivery)
        if parsed_response := parse_response(response_data):
            return parsed_response
    return None


def send_webhook_using_http(
    target_url, message, domain, signature, event_type, timeout=settings.WEBHOOK_TIMEOUT
) -> WebhookResponse:
    """Send a webhook request using http / https protocol.

    :param target_url: Target URL request will be sent to.
    :param message: Payload that will be used.
    :param domain: Current site domain.
    :param signature: Webhook secret key checksum.
    :param event_type: Webhook event type.
    :param timeout: Request timeout.

    :return: WebhookResponse object.
    """
    headers = {
        "Content-Type": "application/json",
        # X- headers will be deprecated in Saleor 4.0, proper headers are without X-
        DeprecatedAppHeaders.EVENT_TYPE: event_type,
        DeprecatedAppHeaders.DOMAIN: domain,
        DeprecatedAppHeaders.SIGNATURE: signature,
        AppHeaders.EVENT_TYPE: event_type,
        AppHeaders.DOMAIN: domain,
        AppHeaders.SIGNATURE: signature,
        AppHeaders.API_URL: build_absolute_uri(reverse("api"), domain),
    }
    try:
        response = requests.post(
            target_url, data=message, headers=headers, timeout=timeout
        )
    except RequestException as e:
        if e.response:
            result = WebhookResponse(
                content=e.response.text,
                status=EventDeliveryStatus.FAILED,
                request_headers=headers,
                response_headers=dict(e.response.headers),
                response_status_code=e.response.status_code,
            )
        else:
            result = WebhookResponse(
                content=str(e),
                status=EventDeliveryStatus.FAILED,
                request_headers=headers,
            )
        return result

    return WebhookResponse(
        content=response.text,
        request_headers=headers,
        response_headers=dict(response.headers),
        response_status_code=response.status_code,
        duration=response.elapsed.total_seconds(),
        status=(
            EventDeliveryStatus.SUCCESS if response.ok else EventDeliveryStatus.FAILED
        ),
    )


def send_webhook_using_aws_sqs(target_url, message, domain, signature, event_type):
    parts = urlparse(target_url)
    region = "us-east-1"
    hostname_parts = parts.hostname.split(".")
    if len(hostname_parts) == 4 and hostname_parts[0] == "sqs":
        region = hostname_parts[1]
    client = boto3.client(
        "sqs",
        region_name=region,
        aws_access_key_id=parts.username,
        aws_secret_access_key=(
            unquote(parts.password) if parts.password else parts.password
        ),
    )
    queue_url = urlunparse(
        (
            "https",
            parts.hostname,
            parts.path,
            parts.params,
            parts.query,
            parts.fragment,
        )
    )
    is_fifo = parts.path.endswith(".fifo")

    msg_attributes = {
        "SaleorDomain": {"DataType": "String", "StringValue": domain},
        "SaleorApiUrl": {
            "DataType": "String",
            "StringValue": build_absolute_uri(reverse("api"), domain),
        },
        "EventType": {"DataType": "String", "StringValue": event_type},
    }
    if signature:
        msg_attributes["Signature"] = {
            "DataType": "String",
            "StringValue": signature,
        }

    message_kwargs = {
        "QueueUrl": queue_url,
        "MessageAttributes": msg_attributes,
        "MessageBody": message.decode("utf-8"),
    }
    if is_fifo:
        message_kwargs["MessageGroupId"] = domain
    with catch_duration_time() as duration:
        try:
            response = client.send_message(**message_kwargs)
        except (ClientError,) as e:
            return WebhookResponse(
                content=str(e), status=EventDeliveryStatus.FAILED, duration=duration()
            )
        return WebhookResponse(content=response, duration=duration())


def send_webhook_using_google_cloud_pubsub(
    target_url, message, domain, signature, event_type
):
    parts = urlparse(target_url)
    client = pubsub_v1.PublisherClient()
    topic_name = parts.path[1:]  # drop the leading slash
    with catch_duration_time() as duration:
        try:
            future = client.publish(
                topic_name,
                message,
                saleorDomain=domain,
                saleorApiUrl=build_absolute_uri(reverse("api"), domain),
                eventType=event_type,
                signature=signature,
            )
        except (pubsub_v1.publisher.exceptions.MessageTooLargeError, RuntimeError) as e:
            return WebhookResponse(content=str(e), status=EventDeliveryStatus.FAILED)
        response_duration = duration()
        response = future.result()
        return WebhookResponse(content=response, duration=response_duration)


def send_webhook_using_scheme_method(
    target_url, domain, secret, event_type, data
) -> WebhookResponse:
    parts = urlparse(target_url)
    message = data.encode("utf-8")
    signature = signature_for_payload(message, secret)
    scheme_matrix: Dict[WebhookSchemes, Callable] = {
        WebhookSchemes.HTTP: send_webhook_using_http,
        WebhookSchemes.HTTPS: send_webhook_using_http,
        WebhookSchemes.AWS_SQS: send_webhook_using_aws_sqs,
        WebhookSchemes.GOOGLE_CLOUD_PUBSUB: send_webhook_using_google_cloud_pubsub,
    }
    if send_method := scheme_matrix.get(parts.scheme.lower()):
        # try:
        return send_method(
            target_url,
            message,
            domain,
            signature,
            event_type,
        )
    raise ValueError("Unknown webhook scheme: %r" % (parts.scheme,))


@app.task(
    queue=settings.WEBHOOK_CELERY_QUEUE_NAME,
    bind=True,
    retry_backoff=10,
    retry_kwargs={"max_retries": 5},
)
def send_webhook_request_async(self, event_delivery_id):
    try:
        delivery = EventDelivery.objects.select_related("payload", "webhook__app").get(
            id=event_delivery_id
        )
    except EventDelivery.DoesNotExist:
        logger.error("Event delivery id: %r not found", event_delivery_id)
        return

    if not delivery.webhook.is_active:
        delivery_update(delivery=delivery, status=EventDeliveryStatus.FAILED)
        logger.info("Event delivery id: %r webhook is disabled.", event_delivery_id)
        return

    webhook = delivery.webhook
    domain = Site.objects.get_current().domain
    attempt = create_attempt(delivery, self.request.id)
    delivery_status = EventDeliveryStatus.SUCCESS
    try:
        if not delivery.payload:
            raise ValueError(
                "Event delivery id: %r has no payload." % event_delivery_id
            )
        data = delivery.payload.payload
        with webhooks_opentracing_trace(
            delivery.event_type, domain, app_name=webhook.app.name
        ):
            response = send_webhook_using_scheme_method(
                webhook.target_url,
                domain,
                webhook.secret_key,
                delivery.event_type,
                data,
            )
        attempt_update(attempt, response)
        if response.status == EventDeliveryStatus.FAILED:
            task_logger.info(
                "[Webhook ID: %r] Failed request to %r: %r for event: %r."
                " Delivery attempt id: %r",
                webhook.id,
                webhook.target_url,
                response.content,
                delivery.event_type,
                attempt.id,
            )
            try:
                countdown = self.retry_backoff * (2**self.request.retries)
                self.retry(countdown=countdown, **self.retry_kwargs)
            except Retry as retry_error:
                next_retry = observability.task_next_retry_date(retry_error)
                observability.report_event_delivery_attempt(attempt, next_retry)
                raise retry_error
            except MaxRetriesExceededError:
                task_logger.warning(
                    "[Webhook ID: %r] Failed request to %r: exceeded retry limit."
                    "Delivery id: %r",
                    webhook.id,
                    webhook.target_url,
                    delivery.id,
                )
                delivery_status = EventDeliveryStatus.FAILED
        elif response.status == EventDeliveryStatus.SUCCESS:
            task_logger.info(
                "[Webhook ID:%r] Payload sent to %r for event %r. Delivery id: %r",
                webhook.id,
                webhook.target_url,
                delivery.event_type,
                delivery.id,
            )
        delivery_update(delivery, delivery_status)
    except ValueError as e:
        response = WebhookResponse(content=str(e), status=EventDeliveryStatus.FAILED)
        attempt_update(attempt, response)
        delivery_update(delivery=delivery, status=EventDeliveryStatus.FAILED)
    observability.report_event_delivery_attempt(attempt)
    clear_successful_delivery(delivery)


def send_webhook_request_sync(
    app_name, delivery, timeout=settings.WEBHOOK_SYNC_TIMEOUT
) -> Optional[Dict[Any, Any]]:
    event_payload = delivery.payload
    data = event_payload.payload
    webhook = delivery.webhook
    parts = urlparse(webhook.target_url)
    domain = Site.objects.get_current().domain
    message = data.encode("utf-8")
    signature = signature_for_payload(message, webhook.secret_key)

    if parts.scheme.lower() not in [WebhookSchemes.HTTP, WebhookSchemes.HTTPS]:
        delivery_update(delivery, EventDeliveryStatus.FAILED)
        raise ValueError("Unknown webhook scheme: %r" % (parts.scheme,))

    logger.debug(
        "[Webhook] Sending payload to %r for event %r.",
        webhook.target_url,
        delivery.event_type,
    )
    attempt = create_attempt(delivery=delivery, task_id=None)
    response = WebhookResponse(content="")
    response_data = None

    try:
        with webhooks_opentracing_trace(
            delivery.event_type, domain, sync=True, app_name=app_name
        ):
            response = send_webhook_using_http(
                webhook.target_url,
                message,
                domain,
                signature,
                delivery.event_type,
                timeout=timeout,
            )
            response_data = json.loads(response.content)

    except JSONDecodeError as e:
        logger.warning(
            "[Webhook] Failed parsing JSON response from %r: %r."
            "ID of failed DeliveryAttempt: %r . ",
            webhook.target_url,
            e,
            attempt.id,
        )
        response.status = EventDeliveryStatus.FAILED
    else:
        if response.status == EventDeliveryStatus.FAILED:
            logger.warning(
                "[Webhook] Failed request to %r: %r. "
                "ID of failed DeliveryAttempt: %r . ",
                webhook.target_url,
                response.content,
                attempt.id,
            )
        if response.status == EventDeliveryStatus.SUCCESS:
            logger.debug(
                "[Webhook] Success response from %r."
                "Successful DeliveryAttempt id: %r",
                webhook.target_url,
                attempt.id,
            )

    attempt_update(attempt, response)
    delivery_update(delivery, response.status)
    observability.report_event_delivery_attempt(attempt)
    clear_successful_delivery(delivery)

    return response_data if response.status == EventDeliveryStatus.SUCCESS else None


def send_observability_events(webhooks: List[WebhookData], events: List[Any]):
    event_type = WebhookEventAsyncType.OBSERVABILITY
    for webhook in webhooks:
        scheme = urlparse(webhook.target_url).scheme.lower()
        failed = 0
        extra = {
            "webhook_id": webhook.id,
            "webhook_target_url": webhook.target_url,
            "events_count": len(events),
        }
        try:
            if scheme in [WebhookSchemes.AWS_SQS, WebhookSchemes.GOOGLE_CLOUD_PUBSUB]:
                for event in events:
                    response = send_webhook_using_scheme_method(
                        webhook.target_url,
                        webhook.saleor_domain,
                        webhook.secret_key,
                        event_type,
                        observability.dump_payload(event),
                    )
                    if response.status == EventDeliveryStatus.FAILED:
                        failed += 1
            else:
                response = send_webhook_using_scheme_method(
                    webhook.target_url,
                    webhook.saleor_domain,
                    webhook.secret_key,
                    event_type,
                    observability.dump_payload(events),
                )
                if response.status == EventDeliveryStatus.FAILED:
                    failed = len(events)
        except ValueError:
            logger.error(
                "Webhook ID: %r unknown webhook scheme: %r.",
                webhook.id,
                scheme,
                extra={**extra, "dropped_events_count": len(events)},
            )
            continue
        if failed:
            logger.warning(
                "Webhook ID: %r failed request to %r (%s/%s events dropped): %r.",
                webhook.id,
                webhook.target_url,
                failed,
                len(events),
                response.content,
                extra={**extra, "dropped_events_count": failed},
            )
            continue
        logger.debug(
            "Successful delivered %s events to %r.",
            len(events),
            webhook.target_url,
            extra={**extra, "dropped_events_count": 0},
        )


@app.task
def observability_send_events():
    with observability.opentracing_trace("send_events_task", "task"):
        if webhooks := observability.get_webhooks():
            with observability.opentracing_trace("pop_events", "buffer"):
                events, _ = observability.pop_events_with_remaining_size()
            if events:
                with observability.opentracing_trace("send_events", "webhooks"):
                    send_observability_events(webhooks, events)


@app.task
def observability_reporter_task():
    with observability.opentracing_trace("reporter_task", "task"):
        if webhooks := observability.get_webhooks():
            with observability.opentracing_trace("pop_events", "buffer"):
                events, batch_count = observability.pop_events_with_remaining_size()
            if batch_count > 0:
                tasks = [observability_send_events.s() for _ in range(batch_count)]
                expiration = settings.OBSERVABILITY_REPORT_PERIOD.total_seconds()
                group(tasks).apply_async(expires=expiration)
            if events:
                with observability.opentracing_trace("send_events", "webhooks"):
                    send_observability_events(webhooks, events)
