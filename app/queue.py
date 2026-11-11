"""Hand conversations to the worker: Cloud Tasks in production, BackgroundTasks locally."""

import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks
from google.cloud import tasks_v2
from google.protobuf import timestamp_pb2

from app import worker
from app.config import get_settings

logger = logging.getLogger(__name__)

TASK_PATH = "/tasks/process-message"
COMMENT_TASK_PATH = "/tasks/process-comment"
OWNER_TASK_PATH = "/tasks/owner-message"          # one owner message -> mybill dropship intake
DROPSHIP_CLOSE_PATH = "/tasks/dropship-close"     # delayed: close forwarded posts that went quiet
DROPSHIP_EXTRACT_PATH = "/tasks/dropship-extract"  # build the draft collection (AI) once the supplier is chosen
DROPSHIP_PUBLISH_PATH = "/tasks/dropship-publish"  # post an approved collection on Instagram + Meta catalog

_tasks_client: tasks_v2.CloudTasksAsyncClient | None = None


def _get_tasks_client() -> tasks_v2.CloudTasksAsyncClient:
    """Lazily create the Cloud Tasks client (uses Cloud Run's service account)."""
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksAsyncClient()
    return _tasks_client


def build_task(conversation_id: uuid.UUID, delay_seconds: int = 0) -> tasks_v2.Task:
    """An HTTP task that POSTs {"conversation_id": ...} to our worker with an OIDC token.

    Tasks are deliberately unnamed: Cloud Tasks reserves a used task name for ~1 hour, which
    would silently drop the task for a customer's follow-up message. Duplicate tasks for one
    conversation are harmless - the worker only answers messages still pending.
    """
    return build_http_task(TASK_PATH, {"conversation_id": str(conversation_id)}, delay_seconds)


def build_http_task(path: str, body: dict, delay_seconds: int = 0) -> tasks_v2.Task:
    settings = get_settings()
    base = settings.tasks_target_url.rstrip("/")
    schedule_time = None
    if delay_seconds > 0:
        schedule_time = timestamp_pb2.Timestamp()
        schedule_time.FromDatetime(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
    return tasks_v2.Task(
        schedule_time=schedule_time,
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{base}{path}",
            headers={"Content-Type": "application/json"},
            body=json.dumps(body).encode(),
            oidc_token=tasks_v2.OidcToken(service_account_email=settings.tasks_service_account, audience=base),
        )
    )


async def _enqueue_cloud_task(conversation_id: uuid.UUID, delay_seconds: int = 0) -> None:
    """Create one Cloud Task for this conversation."""
    settings = get_settings()
    client = _get_tasks_client()
    parent = client.queue_path(settings.gcp_project, settings.tasks_location, settings.tasks_queue)
    await client.create_task(parent=parent, task=build_task(conversation_id, delay_seconds))


async def _process_later(conversation_id: uuid.UUID, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    await worker.process_conversation(conversation_id)


async def enqueue(conversation_ids: Iterable[uuid.UUID], background: BackgroundTasks, delay_seconds: int = 0) -> None:
    """Queue processing for each conversation (optionally delayed, e.g. Instagram echoes). Never raises: failures
    are only logged, because the messages are already saved and the next message (or a retry) picks them up."""
    use_cloud_tasks = get_settings().use_cloud_tasks
    for conversation_id in conversation_ids:
        if not use_cloud_tasks:
            if delay_seconds > 0:
                background.add_task(_process_later, conversation_id, delay_seconds)
            else:
                background.add_task(worker.process_conversation, conversation_id)
            continue
        try:
            await _enqueue_cloud_task(conversation_id, delay_seconds)
        except Exception:
            logger.exception("Failed to enqueue task", extra={"fields": {"conversation_id": str(conversation_id)}})


async def _create_http_task(path: str, body: dict, delay_seconds: int = 0) -> None:
    settings = get_settings()
    client = _get_tasks_client()
    parent = client.queue_path(settings.gcp_project, settings.tasks_location, settings.tasks_queue)
    await client.create_task(parent=parent, task=build_http_task(path, body, delay_seconds))


async def _owner_locally(payloads: list[dict], delay_seconds: int) -> None:
    from app import dropship_intake
    for p in payloads:
        await dropship_intake.handle(p)
    await asyncio.sleep(delay_seconds)
    await dropship_intake.close_quiet_batches()


async def enqueue_owner(payloads: list[dict], background: BackgroundTasks) -> None:
    """The owner's forwarded post: one task per message (in order), then a delayed task that closes the post once it
    has been quiet for dropship_idle_seconds - no polling job, so it works with min-instances=0. Never raises."""
    if not payloads:
        return
    settings = get_settings()
    delay = settings.dropship_idle_seconds + 5
    if not settings.use_cloud_tasks:
        background.add_task(_owner_locally, payloads, delay)
        return
    for p in payloads:
        try:
            await _create_http_task(OWNER_TASK_PATH, p)
        except Exception:
            logger.exception("Failed to enqueue owner message", extra={"fields": {"wa_message_id": p.get("wa_message_id")}})
    await enqueue_dropship_check(delay)


async def enqueue_dropship_extract(batch_id: int, background: BackgroundTasks | None = None) -> None:
    """Run the AI extraction in its own task (it takes a while; a retry is safe - mybill builds a post once)."""
    if not get_settings().use_cloud_tasks:
        from app import dropship_intake
        if background is not None:
            background.add_task(dropship_intake.extract, batch_id)
        return
    try:
        await _create_http_task(DROPSHIP_EXTRACT_PATH, {"batch_id": batch_id})
    except Exception:
        logger.exception("Failed to enqueue dropship extraction", extra={"fields": {"batch_id": batch_id}})


async def enqueue_dropship_publish(collection_id: int) -> None:
    """Instagram posting takes up to a couple of minutes: its own task (a repeat doesn't post twice)."""
    if not get_settings().use_cloud_tasks:
        from app import dropship_intake
        asyncio.create_task(dropship_intake.publish(collection_id))
        return
    try:
        await _create_http_task(DROPSHIP_PUBLISH_PATH, {"collection_id": collection_id})
    except Exception:
        logger.exception("Failed to enqueue dropship publishing", extra={"fields": {"collection_id": collection_id}})


REMINDER_DELAY = 2 * 3600 + 60   # mybill reminds the owner 2 h after asking "Which supplier?"


async def enqueue_dropship_check(delay_seconds: int) -> None:
    """A delayed call to mybill's process-batches (closes quiet posts, sends due supplier reminders). Never raises."""
    if not get_settings().use_cloud_tasks:
        return   # local runs: _owner_locally closes the batch itself
    try:
        await _create_http_task(DROPSHIP_CLOSE_PATH, {}, delay_seconds)
    except Exception:
        logger.exception("Failed to enqueue dropship check")


async def enqueue_comments(comment_ids: Iterable[str], background: BackgroundTasks) -> None:
    """Queue the auto-reply for new Instagram comments. Never raises."""
    from app import ig_comments
    settings = get_settings()
    for comment_id in comment_ids:
        if not settings.use_cloud_tasks:
            background.add_task(ig_comments.process_comment, comment_id)
            continue
        try:
            client = _get_tasks_client()
            parent = client.queue_path(settings.gcp_project, settings.tasks_location, settings.tasks_queue)
            await client.create_task(parent=parent, task=build_http_task(COMMENT_TASK_PATH, {"comment_id": comment_id}))
        except Exception:
            logger.exception("Failed to enqueue comment task", extra={"fields": {"comment_id": comment_id}})
