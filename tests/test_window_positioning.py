"""Mapped-Tk regression coverage for absolute virtual-desktop coordinates.

Exercise event bindings and read actual native window positions, not just strings.
All settings are isolated and no provider requests or pointer warps are performed.
"""
import ctypes
import json
import tempfile
import tkinter as tk
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

import app
from data_client import normalize_provider


class WindowPositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'settings.json'
        self.path.write_text(json.dumps({'always_on_top': False, 'window_x': 120, 'window_y': 120}))
        self.settings_patch = patch.object(app, 'SETTINGS_PATH', self.path)
        self.settings_patch.start()
        self.root = tk.Tk()
        self.widget = app.UsageWidget(self.root, start_hidden=False, start_polling=False, create_tray=False)
        self.settle()

    def tearDown(self):
        self.widget.quit()
        self.settings_patch.stop()
        self.temp.cleanup()

    def settle(self):
        self.root.update_idletasks()
        self.root.update()

    def position(self, window=None):
        window = window or self.root
        u = ctypes.windll.user32
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        rect = wintypes.RECT()
        self.assertTrue(u.GetWindowRect(u.GetAncestor(window.winfo_id(), 2), ctypes.byref(rect)))
        return rect.left, rect.top

    def press(self, widget=None):
        target = widget or self.widget.ui.subtitle
        self.anchor = (target.winfo_rootx()+10, target.winfo_rooty()+10)
        self.origin = self.position()
        target.event_generate('<ButtonPress-1>', x=10, y=10, rootx=self.anchor[0], rooty=self.anchor[1])
        return target

    def move(self, target, x, y):
        target.event_generate('<B1-Motion>', x=10, y=10,
                              rootx=self.anchor[0]+x-self.origin[0], rooty=self.anchor[1]+y-self.origin[1])
        self.settle()

    def release(self, target):
        target.event_generate('<ButtonRelease-1>', x=10, y=10)
        self.settle()

    def test_continuous_drag_crosses_both_zero_axes_without_edge_anchoring(self):
        target = self.press()
        for point in [(1, 120), (-1, 120), (-720, 1920), (-700, -400), (200, -300), (120, 120)]:
            with self.subTest(point=point):
                self.move(target, *point)
                self.assertEqual(self.position(), point)
        self.release(target)

    def test_drag_release_and_quota_refresh_keep_negative_position(self):
        target = self.press()
        self.move(target, -800, -200)
        self.release(target)
        self.assertEqual(self.position(), (-800, -200))
        stored = json.loads(self.path.read_text())
        self.assertEqual((stored['window_x'], stored['window_y']), (-800, -200))
        self.widget.events.put(('roster', (0, ['grok'])))
        self.widget._drain_events()
        self.settle()
        self.assertEqual(self.position(), (-800, -200))
        self.widget.hide(); self.widget.show(); self.settle()
        self.assertEqual(self.position(), (-800, -200))

    def test_saved_negative_coordinates_restore_on_the_same_monitor(self):
        self.widget.settings.update(window_x=-1000, window_y=-900)
        with patch.object(app, '_work_area', return_value=(-1920, -1080, 0, 0)):
            self.widget._set_geometry(initial=True)
        self.settle()
        self.assertEqual(self.position(), (-1000, -900))

    def test_size_toggle_uses_live_monitor_not_stale_saved_position(self):
        self.root.geometry('+-1500+1900'); self.settle()
        self.widget.settings.update(window_x=120, window_y=120)
        before = self.position()
        with patch.object(app, '_work_area', return_value=(-2304, 1843, 0, 3067)) as area:
            self.widget.toggle_size()
            self.settle()
            area.assert_called_once_with(*before)
        x, y = self.position()
        self.assertLess(x, 0)
        self.assertGreaterEqual(y, 1843)

    def test_resize_keeps_the_negative_monitor_position(self):
        self.root.geometry('+-1500+1900'); self.settle()
        target = next(child for child in self.widget.ui.outer.winfo_children()
                      if child.winfo_manager() == 'place' and child.place_info()['anchor'] == 'se')
        old_w, old_h = self.root.winfo_width(), self.root.winfo_height()
        target = self.press(target)
        # Resize uses pointer movement, not the window movement target.
        with patch.object(app, '_work_area', return_value=(-2304, 1843, 0, 3067)):
            self.move(target, self.origin[0]+40, self.origin[1]+30)
        self.release(target)
        self.assertEqual(self.position(), (-1500, 1900))
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (old_w+40, old_h+30))

    def test_tooltip_stays_on_pointer_monitor(self):
        with patch.object(self.root, 'winfo_pointerx', return_value=-1800), \
             patch.object(self.root, 'winfo_pointery', return_value=2100), \
             patch.object(app, '_work_area', return_value=(-2304, 1843, 0, 3067)):
            self.widget._show_tooltip('Test detail')
        self.settle()
        self.assertEqual(self.position(self.widget.tooltip_window), (-1788, 2114))

    def test_refresh_is_deferred_during_drag_then_applied_after_release(self):
        target = self.press()
        with patch.object(self.widget.ui, 'render', wraps=self.widget.ui.render) as render:
            self.widget.events.put(('roster', (0, ['grok'])))
            self.widget.events.put(('provider', (0, normalize_provider('grok', {
                'limits': [{'used_percent': 25, 'label': 'Weekly'}]}))))
            self.widget._drain_events()
            render.assert_not_called()
            self.assertEqual(self.widget.state['provider_quotas']['providers'][0]['limits'][0]['remaining_percent'], 75)
            self.release(target)
            self.widget._drain_events()
            render.assert_called_once()

    def test_begin_drag_closes_tooltip(self):
        self.widget._show_tooltip('Test detail')
        target = self.press()
        self.assertIsNone(self.widget.tooltip_window)
        self.release(target)


if __name__ == '__main__':
    unittest.main()
