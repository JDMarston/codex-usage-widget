"""Native, quota-first view. The host owns polling, credentials and persistence."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk
from typing import Any


class UsageView:
    """Small Tk view: no browser runtime, animation loop or chart dependency."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self.root = host.root
        self.c = host.colors
        self.freshness = []
        self.resets = []
        self.bar_widgets = []
        self.model_rows = []
        self.tooltips = {}
        self._restore_job = None
        self.view = "quota"
        self.scroll = {"quota": 0.0, "history": 0.0}
        self.build()

    def font(self, size=10, bold=False):
        return ("Segoe UI", -self.host.px(size), "bold" if bold else "normal")

    def label(self, parent, text="", *, size=12, color=None, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color or self.c["text"],
                        font=self.font(size, bold), bd=0, highlightthickness=0, **kw)

    def button(self, parent, text, command, *, primary=False):
        button = tk.Button(parent, text=text, command=command, font=self.font(12, True),
                           bg=self.c["card"] if primary else parent.cget("bg"),
                           fg=self.c["accent"] if primary else self.c["muted"],
                           activebackground=self.c["track"], activeforeground=self.c["text"],
                           disabledforeground=self.c["faint"], bd=0, highlightthickness=0,
                           cursor="hand2", padx=self.host.px(10), pady=self.host.px(7), takefocus=True)
        button.bind("<Return>", lambda _e: button.invoke())
        return button

    def tip(self, widget, text):
        self.tooltips[widget] = text
        widget.bind("<Enter>", lambda _e: self.host._show_tooltip(text))
        widget.bind("<Leave>", lambda _e: self.host._hide_tooltip())

    def fit(self, label, text):
        """Pixel-based ellipsis; keep the complete identifier available on hover."""
        def resize(_event=None):
            if not label.winfo_exists():
                return
            width = label.winfo_width() - 3
            font = tkfont.Font(font=label.cget("font"))
            value = text
            if width > 8 and font.measure(value) > width:
                while value and font.measure(value + "…") > width:
                    value = value[:-1]
                value += "…"
            label.configure(text=value)
        label.bind("<Configure>", resize)
        self.tip(label, text)
        label.after_idle(resize)

    def build(self):
        h, p = self.host, self.host.px
        h._hide_tooltip()
        if self._restore_job:
            self.root.after_cancel(self._restore_job)
            self._restore_job = None
        for child in self.root.winfo_children():
            child.destroy()
        self.freshness.clear()
        self.resets.clear()
        self.bar_widgets.clear()
        self.model_rows.clear()
        self.tooltips.clear()
        self.outer = tk.Frame(self.root, bg=self.c["window"], highlightthickness=1,
                              highlightbackground=self.c["border"])
        self.outer.pack(fill="both", expand=True)
        header = tk.Frame(self.outer, bg=self.c["window"])
        header.pack(fill="x", padx=p(18), pady=(p(15), p(8)))
        header.columnconfigure(0, weight=1)
        title = self.label(header, "Usage", size=22 if h.layout == "full" else 18, bold=True, anchor="w")
        title.grid(row=0, column=0, sticky="ew")
        for widget in (header, title):
            h._bind_drag(widget)
        self.refresh_button = self.button(header, "↻", h.refresh_now)
        self.refresh_button.grid(row=0, column=1)
        self.tip(self.refresh_button, "Refresh provider quotas (Ctrl+R)")
        size = self.button(header, "−" if h.layout == "full" else "+", h.toggle_size)
        size.grid(row=0, column=2)
        self.tip(size, "Compact view" if h.layout == "full" else "Expanded view")
        close = self.button(header, "×", h.hide)
        close.grid(row=0, column=3)
        self.tip(close, "Hide to tray · right-click tray icon to quit")
        self.subtitle = self.label(header, "Checking connected providers…", size=11, color=self.c["muted"], anchor="w")
        self.subtitle.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(p(5), 0))
        h._bind_drag(self.subtitle)

        if h.layout == "full":
            tabs = tk.Frame(self.outer, bg=self.c["window"])
            tabs.pack(fill="x", padx=p(18), pady=(p(5), p(10)))
            self.quota_button = self.button(tabs, "Account limits", lambda: self.switch("quota"))
            self.quota_button.pack(side="left")
            self.history_button = self.button(tabs, "Token history", lambda: self.switch("history"))
            self.history_button.pack(side="left", padx=(p(4), 0))
        else:
            self.view = "quota"

        # Pack the footer before the expanding canvas so actions never clip.
        footer = tk.Frame(self.outer, bg=self.c["window"])
        footer.pack(side="bottom", fill="x", padx=p(18), pady=(p(8), p(12)))
        self.footer = self.label(footer, "", size=10, color=self.c["faint"], anchor="w")
        self.footer.pack(fill="x")
        shell = tk.Frame(self.outer, bg=self.c["window"])
        shell.pack(fill="both", expand=True, padx=p(18))
        self.canvas = tk.Canvas(shell, bg=self.c["window"], width=1, height=1, bd=0, highlightthickness=0, yscrollincrement=p(20))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.layout("Usage.Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Usage.Vertical.TScrollbar", troughcolor=self.c["window"], background=self.c["border"],
                        bordercolor=self.c["window"], lightcolor=self.c["border"], darkcolor=self.c["border"],
                        gripcount=0, borderwidth=0, width=p(5))
        style.map("Usage.Vertical.TScrollbar", background=[("active", self.c["faint"])])
        self.scrollbar = ttk.Scrollbar(shell, orient="vertical", command=self.canvas.yview, style="Usage.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self._scroll_changed)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=self.c["window"])
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", self._canvas_size)
        self.content.bind("<Configure>", self._content_size)
        self.root.bind("<MouseWheel>", self._wheel)
        self.root.bind("<Control-r>", lambda _e: h.refresh_now())
        self.render()
        h._install_resize_handles(self.outer)

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        needed = float(last) - float(first) < 0.985
        if needed and not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y", padx=(self.host.px(5), 0), before=self.canvas)
        elif not needed and self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()

    def _canvas_size(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def _content_size(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _wheel(self, event):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def switch(self, view):
        if self.host.layout != "full" or view == self.view:
            return
        self.scroll[self.view] = self.canvas.yview()[0]
        self.view = view
        self.render(preserve_scroll=False)

    def render(self, *, preserve_scroll=True):
        h, p = self.host, self.host.px
        h._hide_tooltip()
        fraction = self.canvas.yview()[0] if preserve_scroll else self.scroll[self.view]
        self.freshness.clear()
        self.resets.clear()
        self.bar_widgets.clear()
        self.model_rows.clear()
        # Release references/bindings to destroyed widgets between snapshots.
        self.tooltips = {w: t for w, t in self.tooltips.items() if w.winfo_exists() and w.winfo_parent() != str(self.content)}
        for child in self.content.winfo_children():
            child.destroy()
        self.tooltips = {w: t for w, t in self.tooltips.items() if w.winfo_exists()}
        if h.layout == "full":
            for view, button in (("quota", self.quota_button), ("history", self.history_button)):
                button.configure(bg=self.c["card"] if self.view == view else self.c["window"],
                                 fg=self.c["text"] if self.view == view else self.c["muted"])
        if self.view == "quota":
            providers = (h.state.get("provider_quotas") or {}).get("providers") or []
            if not providers:
                self.label(self.content, "Checking account limits…" if h.state.get("provider_quotas") is None else "No quota providers connected.",
                           color=self.c["muted"], anchor="w").pack(fill="x", pady=p(18))
                self.wrap(self.content, "Connect a supported provider in Hermes. Credentials stay there; this app only reads usage.")
            for provider in providers:
                self.provider_card(provider)
        else:
            self.history()
        if self._restore_job:
            self.root.after_cancel(self._restore_job)
        self._restore_job = self.root.after(75, lambda: self._restore(fraction))
        self.tick()

    def _restore(self, fraction):
        if self.canvas.winfo_exists():
            self._content_size()
            self.canvas.yview_moveto(fraction)

    def wrap(self, parent, text, *, color=None):
        label = self.label(parent, text, size=11, color=color or self.c["muted"], justify="left", anchor="w")
        label.pack(fill="x", pady=(0, self.host.px(6)))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(60, e.width - 4)))
        return label

    def provider_card(self, provider):
        h, p = self.host, self.host.px
        compact = h.layout == "small"
        card = tk.Frame(self.content, bg=self.c["card"], highlightbackground=self.c["border"], highlightthickness=1)
        card.pack(fill="x", pady=(0, p(10)))
        inside = tk.Frame(card, bg=self.c["card"])
        inside.pack(fill="x", padx=p(14), pady=p(8 if compact else 12))
        heading = tk.Frame(inside, bg=self.c["card"])
        heading.pack(fill="x")
        heading.columnconfigure(0, weight=1)
        name = self.label(heading, provider["label"], size=14, bold=True, anchor="w")
        name.grid(row=0, column=0, sticky="ew")
        freshness = self.label(heading, "", size=10, color=self.c["muted"])
        freshness.grid(row=0, column=1, padx=(p(8), 0))
        self.freshness.append((freshness, provider))
        status = provider.get("status")
        limits = h.card_limits(provider, compact)
        if limits:
            for index, limit in enumerate(limits):
                row = tk.Frame(inside, bg=self.c["card"])
                row.pack(fill="x", pady=(p(5 if compact else 9), p(3 if compact else 5)))
                row.columnconfigure(0, weight=1)
                group = str(limit.get("group") or "")
                label = str(limit.get("label") or "Allowance")
                if limit.get("kind") == "model":
                    label = f"{group} · {label}"
                text = self.label(row, label, size=11, color=self.c["muted"], anchor="w")
                text.grid(row=0, column=0, sticky="ew")
                self.fit(text, label)
                remaining = float(limit["remaining_percent"])
                self.label(row, h.format_percent(remaining) + " left", size=18 if not compact and index == 0 else 14,
                           bold=True, color=self.c["muted"] if provider.get("stale") else h._quota_color(remaining)).grid(row=0, column=1, padx=(p(8), 0))
                bar = tk.Canvas(inside, height=p(5), bd=0, highlightthickness=0, bg=self.c["track"])
                bar.pack(fill="x", pady=(0, p(7)))
                # Bar and headline both mean REMAINING, not opposite metrics.
                def draw(_e=None, widget=bar, value=remaining, stale=provider.get("stale", False)):
                    if widget.winfo_exists():
                        widget.delete("all")
                        widget.create_rectangle(0, 0, widget.winfo_width() * value / 100, widget.winfo_height(),
                                                fill=self.c["faint"] if stale else h._quota_color(value), outline="")
                bar.bind("<Configure>", draw)
                bar.after_idle(draw)
                self.bar_widgets.append(bar)
                self.tip(bar, h.quota_tooltip(limit, provider))
                reset = self.label(inside, "", size=10, color=self.c["muted"], anchor="w")
                reset.pack(fill="x")
                self.resets.append((reset, limit))
        else:
            label = {"not_reported": "Not reported", "auth_required": "Sign-in needed", "loading": "Checking…"}.get(status, "Unavailable")
            self.label(inside, label, size=18 if not compact else 15, bold=True,
                       color=self.c["muted"] if status == "not_reported" else self.c["orange"], anchor="w").pack(fill="x", pady=(p(10), p(7)))
            self.wrap(inside, provider.get("notice") or provider.get("error") or "Waiting for the provider's response.")
            if provider.get("reset_at"):
                reset = self.label(inside, "", size=10, color=self.c["muted"], anchor="w")
                reset.pack(fill="x")
                self.resets.append((reset, provider))
        if provider.get("stale"):
            self.wrap(inside, "Last known values · refresh failed", color=self.c["orange"])
        if not compact:
            source = {"grok-cli-billing": "SuperGrok · authenticated billing", "grok-grpc-web": "SuperGrok · authenticated billing",
                      "codex-usage-api": "Codex · account allowance", "google-ai-studio-rate-limit": "AI Studio · 28-day peak, not live headroom"}.get(provider.get("source"), "Provider account limits")
            line = self.label(inside, source, size=10, color=self.c["faint"], anchor="w")
            line.pack(fill="x", pady=(p(9), 0))
            self.fit(line, source)

    def history(self):
        h, p = self.host, self.host.px
        self.wrap(self.content, "Hermes-only history · cumulative processed tokens, not quota, dollars or context size.")
        session = h.state.get("session") or {}
        if session.get("available"):
            self.label(self.content, "LATEST RECORDED SESSION", size=10, bold=True, color=self.c["faint"], anchor="w").pack(fill="x", pady=(p(10), p(5)))
            model = self.label(self.content, str(session.get("model") or "Unknown model"), size=13, bold=True, anchor="w")
            model.pack(fill="x")
            self.fit(model, str(session.get("model") or "Unknown model"))
            self.wrap(self.content, f"{h.number(session.get('total_tokens'))} processed · {h.number(session.get('api_calls'))} calls")
        models = h.model_rows()
        self.label(self.content, f"ALL TRACKED MODELS  ·  {len(models)}", size=10, bold=True, color=self.c["faint"], anchor="w").pack(fill="x", pady=(p(14), p(7)))
        if not models:
            self.wrap(self.content, (h.state.get("models") or {}).get("error") or "No model history yet.")
        for item in models:
            row = tk.Frame(self.content, bg=self.c["card"])
            row.pack(fill="x", pady=(0, p(6)))
            row.columnconfigure(0, weight=1)
            name = str(item.get("model") or "Unknown model")
            title = self.label(row, name, size=12, bold=True, anchor="w")
            title.grid(row=0, column=0, sticky="ew", padx=p(10), pady=(p(9), p(3)))
            self.fit(title, name)
            self.label(row, h.number(item.get("total_tokens")), size=13, bold=True).grid(row=0, column=1, padx=p(10))
            detail = f"In {h.number(item.get('input_tokens'))}  ·  Out {h.number(item.get('output_tokens'))}  ·  Cache {h.number(item.get('cache_tokens'))}"
            caption = self.label(row, detail, size=10, color=self.c["muted"], anchor="w")
            caption.grid(row=1, column=0, columnspan=2, sticky="ew", padx=p(10), pady=(0, p(9)))
            self.fit(caption, detail)
            self.model_rows.append(row)

    def tick(self):
        h = self.host
        providers = (h.state.get("provider_quotas") or {}).get("providers") or []
        names = " · ".join(p["label"] for p in providers)
        self.subtitle.configure(text="Hermes  /  Token history" if self.view == "history" else (names + "  /  Account limits") if names else "Account limits")
        self.refresh_button.configure(state="disabled" if h.refreshing else "normal")
        for label, provider in self.freshness:
            if label.winfo_exists():
                label.configure(text=h.freshness(provider), fg=self.c["orange"] if provider.get("stale") or provider.get("status") in {"error", "auth_required"} else self.c["muted"])
        for label, limit in self.resets:
            if label.winfo_exists():
                label.configure(text=h.reset_caption(limit))
        self.footer.configure(text="Refreshing account limits…" if h.refreshing else "Hermes history · refreshed locally" if self.view == "history" else "Auto-refresh 3 min · bars show remaining")
