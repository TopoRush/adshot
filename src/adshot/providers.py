"""HTTP clients for Ark-compatible video tasks and chat completions."""

import http.client
import json
import os
import shutil
import time
from pathlib import Path
from urllib import error, parse, request

from .config import validate_base_url
from .storyboard import Shot


class ProviderError(RuntimeError):
    """A provider request failed. The exception omits keys and signed URLs."""


class RejectedSubmission(ProviderError):
    """The provider refused the key or the account before creating a task."""


class _TransientProviderError(ProviderError):
    """A transport or server failure that permits retrying a read-only poll."""


def api_key(*names: str) -> str:
    for name in names:
        key = os.environ.get(name, "").strip()
        if key:
            if not key.isascii() or not key.isprintable() or " " in key:
                raise ValueError(f"Invalid characters in {name}.")
            return key
    raise ValueError("Set " + " or ".join(names) + " in your environment.")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An authenticated POST can create a billable job.
        return None


class _MediaRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_http_url(url: str) -> None:
    if not isinstance(url, str):
        raise ValueError("Expected an HTTP(S) URL.")
    parts = parse.urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None:
        raise ValueError("Expected an HTTP(S) URL without embedded credentials.")


def json_request(url: str, key: str, payload: dict | None = None) -> dict:
    validate_base_url(url)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=body, headers={
        "Accept": "application/json", "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=120) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        exc.close()
        failure = _TransientProviderError if 500 <= exc.code < 600 else ProviderError
        refusal = failure(f"Provider returned HTTP {exc.code}.")
        refusal.status = exc.code
        raise refusal from None
    except (OSError, http.client.HTTPException):
        raise _TransientProviderError("Provider connection failed.") from None
    except ValueError:
        raise ProviderError("Provider returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise ProviderError("Expected a JSON object in the provider response.")
    return result


REFUSALS = {
    401: "Check that ADSHOT_API_KEY is valid for this endpoint.",
    402: "Check the account balance at your provider.",
    403: "Check the account balance and that the key may use this model.",
}


class VideoProvider:
    def __init__(self, base_url: str):
        self.endpoint = validate_base_url(base_url) + "/api/v3/contents/generations/tasks"

    def check_credentials(self) -> None:
        api_key("ADSHOT_API_KEY")

    def submit(self, shot: Shot, image_url: str | None) -> str:
        content = [{"type": "text", "text": shot.prompt}]
        if image_url:
            content.append({
                "type": "image_url", "image_url": {"url": image_url}, "role": "first_frame",
            })
        try:
            result = json_request(self.endpoint, api_key("ADSHOT_API_KEY"), {
                "model": shot.model, "content": content, "resolution": shot.resolution,
                "ratio": shot.ratio, "duration": shot.seconds,
                "generate_audio": False, "watermark": False,
            })
        except ProviderError as exc:
            # 401, 402 and 403 refuse the key or the account. Providers and
            # prepaid gateways check both before creating a task. Other failures
            # leave the outcome unknown.
            status = getattr(exc, "status", None)
            if status in REFUSALS:
                raise RejectedSubmission(
                    f"Provider refused the request with HTTP {status} before creating a task. "
                    f"{REFUSALS[status]} If your provider's task history does show this shot, "
                    "do not run it again.") from None
            raise
        for field in ("id", "task_id"):
            task_id = result.get(field)
            if isinstance(task_id, str) and task_id.strip():
                return task_id.strip()
        raise ProviderError("Submission returned no task ID. Check your provider's task history.")

    def poll(self, task_id: str) -> dict:
        suffix = parse.quote(task_id, safe="")
        url = f"{self.endpoint}/{suffix}"
        key = api_key("ADSHOT_API_KEY")
        for delay in (1, 3, 9):
            try:
                return json_request(url, key)
            except _TransientProviderError:
                time.sleep(delay)
        return json_request(url, key)

    def download(self, url: str, dest: Path) -> None:
        """Stream into a caller-owned temporary file. Publish only after success."""
        validate_http_url(url)
        req = request.Request(url)
        try:
            with request.build_opener(_MediaRedirect()).open(req, timeout=120) as response:
                expected = response.headers.get("Content-Length")
                if getattr(response, "status", 200) != 200:
                    raise ProviderError("Video download returned an incomplete response.")
                with dest.open("wb") as output:
                    shutil.copyfileobj(response, output, 1024 * 1024)
                    size = output.tell()
                    if size == 0 or (expected is not None and size != int(expected)):
                        raise ProviderError("Video download was incomplete.")
                    output.flush()
                    os.fsync(output.fileno())
        except error.HTTPError as exc:
            exc.close()
            raise ProviderError(f"Video download returned HTTP {exc.code}.") from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ProviderError("Video download or local file write failed.") from None
