"""Desktop UI for the photo organizer.

Run with:  python ui.py
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import core

INPUT_FOLDER_NAME = "INPUT_PHOTO_ORGANIZOR"
OUTPUT_FOLDER_NAME = "ORGANIZED_PHOTOS"


def desktop_path() -> Path:
    candidate = Path(os.path.expanduser("~")) / "Desktop"
    if candidate.is_dir():
        return candidate
    onedrive = os.environ.get("OneDrive")
    if onedrive and (Path(onedrive) / "Desktop").is_dir():
        return Path(onedrive) / "Desktop"
    return candidate


class PhotoOrganizerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Photo Organizer")
        self.root.geometry("980x660")
        self.root.minsize(820, 560)

        desktop = desktop_path()
        self.input_var = tk.StringVar(value=str(desktop / INPUT_FOLDER_NAME))
        self.output_var = tk.StringVar(value=str(desktop / OUTPUT_FOLDER_NAME))
        default_log = desktop / "travel_log.xlsx"
        self.travel_log_var = tk.StringVar(value=str(default_log) if default_log.is_file() else "")
        self.status_var = tk.StringVar(value="Pick your folders, then scan the input folder.")

        self.plans: list[core.PhotoPlan] = []
        self.summary: core.RunSummary | None = None
        self.events: queue.Queue = queue.Queue()

        self._build_widgets()
        self._center_window()
        self.root.after(100, self._drain_events)

    def _center_window(self) -> None:
        self.root.update_idletasks()
        width, height = 980, 660
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    # ---------------------------------------------------------------- layout

    def _build_widgets(self) -> None:
        padding = {"padx": 10, "pady": 6}

        folders = ttk.LabelFrame(self.root, text="Folders")
        folders.pack(fill="x", **padding)
        folders.columnconfigure(1, weight=1)

        self._folder_row(folders, 0, "Input folder", self.input_var, self._browse_input)
        self._folder_row(folders, 1, "Output folder", self.output_var, self._browse_output)
        self._folder_row(
            folders, 2, "Travel log (optional)", self.travel_log_var, self._browse_travel_log
        )

        ttk.Label(
            folders,
            text="Photos are filed as Country \\ Year \\ Month.",
            foreground="#555555",
        ).grid(row=3, column=1, sticky="w", padx=6, pady=(0, 8))

        actions = ttk.Frame(self.root)
        actions.pack(fill="x", padx=10)

        self.scan_button = ttk.Button(
            actions, text="1. Scan input folder", command=self._start_scan
        )
        self.scan_button.pack(side="left")

        self.organize_button = ttk.Button(
            actions, text="2. Organize & move photos", command=self._start_organize, state="disabled"
        )
        self.organize_button.pack(side="left", padx=8)

        ttk.Button(actions, text="Open output folder", command=self._open_output).pack(side="left")

        self.progress = ttk.Progressbar(actions, mode="determinate", length=200)
        self.progress.pack(side="right")

        preview = ttk.LabelFrame(self.root, text="Planned result")
        preview.pack(fill="both", expand=True, **padding)

        columns = ("file", "date", "country", "matched", "newname")
        self.tree = ttk.Treeview(preview, columns=columns, show="headings", height=12)
        for column, heading, width in (
            ("file", "Current file", 210),
            ("date", "Capture date", 110),
            ("country", "Country", 140),
            ("matched", "Matched by", 90),
            ("newname", "New name", 330),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor="w")

        scrollbar = ttk.Scrollbar(preview, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.tag_configure("review", foreground="#b45309")
        self.tree.tag_configure("duplicate", foreground="#6b7280")

        log_frame = ttk.LabelFrame(self.root, text="Messages")
        log_frame.pack(fill="both", **padding)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=12, pady=(0, 10)
        )

    def _folder_row(self, parent, row, label, variable, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="Browse...", command=command).grid(
            row=row, column=2, padx=8, pady=4
        )

    # ---------------------------------------------------------------- helpers

    def _browse_input(self) -> None:
        chosen = filedialog.askdirectory(title="Select the input folder")
        if chosen:
            self.input_var.set(chosen)

    def _browse_output(self) -> None:
        chosen = filedialog.askdirectory(title="Select the output folder")
        if chosen:
            self.output_var.set(chosen)

    def _browse_travel_log(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select your travel log", filetypes=[("Excel workbook", "*.xlsx")]
        )
        if chosen:
            self.travel_log_var.set(chosen)

    def _open_output(self) -> None:
        output = Path(self.output_var.get())
        output.mkdir(parents=True, exist_ok=True)
        os.startfile(str(output))

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_paths(self, paths, base: Path, limit: int = 12) -> None:
        """List paths relative to the input folder, so nested leftovers are findable."""
        for path in paths[:limit]:
            try:
                shown = path.relative_to(base)
            except ValueError:
                shown = path
            self._log(f"    {shown}")
        if len(paths) > limit:
            self._log(f"    ... and {len(paths) - limit} more")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        if busy:
            self.organize_button.configure(state="disabled")
        elif self.plans:
            self.organize_button.configure(state="normal")

    # ---------------------------------------------------------------- scanning

    def _start_scan(self) -> None:
        input_dir = Path(self.input_var.get())
        output_dir = Path(self.output_var.get())

        if not input_dir.is_dir():
            messagebox.showerror("Input folder not found", f"This folder does not exist:\n{input_dir}")
            return

        self._clear_log()
        self.tree.delete(*self.tree.get_children())
        self.plans, self.summary = [], None
        self._set_busy(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status_var.set("Scanning photos and looking up locations...")

        travel_log = Path(self.travel_log_var.get()) if self.travel_log_var.get().strip() else None

        def worker():
            try:
                plans, summary = core.build_plan(input_dir, output_dir, travel_log)
                self.events.put(("scan_done", (plans, summary)))
            except Exception as error:
                self.events.put(("error", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_done(self, payload) -> None:
        self.plans, self.summary = payload
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._set_busy(False)

        if not self.plans:
            self.status_var.set("No photos found in the input folder.")
            self._log("The input folder contains no photos. Drop some in and scan again.")
            self.organize_button.configure(state="disabled")
            return

        for plan in self.plans:
            date_text = f"{plan.capture_date:%Y-%m-%d}" if plan.capture_date else "unknown"

            if plan.is_duplicate:
                country, matched = "Duplicate", "-"
                relative = f"stays in input - already have {plan.duplicate_of.name}"
                tags = ("duplicate",)
            else:
                country = plan.country or core.NEEDS_REVIEW_DIR
                matched = plan.country_source
                relative = str(plan.destination_path.relative_to(Path(self.output_var.get())))
                tags = ("review",) if plan.needs_review else ()

            self.tree.insert(
                "",
                "end",
                values=(plan.source_path.name, date_text, country, matched, relative),
                tags=tags,
            )

        summary = self.summary
        self.status_var.set(
            f"{summary.total} photo(s) ready - {summary.by_gps} by GPS, "
            f"{summary.by_travel_log} by travel log, {summary.needs_review} need review, "
            f"{summary.duplicates} duplicate(s)."
        )
        self._log(
            f"Found {summary.total} photo(s). Nothing has been moved yet - "
            "review the table, then click 'Organize & move photos'."
        )
        for warning in summary.warnings:
            self._log(f"Warning: {warning}")
        if summary.skipped_files:
            self._log(
                f"{len(summary.skipped_files)} file(s) are not photos and stay where they are:"
            )
            self._log_paths(summary.skipped_files, Path(self.input_var.get()))
        if summary.duplicates:
            self._log(
                f"{summary.duplicates} photo(s) are already in your library (shown in grey). "
                "They stay in the input folder so you can delete them yourself."
            )
        if summary.needs_review:
            self._log(
                f"{summary.needs_review} photo(s) will go to '{core.NEEDS_REVIEW_DIR}' "
                "(shown in orange). Adding a travel log usually fixes these."
            )

    # ---------------------------------------------------------------- organizing

    def _start_organize(self) -> None:
        if not self.plans or self.summary is None:
            return

        movable = len(self.plans) - self.summary.duplicates
        message = (
            f"{movable} photo(s) will be moved out of the input folder into:\n"
            f"{self.output_var.get()}\n\n"
        )
        message += (
            f"{self.summary.duplicates} duplicate(s) will stay in the input folder.\n\nContinue?"
            if self.summary.duplicates
            else "The input folder will be left empty. Continue?"
        )
        if not messagebox.askyesno("Move photos?", message):
            return

        self._set_busy(True)
        self.progress.configure(mode="determinate", maximum=len(self.plans), value=0)
        self.status_var.set("Moving photos...")

        plans, summary = self.plans, self.summary
        input_dir = Path(self.input_var.get())
        output_dir = Path(self.output_var.get())

        def worker():
            try:
                core.execute_plan(
                    plans, summary, move=True,
                    progress=lambda done, total: self.events.put(("progress", done)),
                )
                core.remove_empty_subfolders(input_dir)
                log_path = core.write_review_log(output_dir, plans, summary)
                self.events.put(("organize_done", (summary, log_path)))
            except Exception as error:
                self.events.put(("error", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_organize_done(self, payload) -> None:
        summary, log_path = payload
        self.progress.configure(value=summary.total)
        self.plans = []
        self._set_busy(False)
        self.organize_button.configure(state="disabled")

        remaining = (
            f"{summary.duplicates} duplicate(s) left in the input folder."
            if summary.duplicates
            else "Input folder is now empty."
        )
        self.status_var.set(f"Done - {summary.moved} photo(s) moved. {remaining}")
        self._log("")
        self._log(f"Moved {summary.moved} of {summary.total} photo(s).")
        self._log(f"  Matched by GPS       : {summary.by_gps}")
        self._log(f"  Matched by travel log: {summary.by_travel_log}")
        self._log(f"  Needed review        : {summary.needs_review}")
        self._log(f"  Duplicates skipped   : {summary.duplicates}")
        if summary.failed:
            self._log(f"  Failed               : {len(summary.failed)}")
            for path, reason in summary.failed:
                self._log(f"    - {path.name}: {reason}")
        left = core.remaining_files(Path(self.input_var.get()))
        self._log("")
        if left:
            self._log(f"Still in the input folder ({len(left)} file(s)):")
            self._log_paths(left, Path(self.input_var.get()))
        else:
            self._log("The input folder is now empty.")
        self._log(f"Log written to: {log_path}")

        self.tree.delete(*self.tree.get_children())
        messagebox.showinfo("Finished", f"{summary.moved} photo(s) organized.")

    # ---------------------------------------------------------------- event pump

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "scan_done":
                    self._on_scan_done(payload)
                elif kind == "organize_done":
                    self._on_organize_done(payload)
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._set_busy(False)
                    self._log(f"Error: {payload}")
                    self.status_var.set("Something went wrong - see messages above.")
                    messagebox.showerror("Error", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    PhotoOrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
