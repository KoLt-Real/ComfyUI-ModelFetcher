"""Sequential model downloads + progress on the ComfyUI websocket.

A queue plus a single worker thread (started lazily). Each job writes to a ``.part`` file then
``os.replace`` at the end. Progress is pushed through ``send_sync`` (throttled). Cancellation
uses one ``threading.Event`` per job, tested on every chunk.

**Resume**: the ``.part`` survives a cancellation, a dropped connection or a truncated
transfer, and the next attempt picks up where it stopped (``Range`` header). On a multi-GB
checkpoint that is the difference between resuming and starting over. The ``.part`` name
carries a fingerprint of the URL: two different sources sharing a filename can never end up
mixed inside the same temporary file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time

import requests

from . import hf_token, scanner, urlpolicy

logger = logging.getLogger("comfyfactory.modelfetcher")

CHUNK = 1024 * 1024  # 1 MiB
PROGRESS_MIN_INTERVAL = 0.25  # s → ~4 events/s max per job
WORKER_IDLE_TIMEOUT = 30  # s without a job before the worker steps down


def _push(event: str, payload: dict) -> None:
    try:
        from server import PromptServer
        PromptServer.instance.send_sync(event, payload)
    except Exception:
        logger.exception("cf_mf: send_sync failed for %s", event)


class _Job:
    __slots__ = ("id", "url", "dest", "overwrite", "cancel")

    def __init__(self, jid, url, dest, overwrite):
        self.id = jid
        self.url = url
        self.dest = dest
        self.overwrite = overwrite
        self.cancel = threading.Event()


class DownloadManager:
    def __init__(self):
        self._q: "queue.Queue[_Job]" = queue.Queue()
        self._jobs: dict[str, _Job] = {}
        self._active: str | None = None
        # Running instance: ``_jobs[id]`` may already point at a re-enqueue of the same id,
        # yet THIS one is what a cancellation must reach.
        self._active_job: _Job | None = None
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    # ---- public API ---------------------------------------------------
    def enqueue(self, jid: str, url: str, dest: str, overwrite: bool = False) -> None:
        job = _Job(jid, url, dest, overwrite)
        with self._lock:
            # Re-enqueue of the same id: the old pending job is replaced (the worker will
            # skip it silently); an active job, on the other hand, runs to completion.
            old = self._jobs.get(jid)
            if old is not None and jid != self._active:
                old.cancel.set()
            self._jobs[jid] = job
        self._q.put(job)
        self._ensure_worker()

    def cancel(self, jid: str) -> bool:
        """Cancel a job. Pending job → removed + immediate "cancelled" event;
        active job → the event is tested on every chunk and the loop pushes the event.

        BOTH instances of an id are targeted: a re-enqueue during an active download replaces
        ``_jobs[id]``, and cancelling only that one would let the running transfer race to the
        end, uncancellable.
        """
        with self._lock:
            queued = self._jobs.get(jid)
            active = self._active_job if self._active == jid else None
            if queued is None and active is None:
                return False
            if queued is not None:
                queued.cancel.set()
                if queued is not active:
                    self._jobs.pop(jid, None)
            if active is not None:
                active.cancel.set()
        # If an active job carries this id, its own loop pushes the event on the next chunk:
        # do not duplicate it here.
        if active is None:
            _push("cf_mf.error", {"id": jid, "code": "cancelled",
                                  "message": "Download cancelled."})
        return True

    def cancel_all(self) -> None:
        with self._lock:
            pending = []
            for jid, j in list(self._jobs.items()):
                j.cancel.set()
                if jid != self._active:
                    self._jobs.pop(jid, None)
                    pending.append(jid)
            # Same thing: the active instance may no longer be the one in the mapping.
            if self._active_job is not None:
                self._active_job.cancel.set()
        for jid in pending:
            _push("cf_mf.error", {"id": jid, "code": "cancelled",
                                  "message": "Download cancelled."})

    def status(self) -> dict:
        with self._lock:
            pending = [jid for jid, j in self._jobs.items()
                       if jid != self._active and not j.cancel.is_set()]
            return {"active": self._active, "pending": pending}

    # ---- internals ----------------------------------------------------
    def _ensure_worker(self) -> None:
        # ``_worker is None`` (not ``is_alive()``) is the only reliable test: a worker that is
        # timing out is still alive but will not pick anything up. It de-registers itself, while
        # holding the lock, once it is sure the queue is empty.
        with self._lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._run, name="cf_mf-downloader", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            # Safety net: an unexpected exception must not leave ``_worker`` pointing at a
            # dead thread — no download would ever start again until a restart.
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None

    def _loop(self) -> None:
        while True:
            try:
                job = self._q.get(timeout=WORKER_IDLE_TIMEOUT)
            except queue.Empty:
                with self._lock:
                    # Step down while holding the lock, and only if nothing arrived meanwhile:
                    # otherwise a concurrent enqueue would see an "alive" worker that is
                    # stopping, and its job would sit in the queue forever.
                    if self._q.empty():
                        self._worker = None
                        return
                continue
            with self._lock:
                # Cancelled while queued: cancel() already removed the job and pushed the
                # "cancelled" event → skip silently.
                if job.cancel.is_set():
                    if self._jobs.get(job.id) is job:
                        self._jobs.pop(job.id, None)
                    skip = True
                else:
                    self._active = job.id
                    self._active_job = job
                    skip = False
            if skip:
                self._q.task_done()
                continue
            try:
                self._download(job)
            except Exception as e:
                logger.exception("cf_mf: download error on %s", job.id)
                _push("cf_mf.error", {"id": job.id, "code": "network", "message": str(e)})
            finally:
                with self._lock:
                    if self._active_job is job:
                        self._active = None
                        self._active_job = None
                    # Only remove ITS OWN instance: a Retry may have re-registered the same
                    # id with a new job still sitting in the queue.
                    if self._jobs.get(job.id) is job:
                        self._jobs.pop(job.id, None)
                self._q.task_done()

    def _download(self, job: _Job) -> None:
        dest = job.dest
        # The worker trusts nothing it is handed. ``routes.download`` already confines the
        # destination and checks the host, but this is the code that writes to the disk and
        # talks to the network: it re-checks both, so a caller reaching the manager some other
        # way gains nothing (AGENTS.md invariants 2, 14 and 15).
        if not scanner.is_allowed_dest(dest):
            _push("cf_mf.error", {"id": job.id, "code": "dest_refused",
                                  "message": "Destination is outside the model folders."})
            return
        reason = urlpolicy.check_url(job.url)
        if reason:
            _push("cf_mf.error", {"id": job.id, "code": "host_not_allowed", "message": reason})
            return
        part = _part_path(dest, job.url)
        if os.path.exists(dest) and not job.overwrite:
            _push("cf_mf.done", {"id": job.id, "path": dest, "size": _safe_size(dest),
                                 "note": "already_exists"})
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        # Bytes already there from a previous attempt: ask for the rest.
        resume_from = max(0, _safe_size(part)) if os.path.exists(part) else 0
        headers = dict(hf_token.auth_headers(job.url))
        if resume_from:
            headers["Range"] = "bytes=%d-" % resume_from

        try:
            # Redirects are followed by ``open_url`` alone: every hop is checked against the
            # host allow-list, and the token never travels past the host it was issued for.
            r = urlpolicy.open_url("GET", job.url, stream=True, timeout=(10, 120),
                                   headers=headers)
            if r.status_code == 416 and resume_from:
                # The ``.part`` is larger than the resource: it no longer matches what we are
                # downloading (file replaced upstream, or already complete). Start over.
                r.close()
                _cleanup(part)
                resume_from = 0
                headers.pop("Range", None)
                r = urlpolicy.open_url("GET", job.url, stream=True, timeout=(10, 120),
                                       headers=headers)
            with r:
                if r.status_code in (401, 403):
                    _push("cf_mf.error", {"id": job.id, "code": "http_%d" % r.status_code,
                                          "message": "Gated model — add your HuggingFace token (see the popup)."})
                    return
                if r.status_code == 404:
                    _push("cf_mf.error", {"id": job.id, "code": "http_404",
                                          "message": "File not found (404)."})
                    return
                r.raise_for_status()

                # 206 = the resume was accepted. Any other 2xx means the server ignores
                # ``Range`` and returns the whole file: rewrite from the start.
                resuming = (r.status_code == 206 and resume_from > 0)
                if not resuming:
                    resume_from = 0

                remaining = int(r.headers.get("Content-Length") or 0)
                total = (resume_from + remaining) if remaining else 0
                downloaded = resume_from
                last_emit = 0.0
                last_bytes = downloaded  # speed only counts the bytes of THIS session
                last_t = time.monotonic()
                if resuming:
                    logger.info("cf_mf: resuming %s at %d bytes", os.path.basename(dest),
                                resume_from)

                with open(part, "ab" if resuming else "wb") as f:
                    for chunk in r.iter_content(chunk_size=CHUNK):
                        if job.cancel.is_set():
                            # The .part stays: restarting this model resumes right here.
                            _push("cf_mf.error", {"id": job.id, "code": "cancelled",
                                                  "message": "Download cancelled."})
                            return
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_emit >= PROGRESS_MIN_INTERVAL:
                            dt = now - last_t
                            speed = int((downloaded - last_bytes) / dt) if dt > 0 else 0
                            _push("cf_mf.progress", {"id": job.id, "downloaded": downloaded,
                                                     "total": total, "speed_bps": speed})
                            last_emit, last_bytes, last_t = now, downloaded, now

            if total and downloaded < total:
                # Connection dropped mid-transfer: the received bytes serve the resume.
                _push("cf_mf.error", {"id": job.id, "code": "incomplete",
                                      "message": "Incomplete download (%d/%d) — retry to resume."
                                                 % (downloaded, total)})
                return
            os.replace(part, dest)
            _push("cf_mf.done", {"id": job.id, "path": dest, "size": _safe_size(dest)})

        # ``requests.RequestException`` derives from ``IOError`` (= ``OSError``): its clauses
        # must come first, or the disk branch below swallows every network error as "io".
        except urlpolicy.HostNotAllowed as e:
            # A redirect left the allow-list: nothing was fetched from there, and a Retry
            # would only meet the same redirect.
            _push("cf_mf.error", {"id": job.id, "code": "host_not_allowed", "message": str(e)})
        except requests.RequestException as e:
            # Network: the .part is kept, a Retry will resume where we stopped.
            _push("cf_mf.error", {"id": job.id, "code": "network", "message": str(e)})
        except OSError as e:
            if getattr(e, "errno", None) == 28:  # ENOSPC
                # The only case where the .part is discarded: keeping it would make a full
                # disk worse.
                _cleanup(part)
                _push("cf_mf.error", {"id": job.id, "code": "disk_full", "message": "Disk full."})
            else:
                _push("cf_mf.error", {"id": job.id, "code": "io", "message": str(e)})


def _part_path(dest: str, url: str) -> str:
    """Temporary file specific to the (destination, source) pair.

    The URL fingerprint in the name prevents resuming a download onto bytes that came from
    ANOTHER source sharing the same filename — which would silently produce a corrupt model.
    """
    return "%s.%s.part" % (dest, hashlib.sha1(url.encode("utf-8")).hexdigest()[:8])


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


# Singleton
manager = DownloadManager()
