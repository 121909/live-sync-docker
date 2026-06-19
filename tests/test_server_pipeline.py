import os
import tempfile
import time
import unittest
from pathlib import Path
import threading
import urllib.error
from unittest.mock import patch

from app.stream_manager import build_ffmpeg_hls_cmd, selected_audio_channels


_RUNTIME = tempfile.TemporaryDirectory()
os.environ["LIVE_SYNC_RUNTIME_DIR"] = _RUNTIME.name
os.environ.pop("LIVE_SYNC_STATE_DIR", None)
os.environ.pop("STATE_DIR", None)
os.environ.pop("LIVE_SYNC_HLS_DIR", None)
os.environ.pop("HLS_DIR", None)
os.environ.pop("WORK_DIR", None)

from app import server  # noqa: E402


class FakeProcess:
    returncode = None

    def poll(self):
        return None


class PreparedPipelineTest(unittest.TestCase):
    def test_negative_offset_snapshot_uses_delayed_audio_input(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile.update({
            "offset_seconds": -6.0,
            "timeout_seconds": 5,
            "segment_time": 4,
        })
        video = server.Channel(name="video", url="http://example.test/video.m3u8")
        audio = server.Channel(name="audio", url="http://example.test/audio.m3u8")

        with (
            patch.object(manager, "_probe_video_codec", return_value="h264"),
            patch.object(manager, "_select_audio_stream", return_value=(0, "aac")),
            patch.object(manager, "_start_delay_recorder", return_value=FakeProcess()),
            patch.object(manager, "_buffer_delay_input", return_value=None),
        ):
            prepared = manager._prepare_pipeline(video, audio, profile, "negative-offset")

        delayed_playlist = str(Path(server.WORK_DIR) / "negative-offset" / "audio_delay.m3u8")
        self.assertEqual(prepared.audio_input, prepared.audio_snapshot_input)
        self.assertEqual(prepared.audio_input[-2:], ["-i", delayed_playlist])

    def test_4k_resolution_prefers_garyshare_source(self):
        sources = [
            {"url": "http://example.test/first.m3u", "text": "", "label": "first"},
            {"url": "https://garyshare.sharewithyou.dpdns.org/garyshare.m3u", "text": "", "label": "gary"},
            {"url": "http://example.test/last.m3u", "text": "", "label": "last"},
        ]

        ordered = server.prefer_garyshare_4k_sources(sources, "TSN 4K CA")

        self.assertEqual(ordered[0]["url"], "https://garyshare.sharewithyou.dpdns.org/garyshare.m3u")

    def test_non_4k_resolution_keeps_source_order(self):
        sources = [
            {"url": "http://example.test/first.m3u", "text": "", "label": "first"},
            {"url": "https://garyshare.sharewithyou.dpdns.org/garyshare.m3u", "text": "", "label": "gary"},
        ]

        ordered = server.prefer_garyshare_4k_sources(sources, "cctv5")

        self.assertEqual(ordered, sources)

    def test_source_cache_stall_classifies_audio_when_only_audio_is_stale(self):
        manager = server.LiveManager()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video_playlist = root / "video_cache.m3u8"
            audio_playlist = root / "audio_cache.m3u8"
            video_segment = root / "video_cache_000000.ts"
            audio_segment = root / "audio_cache_000000.ts"
            video_playlist.write_text("#EXTM3U\n#EXTINF:4,\nvideo_cache_000000.ts\n", encoding="utf-8")
            audio_playlist.write_text("#EXTM3U\n#EXTINF:4,\naudio_cache_000000.ts\n", encoding="utf-8")
            video_segment.write_text("video", encoding="utf-8")
            audio_segment.write_text("audio", encoding="utf-8")
            old = time.time() - 120
            os.utime(audio_playlist, (old, old))
            os.utime(audio_segment, (old, old))
            source_cache = server.LocalSourceCache(
                root,
                server.Channel(name="video", url=str(video_playlist)),
                server.Channel(name="audio", url=str(audio_playlist)),
            )

            failure = manager._source_cache_stall_failure(source_cache, timeout=30)

        self.assertIsNotNone(failure)
        self.assertEqual(failure.kind, "audio")

    def test_preserve_hls_skips_clear_hls(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        video = server.Channel(name="video", url="http://example.test/video.m3u8")
        audio = server.Channel(name="audio", url="")

        with (
            patch.object(manager, "_clear_hls") as clear_hls,
            patch.object(manager, "_stop_processes", return_value=None),
            patch.object(manager, "_start_local_source_cache", side_effect=RuntimeError("cache fail")),
        ):
            result = manager._run_pipeline(video, audio, profile, preserve_hls=True)

        clear_hls.assert_not_called()
        self.assertIn("local source cache failed", str(result))

    def test_should_preserve_hls_uses_recent_output_window(self):
        manager = server.LiveManager()
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            playlist = hls_dir / "index.m3u8"
            segment = hls_dir / "live_000001.m4s"
            playlist.write_text("#EXTM3U\n", encoding="utf-8")
            segment.write_text("seg", encoding="utf-8")
            now = time.time()
            os.utime(playlist, (now, now))
            os.utime(segment, (now, now))

            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server, "HLS_RECOVERY_WINDOW_SECONDS", 300):
                self.assertTrue(manager._should_preserve_hls())

                old = now - 400
                os.utime(playlist, (old, old))
                os.utime(segment, (old, old))
                self.assertFalse(manager._should_preserve_hls())

    def test_run_pipeline_prefers_recovery_window(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        video = server.Channel(name="video", url="http://example.test/video.m3u8")
        audio = server.Channel(name="audio", url="http://example.test/audio.m3u8")

        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            playlist = hls_dir / "index.m3u8"
            segment = hls_dir / "live_000001.m4s"
            playlist.write_text("#EXTM3U\n", encoding="utf-8")
            segment.write_text("seg", encoding="utf-8")
            now = time.time()
            os.utime(playlist, (now, now))
            os.utime(segment, (now, now))

            with (
                patch.object(server, "HLS_DIR", hls_dir),
                patch.object(manager, "_stop_processes", return_value=None),
                patch.object(manager, "_clear_hls") as clear_hls,
                patch.object(manager, "_start_local_source_cache", side_effect=RuntimeError("cache fail")),
            ):
                result = manager._run_pipeline(video, audio, profile, preserve_hls=False)

        clear_hls.assert_not_called()
        self.assertIn("local source cache failed", str(result))

    def test_publish_hls_playlist_copies_staging_playlist_into_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            run_playlist = hls_dir / "run_001014.m3u8"
            run_playlist.write_text(
                "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-MAP:URI=\"init_run_001014.mp4\"\n#EXTINF:8,\nlive_001014.m4s\n",
                encoding="utf-8",
            )
            (hls_dir / "init_run_001014.mp4").write_text("init", encoding="utf-8")
            (hls_dir / "live_001014.m4s").write_text("seg", encoding="utf-8")

            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None):
                manager = server.LiveManager()
                manager._publish_hls_playlist(run_playlist.resolve())

                index = hls_dir / "index.m3u8"
                self.assertFalse(index.is_symlink())
                self.assertEqual(index.read_text(encoding="utf-8"), run_playlist.read_text(encoding="utf-8"))
                self.assertEqual(manager.active_hls_playlist, run_playlist)

    def test_sync_active_hls_playlist_refreshes_index_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            index = hls_dir / "index.m3u8"
            run_playlist = hls_dir / "run_001014.m3u8"
            index.write_text("#EXTM3U\n#EXTINF:8,\nlive_000001.m4s\n", encoding="utf-8")
            run_playlist.write_text("#EXTM3U\n#EXTINF:8,\nlive_001014.m4s\n", encoding="utf-8")

            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None):
                manager = server.LiveManager()
                manager.active_hls_playlist = run_playlist.resolve()
                manager._sync_active_hls_playlist()
                self.assertEqual(index.read_text(encoding="utf-8"), run_playlist.read_text(encoding="utf-8"))

                run_playlist.write_text("#EXTM3U\n#EXTINF:8,\nlive_001015.m4s\n", encoding="utf-8")
                manager._sync_active_hls_playlist()
                self.assertEqual(index.read_text(encoding="utf-8"), run_playlist.read_text(encoding="utf-8"))

    def test_current_served_hls_playlist_prefers_active_playlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            active = hls_dir / "run_002021.m3u8"
            active.write_text("#EXTM3U\n", encoding="utf-8")
            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None):
                manager = server.LiveManager()
                manager.active_hls_playlist = active.resolve()
                self.assertEqual(manager._current_served_hls_playlist(), active.resolve())

    def test_status_uses_active_playlist_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            index = hls_dir / "index.m3u8"
            run_playlist = hls_dir / "run_002021.m3u8"
            index.write_text("#EXTM3U\n#EXTINF:8,\nlive_000021.m4s\n", encoding="utf-8")
            run_playlist.write_text("#EXTM3U\n#EXTINF:8,\nlive_002021.m4s\n", encoding="utf-8")
            (hls_dir / "live_000021.m4s").write_text("old", encoding="utf-8")
            (hls_dir / "live_002021.m4s").write_text("new", encoding="utf-8")
            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None):
                manager = server.LiveManager()
                manager.active_hls_playlist = run_playlist.resolve()
                status = manager.get_status()
                self.assertEqual(status["hls"]["latest_segment"], "live_002021.m4s")
                self.assertEqual(status["hls"]["segment_count"], 1)
                self.assertEqual(status["hls"]["source_playlist"], "run_002021.m3u8")

    def test_publish_hls_playlist_preserves_old_assets_for_grace_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            index = hls_dir / "index.m3u8"
            old_init = hls_dir / "init_index.mp4"
            old_seg = hls_dir / "live_000090.m4s"
            new_run = hls_dir / "run_004104.m3u8"
            new_init = hls_dir / "init_run_004104.mp4"
            new_seg = hls_dir / "live_004104.m4s"

            index.write_text(
                "#EXTM3U\n#EXT-X-MAP:URI=\"init_index.mp4\"\n#EXTINF:8,\nlive_000090.m4s\n",
                encoding="utf-8",
            )
            old_init.write_text("old-init", encoding="utf-8")
            old_seg.write_text("old-seg", encoding="utf-8")
            new_run.write_text(
                "#EXTM3U\n#EXT-X-DISCONTINUITY\n#EXT-X-MAP:URI=\"init_run_004104.mp4\"\n#EXTINF:8,\nlive_004104.m4s\n",
                encoding="utf-8",
            )
            new_init.write_text("new-init", encoding="utf-8")
            new_seg.write_text("new-seg", encoding="utf-8")

            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server, "HLS_CLIENT_GRACE_SECONDS", 240), patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None):
                manager = server.LiveManager()
                manager.active_hls_playlist = index.resolve()
                manager._publish_hls_playlist(new_run.resolve())

                self.assertTrue(old_init.exists())
                self.assertTrue(old_seg.exists())
                self.assertTrue(new_init.exists())
                self.assertTrue(new_seg.exists())
                self.assertGreaterEqual(len(manager.hls_preserved_assets), 1)

    def test_run_pipeline_preserves_existing_index_assets_before_handoff(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        video = server.Channel(name="video", url="http://example.test/video.m3u8")
        audio = server.Channel(name="audio", url="http://example.test/audio.m3u8")

        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            index = hls_dir / "index.m3u8"
            (hls_dir / "init_index.mp4").write_text("old-init", encoding="utf-8")
            (hls_dir / "live_000010.m4s").write_text("old-seg", encoding="utf-8")
            index.write_text(
                "#EXTM3U\n#EXT-X-MAP:URI=\"init_index.mp4\"\n#EXTINF:8,\nlive_000010.m4s\n",
                encoding="utf-8",
            )

            with (
                patch.object(server, "HLS_DIR", hls_dir),
                patch.object(manager, "_stop_processes", return_value=None),
                patch.object(manager, "_start_local_source_cache", side_effect=RuntimeError("cache fail")),
            ):
                result = manager._run_pipeline(video, audio, profile, preserve_hls=True)

        self.assertIn("local source cache failed", str(result))
        self.assertGreaterEqual(len(manager.hls_preserved_assets), 1)
        preserved = manager.hls_preserved_assets[-1]["referenced"]
        self.assertIn("init_index.mp4", preserved)
        self.assertIn("live_000010.m4s", preserved)

    def test_restart_defers_restart_until_old_thread_stops(self):
        manager = server.LiveManager()
        manager.thread = threading.Thread(target=time.sleep, args=(0.2,), daemon=True)
        manager.thread.start()
        calls = []

        def fake_stop(source="manual", cancel_restart=True):
            manager.stop_event.set()
            manager.status["stage"] = "stopped"

        def fake_start(source="manual"):
            calls.append(source)

        with patch.object(manager, "stop", side_effect=fake_stop), patch.object(manager, "start", side_effect=fake_start):
            manager.restart(source="manual")
            time.sleep(0.5)

        self.assertEqual(calls, ["manual"])

    def test_reap_previous_manager_pid_ignores_reused_non_server_pid(self):
        manager = server.LiveManager()
        manager.managed_pidfile.parent.mkdir(parents=True, exist_ok=True)
        manager.managed_pidfile.write_text("12345", encoding="utf-8")

        with (
            patch.object(server, "proc_alive", return_value=True),
            patch.object(server, "proc_cmdline", return_value="/bin/bash"),
            patch.object(server, "kill_pid") as kill_pid,
        ):
            manager._reap_previous_manager_pid()

        kill_pid.assert_not_called()

    def test_monitor_alignment_early_mismatch_realigns(self):
        manager = server.LiveManager()
        monitor = server.AlignmentMonitor()
        monitor.checks = 1
        profile = server.DEFAULT_PROFILE.copy()
        profile["offset_seconds"] = 0.0
        profile["auto_align_threshold"] = 1.0
        with patch.object(manager, "_read_top_quarter_clock", side_effect=[
            server.ClockSample(0.0, 4980.0, "83:00"),
            server.ClockSample(0.0, 4968.0, "82:48"),
        ]):
            offset, message = manager._monitor_alignment_from_frames("video.jpg", "audio.jpg", profile, monitor)

        self.assertEqual(offset, 12.0)
        self.assertIn("early mismatch", message)

    def test_monitor_alignment_single_mismatch_realigns_immediately(self):
        manager = server.LiveManager()
        monitor = server.AlignmentMonitor()
        monitor.checks = 3
        profile = server.DEFAULT_PROFILE.copy()
        profile["offset_seconds"] = 0.0
        profile["auto_align_threshold"] = 1.0
        profile["auto_align_samples"] = 3

        with patch.object(manager, "_read_top_quarter_clock", side_effect=[
            server.ClockSample(0.0, 4640.0, "77:20"),
            server.ClockSample(0.0, 4632.0, "77:12"),
        ]):
            offset, message = manager._monitor_alignment_from_frames("video.jpg", "audio.jpg", profile, monitor)

        self.assertEqual(offset, 8.0)
        self.assertIn("mismatch; new offset 8.000s", message)

    def test_verify_alignment_candidate_accepts_matched_delayed_pair(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["auto_align_threshold"] = 1.0
        profile["timeout_seconds"] = 5
        video = server.Channel(name="video", url="http://example.test/video.m3u8")
        audio = server.Channel(name="audio", url="http://example.test/audio.m3u8")
        monitor = server.AlignmentMonitor()
        manager.status["stage"] = "running"
        video_cap = server.FrameCaptureResult(Path("verify-video.jpg"), "video", 1.0, 1.1)
        audio_cap = server.FrameCaptureResult(Path("verify-audio.jpg"), "audio", 1.0, 1.1)

        with (
            patch.object(manager, "_probe_video_codec", return_value="h264"),
            patch.object(manager, "_start_delay_recorder", return_value=FakeProcess()) as start_delay,
            patch.object(manager, "_buffer_delay_input", return_value=None) as buffer_delay,
            patch.object(manager, "_capture_frame_pair", return_value=(video_cap, audio_cap)),
            patch.object(manager, "_read_top_quarter_clock", side_effect=[
                server.ClockSample(0.0, 2070.0, "34:30"),
                server.ClockSample(0.0, 2070.0, "34:30"),
            ]),
            patch.object(manager, "_stop_processes", return_value=None),
        ):
            verified, message = manager._verify_alignment_candidate(video, audio, profile, monitor, 20.0)

        self.assertTrue(verified)
        self.assertIn("verified offset 20.000s", message)
        self.assertEqual(start_delay.call_args.args[5], "video")
        self.assertEqual(buffer_delay.call_args.args[2], 20.0)
        self.assertEqual(manager.status["stage"], "running")

    def test_verify_alignment_candidate_rejects_unmatched_delayed_pair(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["auto_align_threshold"] = 1.0
        profile["timeout_seconds"] = 5
        video = server.Channel(name="video", url="http://example.test/video.m3u8")
        audio = server.Channel(name="audio", url="http://example.test/audio.m3u8")
        monitor = server.AlignmentMonitor()
        manager.status["stage"] = "running"
        video_cap = server.FrameCaptureResult(Path("verify-video.jpg"), "video", 1.0, 1.1)
        audio_cap = server.FrameCaptureResult(Path("verify-audio.jpg"), "audio", 1.0, 1.1)

        with (
            patch.object(manager, "_probe_video_codec", return_value="h264"),
            patch.object(manager, "_start_delay_recorder", return_value=FakeProcess()),
            patch.object(manager, "_buffer_delay_input", return_value=None),
            patch.object(manager, "_capture_frame_pair", return_value=(video_cap, audio_cap)),
            patch.object(manager, "_read_top_quarter_clock", side_effect=[
                server.ClockSample(0.0, 2070.0, "34:30"),
                server.ClockSample(0.0, 2050.0, "34:10"),
            ]),
            patch.object(manager, "_stop_processes", return_value=None),
        ):
            verified, message = manager._verify_alignment_candidate(video, audio, profile, monitor, 20.0)

        self.assertFalse(verified)
        self.assertIn("verify mismatch", message)
        self.assertEqual(manager.status["stage"], "running")

    def test_ocr_time_skips_cooldown_fallback_provider(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["ocr_provider"] = "custom"
        profile["ocr_api_key"] = "x"
        profile["ocr_custom_endpoint"] = "https://example.test"
        profile["ocrspace_api_key"] = "y"
        manager.ocr_provider_cooldowns["ocrspace"] = time.time() + 60
        with (
            patch.object(manager, "get_profile", return_value=profile),
            patch.object(manager, "_ocr_region_with_provider", return_value=None) as region,
            patch.object(manager, "_log_ocr_provider_failure", return_value=None),
        ):
            result = manager._ocr_time("frame.jpg", (0.1, 0.1, 0.1, 0.1))

        self.assertIsNone(result)
        self.assertEqual(region.call_count, 1)
        self.assertEqual(region.call_args_list[0].args[0], "custom")

    def test_effective_local_cache_seconds_is_capped(self):
        profile = server.DEFAULT_PROFILE.copy()
        profile["segment_time"] = 8.0
        profile["local_cache_seconds"] = 360
        profile["offset_seconds"] = 180.0

        seconds = server.effective_local_cache_seconds(profile)

        self.assertEqual(seconds, 120)

    def test_startup_hls_wait_timeout_includes_large_offset_buffer(self):
        profile = server.DEFAULT_PROFILE.copy()
        profile["timeout_seconds"] = 25
        profile["segment_time"] = 8.0
        profile["offset_seconds"] = 30.0

        timeout = server.startup_hls_wait_timeout(profile)

        self.assertEqual(timeout, 74)

    def test_startup_hls_wait_timeout_matches_stall_timeout_without_offset(self):
        profile = server.DEFAULT_PROFILE.copy()
        profile["timeout_seconds"] = 25
        profile["segment_time"] = 8.0
        profile["offset_seconds"] = 0.0

        timeout = server.startup_hls_wait_timeout(profile)

        self.assertEqual(timeout, server.hls_stall_timeout(profile))

    def test_normalize_profile_clamps_auto_align_interval_to_60_seconds(self):
        manager = server.LiveManager()

        normalized = manager.normalize_profile({"auto_align_interval": 15})

        self.assertEqual(normalized["auto_align_interval"], 60)

    def test_normalize_profile_defaults_auto_align_debug_override_off(self):
        manager = server.LiveManager()

        normalized = manager.normalize_profile({})

        self.assertFalse(normalized["auto_align_debug_override"])

    def test_normalize_profile_strips_audio_fallbacks(self):
        manager = server.LiveManager()

        normalized = manager.normalize_profile({"audio_fallbacks": [" alt 1 ", "", "alt 2"]})

        self.assertEqual(normalized["audio_fallbacks"], ["alt 1", "alt 2"])

    def test_resolve_audio_channel_uses_fallback_when_primary_fails(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile.update({
            "audio_channel": "primary audio",
            "audio_fallbacks": ["backup audio"],
        })
        sources = [{"url": "http://example.test/audio.m3u8", "text": "", "label": "audio"}]

        def resolve_side_effect(_sources, channel_name, force=False):
            if channel_name == "primary audio":
                raise RuntimeError("primary missing")
            return server.Channel(name=channel_name, url=f"http://example.test/{channel_name.replace(' ', '-')}.m3u8")

        with patch.object(manager.resolver, "find_any_sources", side_effect=resolve_side_effect) as resolve:
            audio, active_channel = manager._resolve_audio_channel(profile, sources, force=True)

        self.assertEqual(active_channel, "backup audio")
        self.assertEqual(audio.name, "backup audio")
        self.assertEqual(resolve.call_args_list[0].args[1], "primary audio")
        self.assertEqual(resolve.call_args_list[1].args[1], "backup audio")

    def test_run_loop_fails_when_all_audio_fallbacks_fail(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile.update({
            "video_primary": "video main",
            "video_playlist": "http://example.test/video.m3u8",
            "audio_playlist": "http://example.test/audio.m3u8",
            "audio_channel": "audio main",
            "audio_fallbacks": ["audio backup"],
        })

        def resolve_side_effect(_sources, channel_name, force=False):
            if channel_name == "video main":
                return server.Channel(name=channel_name, url="http://example.test/video-main.m3u8")
            raise RuntimeError(f"{channel_name} missing")

        with patch.object(manager, "get_profile", return_value=profile), \
            patch.object(server, "m3u_sources", side_effect=[
                [{"url": "http://example.test/video.m3u8", "text": "", "label": "video"}],
                [{"url": "http://example.test/audio.m3u8", "text": "", "label": "audio"}],
            ]), \
            patch.object(manager.resolver, "find_any_sources", side_effect=resolve_side_effect), \
            patch.object(manager, "_run_pipeline") as run_pipeline:
            manager._run_loop()

        run_pipeline.assert_not_called()
        self.assertEqual(manager.status["stage"], "stopped")
        self.assertIn("audio source unavailable: audio backup missing", manager.status["last_error"])

    def test_run_loop_records_active_audio_fallback_channel(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile.update({
            "video_primary": "video main",
            "video_playlist": "http://example.test/video.m3u8",
            "audio_playlist": "http://example.test/audio.m3u8",
            "audio_channel": "audio main",
            "audio_fallbacks": ["audio backup"],
        })

        def resolve_side_effect(_sources, channel_name, force=False):
            if channel_name == "video main":
                return server.Channel(name=channel_name, url="http://example.test/video-main.m3u8")
            if channel_name == "audio main":
                raise RuntimeError("audio main missing")
            if channel_name == "audio backup":
                return server.Channel(name=channel_name, url="http://example.test/audio-backup.m3u8")
            raise AssertionError(channel_name)

        with patch.object(manager, "get_profile", return_value=profile), \
            patch.object(server, "m3u_sources", side_effect=[
                [{"url": "http://example.test/video.m3u8", "text": "", "label": "video"}],
                [{"url": "http://example.test/audio.m3u8", "text": "", "label": "audio"}],
            ]), \
            patch.object(manager.resolver, "find_any_sources", side_effect=resolve_side_effect), \
            patch.object(manager, "_run_pipeline", side_effect=lambda *args, **kwargs: manager.stop_event.set() or "stopped"), \
            patch.object(manager, "_should_preserve_hls", return_value=False):
            manager._run_loop()

        self.assertEqual(manager.status["active_audio_channel"], "audio backup")
        self.assertEqual(manager.status["audio_url"], "http://example.test/audio-backup.m3u8")

    def test_auto_align_schedule_gate_blocks_when_schedule_inactive_by_default(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["schedule_enabled"] = True
        profile["auto_align_debug_override"] = False
        manager.schedule_last_refresh = time.time()

        with patch.object(manager, "_active_and_next_match", return_value=(None, None)):
            allowed = manager._auto_align_allowed_by_schedule(profile)

        self.assertFalse(allowed)

    def test_auto_align_schedule_gate_allows_when_debug_override_enabled(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["schedule_enabled"] = True
        profile["auto_align_debug_override"] = True
        manager.schedule_events = [{"id": "match-1"}]
        manager.schedule_last_refresh = time.time()

        with patch.object(manager, "_active_and_next_match", return_value=(None, None)):
            allowed = manager._auto_align_allowed_by_schedule(profile)

        self.assertTrue(allowed)

    def test_status_reports_auto_align_debug_override(self):
        manager = server.LiveManager()
        manager.profile["auto_align_debug_override"] = True

        status = manager.get_status()

        self.assertTrue(status["auto_align"]["debug_override"])

    def test_send_custom_ocr_crop_prefers_responses_endpoint(self):
        manager = server.LiveManager()

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self):
                return self.payload

        requests = []

        def fake_urlopen(req, timeout=0):
            requests.append(req.full_url)
            return FakeResponse(b'{"output_text":"71:43"}')

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = manager._send_custom_ocr_crop(server.np.zeros((4, 4, 3), dtype=server.np.uint8), "https://example.test/v1", "key", "gpt-5.4")

        self.assertEqual(result, "71:43")
        self.assertEqual(requests, ["https://example.test/v1/responses"])

    def test_send_custom_ocr_crop_falls_back_to_chat_completions(self):
        manager = server.LiveManager()

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self):
                return self.payload

        requests = []

        def fake_urlopen(req, timeout=0):
            requests.append(req.full_url)
            if req.full_url.endswith("/responses"):
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
            return FakeResponse(b'{"choices":[{"message":{"content":"71:44"}}]}')

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = manager._send_custom_ocr_crop(server.np.zeros((4, 4, 3), dtype=server.np.uint8), "https://example.test/v1", "key", "gpt-5.4")

        self.assertEqual(result, "71:44")
        self.assertEqual(requests, [
            "https://example.test/v1/responses",
            "https://example.test/v1/chat/completions",
        ])

    def test_save_snapshot_from_frame_updates_last_ocr_results(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        frame_file = Path(server.STATE_DIR) / "test-frame.jpg"
        frame_file.parent.mkdir(parents=True, exist_ok=True)
        frame_file.write_bytes(b"frame")

        with (
            patch.object(manager, "_ocr_time", return_value=(474, "07:54")),
            patch.object(manager, "_prune_snapshots", return_value=None),
            patch.object(manager, "_roi_crop", return_value=None),
            patch.object(server, "cv2") as fake_cv2,
            patch.object(server.os, "replace", return_value=None),
        ):
            fake_cv2.imwrite.return_value = True
            result = manager._save_snapshot_from_frame(frame_file, "video", profile, "active video")

        self.assertEqual(result["clock"], "07:54")
        self.assertEqual(manager.status["last_ocr_results"]["video"]["clock"], "07:54")
        self.assertEqual(manager.status["last_ocr_results"]["video"]["game_time"], 474)

    def test_ocr_time_retries_expanded_roi_for_stoppage_clock(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["ocr_provider"] = "custom"
        profile["ocr_api_key"] = "x"
        profile["ocr_custom_endpoint"] = "https://example.test"
        roi = (0.05, 0.05, 0.07, 0.05)

        with (
            patch.object(manager, "get_profile", return_value=profile),
            patch.object(manager, "_ocr_region_with_provider", side_effect=[
                (5400, "ENG 0-0 BRA 90:00"),
                (5520, "ENG 0-0 BRA 90:00+02:00"),
            ]) as region,
            patch.object(manager, "_log_ocr_provider_failure", return_value=None),
        ):
            result = manager._ocr_time("frame.jpg", roi)

        self.assertEqual(result, (5520, "90:00+02:00"))
        self.assertEqual(region.call_count, 2)

    def test_ocr_time_does_not_retry_non_stoppage_clock(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile["ocr_provider"] = "custom"
        profile["ocr_api_key"] = "x"
        profile["ocr_custom_endpoint"] = "https://example.test"
        roi = (0.05, 0.05, 0.07, 0.05)

        with (
            patch.object(manager, "get_profile", return_value=profile),
            patch.object(manager, "_ocr_region_with_provider", return_value=(3870, "ENG 1-0 BRA 64:30")) as region,
            patch.object(manager, "_log_ocr_provider_failure", return_value=None),
        ):
            result = manager._ocr_time("frame.jpg", roi)

        self.assertEqual(result, (3870, "64:30"))
        self.assertEqual(region.call_count, 1)

    def test_parse_clock_text_accepts_plain_timer(self):
        manager = server.LiveManager()

        parsed = manager._parse_clock_text("90:00")

        self.assertIsNotNone(parsed)

    def test_parse_ocr_text_candidates_rejects_no_scoreboard_reply(self):
        manager = server.LiveManager()

        parsed = manager._parse_ocr_text_candidates("NO_SCOREBOARD")

        self.assertIsNone(parsed)

    def test_parse_ocr_text_candidates_rejects_timer_without_team_or_score(self):
        manager = server.LiveManager()

        parsed = manager._parse_ocr_text_candidates("90:00")

        self.assertIsNone(parsed)

    def test_parse_ocr_text_candidates_accepts_timer_with_scoreboard_features(self):
        manager = server.LiveManager()

        parsed = manager._parse_ocr_text_candidates("ENG 0-0 BRA 90:00")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.text, "90:00")

    def test_prune_handoff_hls_limits_segments_and_prunes_old_run_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hls_dir = root / "hls"
            work_dir = root / "work"
            hls_dir.mkdir()
            work_dir.mkdir()
            index = hls_dir / "index.m3u8"
            run_playlist = hls_dir / "run_000100.m3u8"
            index.write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init_run_000100.mp4\"\n#EXTINF:8,\nlive_000100.m4s\n", encoding="utf-8")
            run_playlist.write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init_run_000100.mp4\"\n#EXTINF:8,\nlive_000100.m4s\n", encoding="utf-8")
            (hls_dir / "init_run_000100.mp4").write_text("init", encoding="utf-8")
            for i in range(80, 111):
                (hls_dir / f"live_{i:06d}.m4s").write_text("seg", encoding="utf-8")
            old_run = work_dir / "run_old"
            old_run.mkdir()
            old_file = old_run / "video_delay.m3u8"
            old_file.write_text("#EXTM3U\n", encoding="utf-8")
            old_time = time.time() - 1000
            os.utime(old_run, (old_time, old_time))
            os.utime(old_file, (old_time, old_time))

            with (
                patch.object(server, "HLS_DIR", hls_dir),
                patch.object(server, "WORK_DIR", work_dir),
                patch.object(server, "HLS_CLIENT_GRACE_SECONDS", 60),
            ):
                manager = server.LiveManager()
                manager.active_hls_playlist = run_playlist.resolve()
                manager._prune_handoff_hls()

            remaining_segments = sorted(p.name for p in hls_dir.glob("live_*.m4s"))
            self.assertIn("live_000100.m4s", remaining_segments)
            self.assertLessEqual(len(remaining_segments), 13)
            self.assertFalse(old_run.exists())

    def test_reclaim_runtime_processes_prunes_hls_on_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hls_dir = root / "hls"
            work_dir = root / "work"
            state_dir = root / "state"
            hls_dir.mkdir()
            work_dir.mkdir()
            state_dir.mkdir()
            index = hls_dir / "index.m3u8"
            index.write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init_index.mp4\"\n#EXTINF:8,\nlive_000100.m4s\n", encoding="utf-8")
            (hls_dir / "init_index.mp4").write_text("init", encoding="utf-8")
            for i in range(80, 110):
                (hls_dir / f"live_{i:06d}.m4s").write_text("seg", encoding="utf-8")

            with (
                patch.object(server, "HLS_DIR", hls_dir),
                patch.object(server, "WORK_DIR", work_dir),
                patch.object(server, "STATE_DIR", state_dir),
                patch.object(server, "HLS_CLIENT_GRACE_SECONDS", 1),
                patch.object(server, "proc_alive", return_value=False),
                patch.object(server, "kill_pid", return_value=True),
            ):
                manager = server.LiveManager()

            remaining_segments = sorted(p.name for p in hls_dir.glob("live_*.m4s"))
            self.assertLessEqual(len(remaining_segments), 13)
            self.assertEqual(manager.managed_pidfile, state_dir / "live_manager.pid")

    def test_prune_handoff_hls_can_be_called_repeatedly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hls_dir = root / "hls"
            work_dir = root / "work"
            hls_dir.mkdir()
            work_dir.mkdir()
            run_playlist = hls_dir / "run_000100.m3u8"
            run_playlist.write_text("#EXTM3U\n#EXT-X-MAP:URI=\"init_run_000100.mp4\"\n#EXTINF:8,\nlive_000100.m4s\n", encoding="utf-8")
            (hls_dir / "index.m3u8").write_text(run_playlist.read_text(encoding="utf-8"), encoding="utf-8")
            (hls_dir / "init_run_000100.mp4").write_text("init", encoding="utf-8")
            for i in range(80, 111):
                (hls_dir / f"live_{i:06d}.m4s").write_text("seg", encoding="utf-8")

            with (
                patch.object(server, "HLS_DIR", hls_dir),
                patch.object(server, "WORK_DIR", work_dir),
                patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None),
            ):
                manager = server.LiveManager()
                manager.active_hls_playlist = run_playlist.resolve()
                manager._prune_handoff_hls()
                first_count = len(list(hls_dir.glob("live_*.m4s")))
                manager._prune_handoff_hls()
                second_count = len(list(hls_dir.glob("live_*.m4s")))

            self.assertEqual(first_count, second_count)


class StreamManagerCommandTest(unittest.TestCase):
    def test_stream_manager_selected_audio_channels_preserves_primary_then_fallbacks(self):
        channels = selected_audio_channels({
            "audio": {
                "channel": "main",
                "fallback_channels": ["backup", "main", "backup 2"],
            }
        })

        self.assertEqual(channels, ["main", "backup", "backup 2"])

    def test_fmp4_init_filename_keeps_segment_filename_argument(self):
        with patch("app.stream_manager.select_aac_audio_index", return_value=0):
            cmd = build_ffmpeg_hls_cmd(
                {"settings": {"hls_segment_type": "fmp4", "timeout_seconds": 5}},
                "http://example.test/video.m3u8",
                "http://example.test/audio.m3u8",
                Path("/tmp/hls"),
                video_channel="4k",
            )

        segment_idx = cmd.index("-hls_segment_filename")
        init_idx = cmd.index("-hls_fmp4_init_filename")
        self.assertLess(init_idx, segment_idx)
        self.assertEqual(cmd[segment_idx + 1], "/tmp/hls/live_%06d.m4s")
        self.assertEqual(cmd[-1], "/tmp/hls/index.m3u8")

    def test_hls_delete_threshold_is_compact(self):
        manager = server.LiveManager()
        prepared = server.PreparedPipeline(
            offset=0.0,
            run_dir=Path("/tmp/run"),
            video_input=["-i", "/tmp/video.m3u8"],
            audio_input=["-i", "/tmp/audio.m3u8"],
            delay_procs=[],
            audio_map="1:a:0",
            video_codec="h264",
        )

        with patch.object(manager, "_start_process", return_value=FakeProcess()) as start_process:
            manager._start_mux(prepared, server.DEFAULT_PROFILE.copy(), start_number=0, playlist_path=Path("/tmp/index.m3u8"))

        cmd = start_process.call_args.args[0]
        threshold_idx = cmd.index("-hls_delete_threshold")
        self.assertEqual(cmd[threshold_idx + 1], "2")


class LocalCacheTest(unittest.TestCase):
    def test_audio_cache_keeps_video_stream_for_snapshot_ocr(self):
        manager = server.LiveManager()
        profile = server.DEFAULT_PROFILE.copy()
        profile.update({
            "timeout_seconds": 5,
            "segment_time": 4,
        })
        audio = server.Channel(name="audio", url="http://example.test/audio.m3u8")
        playlist = Path("/tmp/audio_cache.m3u8")

        with patch.object(manager, "_http_input_options", return_value=[]), \
            patch.object(manager, "_start_process", return_value=FakeProcess()) as start_process:
            manager._start_local_cache_recorder(audio, playlist, profile, "audio", audio_stream_index=2)

        cmd = start_process.call_args.args[0]
        self.assertIn("-map", cmd)
        self.assertIn("0:v:0?", cmd)
        self.assertIn("0:a:2?", cmd)

    def test_audio_delay_keeps_video_stream_for_snapshot_ocr(self):
        manager = server.LiveManager()
        playlist = Path("/tmp/audio_delay.m3u8")

        with patch.object(manager, "_http_input_options", return_value=[]), \
            patch.object(manager, "_local_hls_input_options", return_value=[]), \
            patch.object(manager, "_start_process", return_value=FakeProcess()) as start_process:
            manager._start_delay_recorder(
                server.Channel(name="audio", url="http://example.test/audio.m3u8"),
                playlist,
                "8.000",
                20,
                5,
                "audio",
                audio_stream_index=2,
            )

        cmd = start_process.call_args.args[0]
        self.assertIn("0:v:0?", cmd)
        self.assertIn("0:a:2?", cmd)
        self.assertIn("-c", cmd)


class HandlerActivePlaylistTest(unittest.TestCase):
    def test_index_route_serves_active_playlist_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            hls_dir = Path(tmp)
            index = hls_dir / "index.m3u8"
            active = hls_dir / "run_002021.m3u8"
            index.write_text("#EXTM3U\n#EXTINF:8,\nlive_000021.m4s\n", encoding="utf-8")
            active.write_text("#EXTM3U\n#EXTINF:8,\nlive_002021.m4s\n", encoding="utf-8")

            with patch.object(server, "HLS_DIR", hls_dir), patch.object(server.LiveManager, "_reclaim_runtime_processes", return_value=None):
                original_manager = server.MANAGER
                manager = server.LiveManager()
                manager.active_hls_playlist = active.resolve()
                server.MANAGER = manager
                try:
                    handler = server.Handler.__new__(server.Handler)
                    captured = {}

                    def fake_send_file(path, content_type=None):
                        captured["path"] = Path(path)
                        captured["content_type"] = content_type

                    handler.path = "/index.m3u8"
                    handler.send_file = fake_send_file
                    handler.send_error = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("send_error"))
                    handler.send_text = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("send_text"))
                    handler.do_GET()

                    self.assertEqual(captured["path"], active)
                finally:
                    server.MANAGER = original_manager


if __name__ == "__main__":
    unittest.main()
