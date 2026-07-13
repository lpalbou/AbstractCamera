"""Capture-event servicing and file custody: announce bookkeeping, deferred
downloads (Auto-Fire), fetch-on-announce families (Sony slot eviction), and
budget-bounded drains where the deadline always wins."""

from __future__ import annotations

import time

from abstractcamera import wire


class DownloadsMixin:

    # Deferred downloads: force one download once the oldest queued file is
    # this old, even while armed (card-pressure/feedback safety valve).
    DEFERRED_DOWNLOAD_MAX_AGE_S = 120.0

    def _handle_non_file_event(self, event_type, event_data) -> None:
        # Card-full and other camera-side errors used to be silently
        # discarded here (F10) — surface anything that carries text. The
        # adapter classifies: Sony streams property-change noise at several
        # events per second that must never reach the catch log.
        adapter = self._adapter
        if adapter is None:
            return
        classified = adapter.classify_event(event_type, event_data)
        if classified.kind == "status" and classified.note:
            self._append_event(kind="camera-event", reason="camera", note=classified.note)

    def _announce_file_added(self, event_data, camera=None) -> str:
        """FILE_ADDED bookkeeping. Burst accounting lives HERE (adversarial
        verdict): decrementing on download completion left
        _preview_pause_until stuck for the whole deferral window.

        Returns 'downloaded' | 'deferred' | 'duplicate'. Families flagged
        fetch_on_announce (Sony: ONE sdram slot — the next capture EVICTS an
        unfetched object, hardware-observed as [-1] on burst files 2..N-1)
        download immediately; the driver pre-spools the object during event
        pumping so the fetch itself is ~10ms. Everyone else queues for the
        deferred-download policy exactly as before."""
        key = (event_data.folder, event_data.name)
        with self._state_lock:
            download_locally = self._download_locally
        fetch_now = (
            camera is not None
            and self._adapter is not None
            and self._adapter.fetch_on_announce
        )
        outcome = "duplicate"
        with self._state_lock:
            # A file arrived: the silent-refusal watch (if any) is satisfied.
            self._expect_file_deadline = 0.0
            self._expect_file_note = None
            if key not in self._pending_download_keys:
                if not download_locally:
                    # Save policy: DEVICE ONLY — announce honestly, never
                    # fetch (the file lives on the camera's storage).
                    outcome = "device-only"
                elif fetch_now:
                    outcome = "downloaded"
                else:
                    outcome = "deferred"
                    self._pending_download_keys.add(key)
                    self._pending_downloads.append((event_data.folder, event_data.name, time.time()))
                    self._downloads_pending = len(self._pending_downloads)
        if outcome == "device-only":
            self._append_event(
                kind="photo-pending", reason="captured",
                note=f"{event_data.name} — saved on the camera (local download is off)",
                thumbnail_jpeg=self._latest_frame,
            )
        if outcome == "downloaded":
            self._download_one_pending(camera, event_data.folder, event_data.name)
        # Announce-driven window extension: while files are still flowing
        # (burst tails trail for ~10s on the A7R IV), keep the drain window
        # open; 5s of announce silence lets it close. max() — never shorten.
        self._event_drain_until = max(self._event_drain_until, time.time() + 5.0)
        # Resume live view early once the whole burst has been announced.
        pending = getattr(self, "_burst_photos_pending", 0)
        if pending > 0:
            self._burst_photos_pending = pending - 1
            if self._burst_photos_pending <= 0:
                self._preview_pause_until = 0.0
        return outcome

    def _poll_capture_events(self, camera, time_budget_s: float = 0.03) -> None:
        """Armed-mode event servicing: announce FILE_ADDED (queue + catch-log
        row), NEVER file_get. Budget is tiny by design — the whole point is
        that detection keeps running between shots."""
        deadline = time.time() + max(0.005, float(time_budget_s))
        while time.time() < deadline and not self._stop_requested.is_set():
            try:
                event_type, event_data = camera.wait_for_event(5)
            except Exception:
                return
            if event_type == wire.GP_EVENT_TIMEOUT:
                return
            if event_type == wire.GP_EVENT_FILE_ADDED and event_data is not None:
                outcome = self._announce_file_added(event_data, camera)
                if outcome == "deferred":
                    # Instant honest feedback: the shot exists ON THE CAMERA;
                    # the thumbnail is the latest live-view frame (~0ms).
                    # fetch_on_announce families logged the real photo event
                    # inside the announce instead.
                    self._append_event(
                        kind="photo-pending",
                        reason="captured",
                        note=f"{event_data.name} — on camera, downloads when Auto-Fire disarms",
                        thumbnail_jpeg=self._latest_frame,
                    )
            else:
                self._handle_non_file_event(event_type, event_data)

    def _download_one_pending(self, camera, folder: str, name: str) -> None:
        """One file_get + save + catch-log photo event (extracted from the
        old inline drain body)."""
        import os

        saved_path = None
        try:
            try:
                cam_file = camera.file_get(folder, name, wire.GP_FILE_TYPE_NORMAL)
            except Exception:
                # One bounded retry: transient [-1] fetch failures happen
                # while the body is still flushing a burst to the card
                # (hardware-observed on the A7R IV, 2026-07-12). The file is
                # not gone — the camera was just busy.
                time.sleep(0.4)
                cam_file = camera.file_get(folder, name, wire.GP_FILE_TYPE_NORMAL)
            if self._capture_dir:
                os.makedirs(self._capture_dir, exist_ok=True)
                stamp = time.strftime("%Y%m%d_%H%M%S")
                base, ext = os.path.splitext(name)
                saved_path = os.path.join(self._capture_dir, f"capture_{stamp}_{base}{ext or '.jpg'}")
                cam_file.save(saved_path)
        except Exception as exc:
            self._append_event(kind="error", reason="download", note=f"failed to fetch {name}: {exc}")
            return
        thumbnail = None
        if saved_path and saved_path.lower().endswith((".jpg", ".jpeg")):
            try:
                with open(saved_path, "rb") as fh:
                    thumbnail = fh.read()
            except OSError:
                thumbnail = None
        elif saved_path:
            # RAW captures (NEF/ARW/...) carry a full-res embedded JPEG:
            # extract it so the catch log shows the shot instead of
            # a blank row.
            try:
                from abstractcamera.raw_thumbs import extract_raw_thumbnail_jpeg, is_raw_extension
                if is_raw_extension(os.path.splitext(saved_path)[1]):
                    with open(saved_path, "rb") as fh:
                        thumbnail = extract_raw_thumbnail_jpeg(fh.read())
            except Exception:
                thumbnail = None
        self._append_event(
            kind="photo",
            reason="captured",
            note=os.path.basename(saved_path) if saved_path else name,
            path=saved_path,
            thumbnail_jpeg=thumbnail,
        )

    def _flush_pending_downloads(self, camera, time_budget_s: float = 10.0,
                                 ignore_stop: bool = False) -> None:
        """Download queued files within a budget; the deadline wins mid-queue
        (a 25MB NEF costs 1-3s on real USB). `ignore_stop` is for the final
        worker flush, which runs AFTER the stop flag is already set."""
        deadline = time.time() + max(0.05, float(time_budget_s))
        while ignore_stop or not self._stop_requested.is_set():
            with self._state_lock:
                if not self._pending_downloads:
                    return
                folder, name, _announced_at = self._pending_downloads[0]
            if time.time() >= deadline:
                return
            self._download_one_pending(camera, folder, name)
            with self._state_lock:
                # Remove regardless of outcome: a failed fetch must not wedge
                # the queue head forever (the file stays on the card).
                if self._pending_downloads and self._pending_downloads[0][:2] == (folder, name):
                    self._pending_downloads.popleft()
                self._pending_download_keys.discard((folder, name))
                self._downloads_pending = len(self._pending_downloads)

    def _oldest_pending_download_age(self) -> float:
        with self._state_lock:
            if not self._pending_downloads:
                return 0.0
            return time.time() - self._pending_downloads[0][2]

    def _drain_capture_events(self, camera, time_budget_s: float = 10.0) -> None:
        """Collect capture results without blocking the live view loop.
        `time_budget_s` bounds the WHOLE drain (checked between events) so a
        slow multi-file download cannot blow a sequence deadline. Downloads
        are announced first (burst bookkeeping) then fetched inline."""
        drain_deadline = time.time() + max(0.05, float(time_budget_s))
        while True:
            # Stop-flag check: disconnect()'s 10s join must not strand a
            # zombie worker mid-download loop (F8).
            if self._stop_requested.is_set():
                return
            if time.time() >= drain_deadline:
                return  # deadline wins; remaining files drain next window
            try:
                event_type, event_data = camera.wait_for_event(10)
            except Exception:
                break
            if event_type == wire.GP_EVENT_TIMEOUT:
                break
            if event_type != wire.GP_EVENT_FILE_ADDED or event_data is None:
                self._handle_non_file_event(event_type, event_data)
                continue
            self._announce_file_added(event_data, camera)
        # Fetch everything announced (and anything deferred earlier) within
        # the remaining budget.
        remaining = drain_deadline - time.time()
        if remaining > 0.05:
            self._flush_pending_downloads(camera, time_budget_s=remaining)
