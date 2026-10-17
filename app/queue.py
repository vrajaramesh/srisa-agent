"""Hand conversations to the worker: Cloud Tasks in production, BackgroundTasks locally."""

import json
import logging
import uuid
from collections.abc import Iterable

from fastapi import BackgroundTasks
from google.cloud import tasks_v2

from app import worker
from app.config import get_settings

logger = logging.getLogger(__name__)

TASK_PATH = "/tasks/process-message"

_tasks_client: tasks_v2.CloudTasksAsyncClient | None = None


def _get_tasks_client() -> tasks_v2.CloudTasksAsyncClient:
    """Lazily create the Cloud Tasks client (uses Cloud Run's service account)."""
    global _tasks_client
    if _tasks_client is None:
        _tasks_client = tasks_v2.CloudTasksAsyncClient()
    return _tasks_client


def build_task(conversation_id: uuid.UUID) -> tasks_v2.Task:
    """An HTTP task that POSTs {"conversation_id": ...} to our worker with an OIDC token.

    Tasks are deliberately unnamed: Cloud Tasks reserves a used task name for ~1 hour, which
    would silently drop the task for a customer's follow-up message. Duplicate tasks for one
    conversation are harmless - the worker only answers messages still pending.
    """
    settings = get_settings()
    base = settings.tasks_target_url.rstrip("/")
    return tasks_v2.Task(
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=f"{base}{TASK_PATH}",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"conversation_id": str(conversation_id)}).encode(),
            oidc_token=tasks_v2.OidcToken(service_account_email=settings.tasks_service_account, audience=base),
        )
    )


async def _enqueue_cloud_task(conversation_id: uuid.UUID) -> None:
    """Create one Cloud Task for this conversation."""
    settings = get_settings()
    client = _get_tasks_client()
    parent = client.queue_path(settings.gcp_project, settings.tasks_location, settings.tasks_queue)
    await client.create_task(parent=parent, task=build_task(conversation_id))


async def enqueue(conversation_ids: Iterable[uuid.UUID], background: BackgroundTasks) -> None:
    """Queue processing for each conversation. Never raises: failures are only logged,
    because the messages are already saved and the next message (or a retry) picks them up."""
    use_cloud_tasks = get_settings().use_cloud_tasks
    for conversation_id in conversation_ids:
        if not use_cloud_tasks:
            background.add_task(worker.process_conversation, conversation_id)
            continue
        try:
            await _enqueue_cloud_task(conversation_id)
        except Exception:
            logger.exception("Failed to enqueue task", extra={"fields": {"conversation_id": str(conversation_id)}})
