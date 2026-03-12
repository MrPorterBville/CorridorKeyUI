from __future__ import annotations

import logging
import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from backend import ClipEntry, ClipState, CorridorKeyService, InferenceParams, OutputConfig
from backend.project import create_project, projects_root


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class ClipView:
    clip: ClipEntry
    label: str


class ToolTip:
    """Simple hover tooltip for Tk widgets."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 350):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._job = None
        self._tip_window: tk.Toplevel | None = None
        self.widget.bind("<Enter>", self._on_enter, add="+")
        self.widget.bind("<Leave>", self._on_leave, add="+")
        self.widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, _event=None) -> None:
        self._cancel_job()
        self._job = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _event=None) -> None:
        self._cancel_job()
        self._hide()

    def _cancel_job(self) -> None:
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        if self._tip_window is not None:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            win,
            text=self.text,
            justify="left",
            background="#fff8d7",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=4,
            wraplength=360,
        )
        label.pack()
        self._tip_window = win

    def _hide(self) -> None:
        if self._tip_window is not None:
            self._tip_window.destroy()
            self._tip_window = None


class CorridorKeyUI:
    """Desktop UI for scanning clips and running CorridorKey jobs."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CorridorKey UI")
        self.root.geometry("1200x760")

        self.service = CorridorKeyService()
        self.device = "not initialized"

        self._clips: list[ClipView] = []
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._busy = False

        self.clips_dir_var = tk.StringVar(value=self._default_scan_dir())
        self.status_var = tk.StringVar(value="Ready. Import a video, or choose a folder and click Scan.")
        self.progress_var = tk.StringVar(value="Idle")

        self.input_linear_var = tk.BooleanVar(value=False)
        self.despill_var = tk.DoubleVar(value=1.0)
        self.auto_despeckle_var = tk.BooleanVar(value=True)
        self.despeckle_size_var = tk.IntVar(value=400)
        self.refiner_scale_var = tk.DoubleVar(value=1.0)

        self.fg_enabled_var = tk.BooleanVar(value=True)
        self.fg_format_var = tk.StringVar(value="exr")
        self.matte_enabled_var = tk.BooleanVar(value=True)
        self.matte_format_var = tk.StringVar(value="exr")
        self.comp_enabled_var = tk.BooleanVar(value=True)
        self.comp_format_var = tk.StringVar(value="png")
        self.proc_enabled_var = tk.BooleanVar(value=True)
        self.proc_format_var = tk.StringVar(value="exr")
        self._help_texts = self._build_help_texts()

        self._build_ui()
        self._poll_events()

    def _ensure_device(self) -> None:
        """Lazy device detection: initialize only when work is requested."""
        if self.device != "not initialized":
            return
        self.device = self.service.detect_device()
        self.append_log(f"Compute device initialized: {self.device}")

    def _default_scan_dir(self) -> str:
        local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ClipsForInference")
        if os.path.isdir(local):
            return local
        return projects_root()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=3)
        self.root.columnconfigure(1, weight=2)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Clips Directory:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(top, textvariable=self.clips_dir_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(top, text="Browse", command=self.choose_clips_dir).grid(row=0, column=2, padx=8)
        self._help_button(top, row=0, column=3, help_key="what_is_corridorkey")
        ttk.Button(top, text="Import Video", command=self.import_video).grid(row=0, column=4, padx=(8, 8))
        ttk.Button(top, text="Scan", command=self.scan_clips).grid(row=0, column=5)

        left = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        left.grid(row=1, column=0, sticky="nsew")
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(left, text="Clips").grid(row=0, column=0, sticky="w")
        self.clip_list = tk.Listbox(left, selectmode=tk.EXTENDED)
        self.clip_list.grid(row=1, column=0, sticky="nsew")

        clip_scroll = ttk.Scrollbar(left, orient="vertical", command=self.clip_list.yview)
        clip_scroll.grid(row=1, column=1, sticky="ns")
        self.clip_list.configure(yscrollcommand=clip_scroll.set)

        button_row = ttk.Frame(left)
        button_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        run_gvm_btn = ttk.Button(button_row, text="Run GVM", command=self.run_gvm)
        run_gvm_btn.grid(row=0, column=0, padx=(0, 4))
        self._help_button(button_row, row=0, column=1, help_key="run_gvm")

        run_vm_btn = ttk.Button(button_row, text="Run VideoMaMa", command=self.run_videomama)
        run_vm_btn.grid(row=0, column=2, padx=(8, 4))
        self._help_button(button_row, row=0, column=3, help_key="run_videomama")

        run_inf_btn = ttk.Button(button_row, text="Run Inference", command=self.run_inference)
        run_inf_btn.grid(row=0, column=4, padx=(8, 4))
        self._help_button(button_row, row=0, column=5, help_key="run_inference")

        unload_btn = ttk.Button(button_row, text="Unload Models", command=self.unload_models)
        unload_btn.grid(row=0, column=6, padx=(8, 4))
        self._help_button(button_row, row=0, column=7, help_key="unload_models")

        right = ttk.Frame(self.root, padding=(0, 0, 10, 10))
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(2, weight=1)

        ttk.Label(right, text="Inference Parameters").grid(row=0, column=0, columnspan=2, sticky="w")

        params = ttk.LabelFrame(right, text="Model")
        params.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 8))
        params.columnconfigure(1, weight=1)
        params.columnconfigure(2, weight=0)

        input_linear = ttk.Checkbutton(params, text="Input is linear", variable=self.input_linear_var)
        input_linear.grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self._help_button(params, row=0, column=2, help_key="input_linear")
        self._tooltip(
            input_linear,
            "Enable when your input footage is already linear-light. Keep this off for normal "
            "sRGB/Rec.709 video files.",
        )

        ttk.Label(params, text="Despill strength").grid(row=1, column=0, sticky="w")
        despill = ttk.Scale(params, from_=0.0, to=1.0, variable=self.despill_var)
        despill.grid(row=1, column=1, sticky="ew")
        self._help_button(params, row=1, column=2, help_key="despill_strength")
        self._tooltip(despill, "How strongly to remove green spill from foreground colors. 1.0 is default.")

        ttk.Label(params, text="Refiner scale").grid(row=2, column=0, sticky="w")
        refiner = ttk.Scale(params, from_=0.0, to=2.0, variable=self.refiner_scale_var)
        refiner.grid(row=2, column=1, sticky="ew")
        self._help_button(params, row=2, column=2, help_key="refiner_scale")
        self._tooltip(refiner, "Multiplier for edge/detail refinement. 1.0 is balanced. Higher can over-sharpen.")

        auto_despeckle = ttk.Checkbutton(params, text="Auto despeckle", variable=self.auto_despeckle_var)
        auto_despeckle.grid(
            row=3, column=0, columnspan=2, sticky="w"
        )
        self._help_button(params, row=3, column=2, help_key="auto_despeckle")
        self._tooltip(
            auto_despeckle,
            "Automatically removes tiny isolated matte islands (tracking dots/noise) from the result.",
        )
        ttk.Label(params, text="Despeckle size").grid(row=4, column=0, sticky="w")
        despeckle_size = ttk.Entry(params, textvariable=self.despeckle_size_var, width=8)
        despeckle_size.grid(row=4, column=1, sticky="w")
        self._help_button(params, row=4, column=2, help_key="despeckle_size")
        self._tooltip(
            despeckle_size,
            "Maximum connected-pixel area removed by auto despeckle. Larger values remove bigger specks.",
        )

        outputs = ttk.LabelFrame(right, text="Outputs")
        outputs.grid(row=2, column=0, columnspan=2, sticky="nsew")
        outputs.columnconfigure(1, weight=1)
        outputs.columnconfigure(2, weight=0)

        self._output_row(outputs, 0, "FG", self.fg_enabled_var, self.fg_format_var)
        self._output_row(outputs, 1, "Matte", self.matte_enabled_var, self.matte_format_var)
        self._output_row(outputs, 2, "Comp", self.comp_enabled_var, self.comp_format_var)
        self._output_row(outputs, 3, "Processed", self.proc_enabled_var, self.proc_format_var)

        log_frame = ttk.LabelFrame(right, text="Log")
        log_frame.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=10, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        bottom = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew")
        bottom.columnconfigure(1, weight=1)

        ttk.Label(bottom, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(bottom, mode="determinate", length=300)
        self.progress.grid(row=0, column=1, sticky="ew", padx=10)
        ttk.Label(bottom, textvariable=self.progress_var).grid(row=0, column=2, sticky="e")

    def _output_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        enabled_var: tk.BooleanVar,
        format_var: tk.StringVar,
    ) -> None:
        check = ttk.Checkbutton(parent, text=label, variable=enabled_var)
        check.grid(row=row, column=0, sticky="w", padx=(4, 8))
        combo = ttk.Combobox(parent, textvariable=format_var, values=["exr", "png"], width=8, state="readonly")
        combo.grid(row=row, column=1, sticky="w")
        help_key = f"output_{label.lower()}"
        self._help_button(parent, row=row, column=2, help_key=help_key)

        if label == "FG":
            self._tooltip(check, "Write straight foreground colors.")
            self._tooltip(combo, "FG output format. EXR is float/linear-friendly, PNG is 8-bit preview-friendly.")
        elif label == "Matte":
            self._tooltip(check, "Write alpha matte pass.")
            self._tooltip(combo, "Matte format. EXR preserves float precision; PNG is 8-bit.")
        elif label == "Comp":
            self._tooltip(check, "Write quick checkerboard composite preview.")
            self._tooltip(combo, "Comp format. PNG is typical for preview; EXR is optional.")
        elif label == "Processed":
            self._tooltip(check, "Write premultiplied RGBA output for editorial/compositing handoff.")
            self._tooltip(combo, "Processed format. EXR recommended for high-quality linear workflow.")

    def _tooltip(self, widget: tk.Widget, text: str) -> None:
        ToolTip(widget, text)

    def _help_button(self, parent: tk.Widget, row: int, column: int, help_key: str) -> None:
        btn = ttk.Button(parent, text="?", width=2, command=lambda key=help_key: self._show_help(key))
        btn.grid(row=row, column=column, padx=(6, 0), sticky="w")
        self._tooltip(btn, "Open detailed help")

    def _show_help(self, help_key: str) -> None:
        payload = self._help_texts.get(help_key)
        if payload is None:
            messagebox.showinfo("Help", "Help text is not available for this item yet.")
            return
        title, body = payload
        messagebox.showinfo(title, body)

    def _build_help_texts(self) -> dict[str, tuple[str, str]]:
        return {
            "what_is_corridorkey": (
                "What Is CorridorKey?",
                "CorridorKey is an AI green-screen keying tool.\n\n"
                "In simple terms:\n"
                "- You give it your footage.\n"
                "- You give it a rough hint of where the subject is (called an AlphaHint).\n"
                "- It produces clean keying outputs for compositing.\n\n"
                "Main outputs:\n"
                "- Matte: transparency/alpha channel\n"
                "- FG: foreground color (subject)\n"
                "- Comp: quick preview over checkerboard\n"
                "- Processed: premultiplied RGBA for fast editorial use\n\n"
                "Typical workflow:\n"
                "1) Import video.\n"
                "2) Generate AlphaHint (GVM or VideoMaMa).\n"
                "3) Run Inference to create final outputs.",
            ),
            "input_linear": (
                "Input Is Linear",
                "What it does:\n"
                "This tells CorridorKey how to interpret the color space of your input footage before processing.\n\n"
                "When to turn it ON:\n"
                "- Your source is already linear-light (common in some EXR/VFX pipelines).\n\n"
                "When to keep it OFF:\n"
                "- Most camera files and editorial formats (mp4, mov, h264/h265) that are usually viewed as sRGB/Rec.709.\n\n"
                "Why this matters:\n"
                "If this is set wrong, edge brightness, spill behavior, and final comp energy can look incorrect even if the matte shape seems good.",
            ),
            "despill_strength": (
                "Despill Strength",
                "What it does:\n"
                "Controls how much green contamination is removed from foreground colors.\n\n"
                "Typical values:\n"
                "- 0.0: no despill\n"
                "- 0.6 to 1.0: common production range\n"
                "- >1.0 equivalent behavior is not used here; 1.0 is max in this UI\n\n"
                "How to tune:\n"
                "- Increase if you see green glow around hair or skin edges.\n"
                "- Decrease if skin tones look gray/desaturated or highlights lose natural color.\n\n"
                "Beginner tip:\n"
                "Start at 1.0, then back down only if subjects start to look washed out.",
            ),
            "refiner_scale": (
                "Refiner Scale",
                "What it does:\n"
                "Scales the amount of edge/detail refinement added after the core prediction.\n\n"
                "Typical values:\n"
                "- 1.0: recommended default (training baseline)\n"
                "- 0.8 to 1.0: safer/cleaner edges\n"
                "- 1.1 to 1.4: stronger detail recovery in difficult shots\n\n"
                "How to tune:\n"
                "- Increase to recover wispy hair/fine detail.\n"
                "- Decrease if you notice edge chatter, ringing, or unstable micro-detail.",
            ),
            "auto_despeckle": (
                "Auto Despeckle",
                "What it does:\n"
                "Automatically removes tiny isolated matte islands that are likely noise.\n\n"
                "Great for:\n"
                "- Tracking marker dots\n"
                "- Sensor/compression junk\n"
                "- Small disconnected matte artifacts\n\n"
                "Turn OFF when:\n"
                "- You need to preserve very tiny intentional details (e.g., glitter, fine spray, particles).",
            ),
            "despeckle_size": (
                "Despeckle Size",
                "What it does:\n"
                "Sets the largest connected blob area that Auto Despeckle is allowed to remove.\n\n"
                "How to think about it:\n"
                "- Larger value = more aggressive cleanup\n"
                "- Smaller value = preserves more small features\n\n"
                "Practical workflow:\n"
                "1) Keep Auto Despeckle ON.\n"
                "2) Start around the default (400).\n"
                "3) Increase if random specks remain.\n"
                "4) Decrease if legitimate tiny details disappear.",
            ),
            "output_fg": (
                "FG Output",
                "What it is:\n"
                "Foreground color pass (subject colors without background).\n\n"
                "Who should use it:\n"
                "- Compositors who want full control in Nuke/Fusion/AE.\n\n"
                "Recommended format:\n"
                "- EXR for high-quality linear workflows.\n"
                "- PNG only for quick inspection or lightweight previews.",
            ),
            "output_matte": (
                "Matte Output",
                "What it is:\n"
                "Alpha matte pass (transparency shape).\n\n"
                "Why it matters:\n"
                "This is the primary control channel for compositing and edge blending.\n\n"
                "Recommended format:\n"
                "- EXR for best precision.\n"
                "- PNG if you need a quick 8-bit deliverable.",
            ),
            "output_comp": (
                "Comp Output",
                "What it is:\n"
                "Quick preview composite over a checkerboard.\n\n"
                "Best use:\n"
                "- Fast QC\n"
                "- Client/editor review\n\n"
                "Important:\n"
                "This is a convenience preview, not a replacement for professional final comp outputs.",
            ),
            "output_processed": (
                "Processed Output",
                "What it is:\n"
                "Premultiplied RGBA pass intended for quick drop-in use.\n\n"
                "Best use:\n"
                "- Editorial timelines\n"
                "- Fast handoff where a single ready pass is preferred\n\n"
                "Compositing note:\n"
                "For maximum flexibility and troubleshooting control, FG + Matte is still the most robust workflow.",
            ),
            "run_gvm": (
                "Run GVM",
                "What it does:\n"
                "Automatically creates an AlphaHint for RAW clips that only have input footage.\n\n"
                "What AlphaHint means:\n"
                "AlphaHint is a rough guide mask that helps the main model understand what should be foreground.\n\n"
                "When to use:\n"
                "- You want a fast, automatic first pass.\n"
                "- You do not have a manual mask hint prepared.\n\n"
                "Result:\n"
                "On success, clips move toward inference-ready status (READY) once hint coverage is valid.",
            ),
            "run_videomama": (
                "Run VideoMaMa",
                "What it does:\n"
                "Generates AlphaHint using your provided VideoMaMa mask hints for MASKED clips.\n\n"
                "What this means in plain language:\n"
                "You give a rough user mask first, then VideoMaMa turns that into a full alpha hint sequence.\n\n"
                "When to use:\n"
                "- You need more control over what should be foreground.\n"
                "- Automatic methods are including/excluding the wrong regions.\n\n"
                "Why users like it:\n"
                "It is often more art-directable than fully automatic hint generation.",
            ),
            "run_inference": (
                "Run Inference",
                "What it does:\n"
                "Runs the main CorridorKey model on selected READY/COMPLETE clips using current settings.\n\n"
                "Plain-language version:\n"
                "This is the step that creates your actual keying results from your footage + AlphaHint.\n\n"
                "Input requirement:\n"
                "- Clip must have valid footage and AlphaHint.\n\n"
                "Output location:\n"
                "- Each clip writes to its own Output folders (FG/Matte/Comp/Processed depending on what you enabled).",
            ),
            "unload_models": (
                "Unload Models",
                "What it does:\n"
                "Releases loaded model weights from GPU memory (VRAM).\n\n"
                "When to use:\n"
                "- You are done processing and want to free VRAM.\n"
                "- You are switching between heavy tasks.\n"
                "- You are troubleshooting out-of-memory issues.",
            ),
        }

    def choose_clips_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.clips_dir_var.get() or os.getcwd())
        if chosen:
            self.clips_dir_var.set(chosen)

    def import_video(self) -> None:
        if self._busy:
            return
        initial = projects_root()
        paths = filedialog.askopenfilenames(
            title="Import Video Clip(s)",
            initialdir=initial,
            filetypes=[("Supported Video", "*.mp4 *.mov *.avi *.mkv *.mxf *.webm *.m4v"), ("All Files", "*.*")],
        )
        if not paths:
            return

        imported = []
        failed = []
        for src in paths:
            try:
                project_dir = create_project(src, copy_source=True)
                imported.append(project_dir)
            except Exception as e:  # noqa: BLE001
                failed.append((src, str(e)))

        self.clips_dir_var.set(projects_root())
        self.scan_clips()

        if imported:
            self.append_log(f"Imported {len(imported)} clip(s) into Projects.")
        if failed:
            details = "\n".join([f"- {os.path.basename(path)}: {err}" for path, err in failed[:8]])
            messagebox.showwarning("Some imports failed", details)

    def _selected_clips(self) -> list[ClipEntry]:
        return [self._clips[i].clip for i in self.clip_list.curselection() if 0 <= i < len(self._clips)]

    def _set_busy(self, value: bool) -> None:
        self._busy = value
        state = "disabled" if value else "normal"
        for child in self.root.winfo_children():
            self._set_state_recursive(child, state)
        # Keep log scroll/text interactive while busy
        self.log_text.configure(state="normal")
        if value:
            self.status_var.set("Working...")

    def _set_state_recursive(self, widget: tk.Widget, state: str) -> None:
        if isinstance(widget, (ttk.Button, ttk.Entry, ttk.Combobox, ttk.Checkbutton, tk.Listbox)):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._set_state_recursive(child, state)

    def append_log(self, message: str) -> None:
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")

    def scan_clips(self) -> None:
        if self._busy:
            return
        path = self.clips_dir_var.get().strip()
        if not path:
            messagebox.showerror("Missing path", "Choose a clips directory first.")
            return
        if not os.path.isdir(path):
            messagebox.showerror("Invalid path", f"Directory not found:\n{path}")
            return

        try:
            clips = self.service.scan_clips(path, allow_standalone_videos=True)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Scan failed", str(e))
            return

        self._clips = []
        self.clip_list.delete(0, "end")
        for clip in clips:
            rel = os.path.relpath(clip.root_path, path)
            frames = clip.input_asset.frame_count if clip.input_asset else 0
            label = f"{clip.name} [{clip.state.value}] ({frames}f) - {rel}"
            self._clips.append(ClipView(clip=clip, label=label))
            self.clip_list.insert("end", label)

        self.status_var.set(f"Device: {self.device} | Clips: {len(clips)}")
        self.progress_var.set("Idle")
        self.progress["value"] = 0

    def _build_inference_params(self) -> InferenceParams:
        return InferenceParams(
            input_is_linear=bool(self.input_linear_var.get()),
            despill_strength=float(self.despill_var.get()),
            auto_despeckle=bool(self.auto_despeckle_var.get()),
            despeckle_size=int(self.despeckle_size_var.get()),
            refiner_scale=float(self.refiner_scale_var.get()),
        )

    def _build_output_config(self) -> OutputConfig:
        return OutputConfig(
            fg_enabled=bool(self.fg_enabled_var.get()),
            fg_format=self.fg_format_var.get(),
            matte_enabled=bool(self.matte_enabled_var.get()),
            matte_format=self.matte_format_var.get(),
            comp_enabled=bool(self.comp_enabled_var.get()),
            comp_format=self.comp_format_var.get(),
            processed_enabled=bool(self.proc_enabled_var.get()),
            processed_format=self.proc_format_var.get(),
        )

    def _start_worker(self, target) -> None:
        if self._busy:
            return
        self._ensure_device()
        self._set_busy(True)
        self._worker = threading.Thread(target=target, daemon=True)
        self._worker.start()

    def run_gvm(self) -> None:
        clips = [c for c in self._selected_clips() if c.state == ClipState.RAW]
        if not clips:
            messagebox.showinfo("No RAW clips", "Select at least one clip in RAW state.")
            return

        def _work() -> None:
            self._run_batch("GVM", clips, self._run_single_gvm)

        self._start_worker(_work)

    def run_videomama(self) -> None:
        clips = [c for c in self._selected_clips() if c.state == ClipState.MASKED]
        if not clips:
            messagebox.showinfo("No MASKED clips", "Select at least one clip in MASKED state.")
            return

        def _work() -> None:
            self._run_batch("VideoMaMa", clips, self._run_single_videomama)

        self._start_worker(_work)

    def run_inference(self) -> None:
        clips = [c for c in self._selected_clips() if c.state in {ClipState.READY, ClipState.COMPLETE}]
        if not clips:
            messagebox.showinfo("No ready clips", "Select one or more READY/COMPLETE clips.")
            return

        try:
            params = self._build_inference_params()
            output_cfg = self._build_output_config()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Invalid settings", str(e))
            return
        if not any(
            [
                output_cfg.fg_enabled,
                output_cfg.matte_enabled,
                output_cfg.comp_enabled,
                output_cfg.processed_enabled,
            ]
        ):
            messagebox.showerror("Invalid settings", "Enable at least one output type.")
            return

        def _work() -> None:
            self._run_batch(
                "Inference",
                clips,
                lambda clip: self._run_single_inference(clip, params, output_cfg),
            )

        self._start_worker(_work)

    def unload_models(self) -> None:
        try:
            self._ensure_device()
            self.service.unload_engines()
            self.append_log("All models unloaded.")
            self.status_var.set(f"Device: {self.device}")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Unload failed", str(e))

    def _run_batch(self, name: str, clips: list[ClipEntry], runner) -> None:
        self._events.put(("log", f"Starting {name} for {len(clips)} clip(s)"))
        failures = 0
        for idx, clip in enumerate(clips, start=1):
            self._events.put(("status", f"{name}: {clip.name} ({idx}/{len(clips)})"))
            try:
                runner(clip)
            except Exception as e:  # noqa: BLE001
                failures += 1
                self._events.put(("log", f"{name} failed for '{clip.name}': {e}"))
        if failures:
            self._events.put(("log", f"{name} complete with {failures} failure(s)."))
        else:
            self._events.put(("log", f"{name} complete."))
        self._events.put(("done", None))

    def _run_single_gvm(self, clip: ClipEntry) -> None:
        self.service.run_gvm(
            clip,
            on_progress=self._progress_callback,
            on_warning=self._warning_callback,
        )

    def _run_single_videomama(self, clip: ClipEntry) -> None:
        self.service.run_videomama(
            clip,
            on_progress=self._progress_callback,
            on_warning=self._warning_callback,
            on_status=lambda m: self._events.put(("log", f"{clip.name}: {m}")),
            chunk_size=50,
        )

    def _run_single_inference(self, clip: ClipEntry, params: InferenceParams, output_cfg: OutputConfig) -> None:
        self.service.run_inference(
            clip,
            params,
            on_progress=self._progress_callback,
            on_warning=self._warning_callback,
            output_config=output_cfg,
        )

    def _progress_callback(self, clip_name: str, current: int, total: int) -> None:
        self._events.put(("progress", (clip_name, current, total)))

    def _warning_callback(self, message: str) -> None:
        self._events.put(("log", f"Warning: {message}"))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self.append_log(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    clip_name, current, total = payload
                    pct = 0 if total <= 0 else (current / total) * 100.0
                    self.progress["value"] = max(0.0, min(100.0, pct))
                    self.progress_var.set(f"{clip_name}: {current}/{total}")
                elif kind == "error":
                    self.append_log(str(payload))
                    self.status_var.set("Error")
                    messagebox.showerror("Processing error", str(payload))
                elif kind == "done":
                    self._set_busy(False)
                    self.progress["value"] = 0
                    self.progress_var.set("Idle")
                    self.scan_clips()
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_events)


def main() -> None:
    root = tk.Tk()
    CorridorKeyUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
