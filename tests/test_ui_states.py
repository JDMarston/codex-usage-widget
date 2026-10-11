"""Native mapped-widget regression coverage, using explicitly synthetic data."""
import copy
import json
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from data_client import merge_provider, normalize_provider


def provider(identifier, used=25, **kw):
    return normalize_provider(identifier, {"available": True, "status": "ok", "source": "test-fixture",
        "limits": [{"id": identifier, "group": identifier.title(), "kind": "account", "label": "Weekly",
                    "used_percent": used, "reset_at": time.time() + 3600, "window_seconds": 604800}], **kw})


class SnapshotTests(unittest.TestCase):
    def test_missing_percent_rejected_for_all_providers(self):
        for item in ({}, {"used_percent": None}, {"used_percent": float('nan')}, {"used_percent": True}):
            result = normalize_provider('grok', {'limits': [item]})
            self.assertFalse(result['available'])
            self.assertIsNone(result['fetched_at'])

    def test_failed_refresh_preserves_original_time_not_new_check_time(self):
        previous = provider('grok', 40)
        failure = normalize_provider('grok', {'error': 'offline'}, now=previous['fetched_at']+100)
        result = merge_provider(previous, failure)
        self.assertEqual(result['fetched_at'], previous['fetched_at'])
        self.assertGreater(result['checked_at'], result['fetched_at'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['limits'][0]['remaining_percent'], 60)

    def test_unpublished_response_drops_prior_number(self):
        result = merge_provider(provider('grok'), normalize_provider('grok', {'status': 'not_reported'}))
        self.assertFalse(result['stale'])
        self.assertFalse(result['limits'])

    def test_auth_failure_does_not_keep_old_account_numbers(self):
        result = merge_provider(provider('grok'), normalize_provider('grok', {'status': 'auth_required'}))
        self.assertFalse(result['limits'])

    def test_tray_reports_unavailable_and_stale_instead_of_hiding_provider(self):
        grok = normalize_provider('grok', {'status': 'not_reported'})
        codex = provider('codex'); codex['stale'] = True
        title = app.tray_usage_title({'provider_quotas': {'providers': [codex, grok]}})
        self.assertIn('Codex 75% left (stale)', title)
        self.assertIn('Grok: not reported', title)

    def test_small_changes_remain_visible(self):
        self.assertEqual(app.percent(99.7), '99.7%')
        self.assertEqual(app.percent(0.01), '<0.1%')
        self.assertEqual(app.percent(float('inf')), '—')


class NativeViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Path(self.temp.name) / 'settings.json'
        self.settings.write_text(json.dumps({'always_on_top': False, 'window_x': 40, 'window_y': 40}))
        self.setting_patch = patch.object(app, 'SETTINGS_PATH', self.settings)
        self.setting_patch.start()
        self.root = tk.Tk()
        self.widget = app.UsageWidget(self.root, start_hidden=False, start_polling=False, create_tray=False)
        self.widget.state['provider_quotas'] = {'providers': [provider('codex'), provider('grok', 42)]}
        self.widget.ui.render()
        self.settle()

    def tearDown(self):
        self.widget.quit()
        self.setting_patch.stop()
        self.temp.cleanup()

    def settle(self):
        deadline = time.monotonic() + 0.12
        while time.monotonic() < deadline:
            self.root.update_idletasks()
            self.root.update()
            time.sleep(0.01)

    def texts(self):
        values = []
        def walk(widget):
            if isinstance(widget, tk.Label):
                values.append(widget.cget('text'))
            for child in widget.winfo_children():
                walk(child)
        walk(self.root)
        return '\n'.join(values)

    def test_full_compact_history_transition_and_remaining_bars(self):
        for compact in (False, True, False):
            if (self.widget.layout == 'small') != compact:
                self.widget.toggle_size()
            self.settle()
            self.assertIn('75% left', self.texts())
            self.assertIn('58% left', self.texts())
            self.assertEqual(len(self.widget.ui.bar_widgets), 2)
            for bar, expected in zip(self.widget.ui.bar_widgets, (0.75, 0.58)):
                coords = bar.coords(bar.find_all()[0])
                self.assertAlmostEqual(coords[2] / bar.winfo_width(), expected)
            footer = self.widget.ui.footer
            self.assertLessEqual(footer.winfo_rooty()+footer.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
        self.widget.ui.history_button.invoke(); self.settle()
        self.assertIn('Hermes-only history', self.texts())
        self.widget.ui.quota_button.invoke(); self.settle()
        self.assertIn('75% left', self.texts())

    def test_unchanged_clock_tick_does_not_repaint_widgets(self):
        self.widget.ui.tick()
        targets = [self.widget.ui.subtitle, self.widget.ui.refresh_button, self.widget.ui.footer]
        targets.extend(label for label, _ in self.widget.ui.freshness)
        targets.extend(label for label, _ in self.widget.ui.resets)
        patches = [patch.object(widget, 'configure', wraps=widget.configure) for widget in targets]
        mocks = [item.start() for item in patches]
        try:
            self.widget.ui.tick()
            for configured in mocks:
                configured.assert_not_called()
        finally:
            for item in patches:
                item.stop()

    def test_not_reported_and_failures_visible_in_compact(self):
        self.widget.state['provider_quotas']['providers'][1] = normalize_provider('grok', {
            'status': 'not_reported', 'notice': 'Grok did not publish a percentage.', 'reset_at': time.time()+7200})
        self.widget.toggle_size(); self.settle()
        self.assertIn('Not reported', self.texts())
        self.assertIn('Grok did not publish', self.texts())
        self.assertNotIn('100% left', self.texts())
        self.assertEqual(len(self.widget.ui.bar_widgets), 1)

    def test_hover_is_live_and_dies_on_refresh_or_hide(self):
        bar = self.widget.ui.bar_widgets[1]
        bar.event_generate('<Enter>'); self.settle()
        self.assertIsNotNone(self.widget.tooltip_window)
        self.assertIn('42% used', self.texts())
        self.widget.ui.render(); self.settle()
        self.assertIsNone(self.widget.tooltip_window)
        self.widget.ui.bar_widgets[0].event_generate('<Enter>'); self.settle()
        self.widget.hide()
        self.assertIsNone(self.widget.tooltip_window)
        self.assertEqual(self.root.state(), 'withdrawn')
        self.widget.show(); self.settle()
        self.assertEqual(self.root.state(), 'normal')

    def test_local_telemetry_does_not_relabel_quota_as_fresh(self):
        prior = copy.deepcopy(self.widget.state['provider_quotas'])
        bars = list(self.widget.ui.bar_widgets)
        self.widget.events.put(('snapshot', {'session': {'model': '', 'total_tokens': 100}}))
        self.widget._drain_events(); self.settle()
        self.assertEqual(self.widget.state['provider_quotas'], prior)
        self.assertEqual(self.widget.ui.bar_widgets, bars)

    def test_history_scroll_survives_refresh_and_all_models_accessible(self):
        self.widget.state['models'] = {'models': [{'model': 'example-model-'+str(n), 'total_tokens': 100+n} for n in range(30)]}
        self.widget.ui.switch('history'); self.settle()
        self.assertEqual(len(self.widget.ui.model_rows), 30)
        self.widget.ui.canvas.yview_moveto(1)
        self.settle()
        before = self.widget.ui.canvas.yview()[0]
        self.widget.ui.render(); self.settle()
        self.assertAlmostEqual(self.widget.ui.canvas.yview()[0], before, places=2)

    def test_stale_generation_cannot_resurrect_disconnected_provider(self):
        self.widget._generation = 2
        self.widget.state['provider_quotas']['providers'] = [provider('codex')]
        self.widget.events.put(('provider', (1, provider('grok'))))
        self.widget._drain_events(); self.settle()
        self.assertEqual([p['id'] for p in self.widget.state['provider_quotas']['providers']], ['codex'])

    def test_minimum_geometry_keeps_controls_and_bars_within_width(self):
        for layout in ('full', 'small'):
            self.widget.layout = layout
            self.widget.ui.build()
            width, height = app.MIN_SIZES[layout]
            self.root.geometry(f'{width}x{height}+40+40')
            self.settle()
            right = self.root.winfo_rootx() + self.root.winfo_width()
            self.assertLessEqual(self.widget.ui.refresh_button.winfo_rootx()+self.widget.ui.refresh_button.winfo_width(), right)
            for bar in self.widget.ui.bar_widgets:
                self.assertGreater(bar.winfo_width(), 100)
                self.assertLessEqual(bar.winfo_rootx()+bar.winfo_width(), right)


if __name__ == '__main__':
    unittest.main()
