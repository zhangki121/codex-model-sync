# -*- coding: utf-8 -*-
"""
Codex 模型同步器（GUI 桌面版）

把 Codex 桌面端所有旧任务(thread)锁定的 model / model_provider
批量改成当前全局配置(~/.codex/config.toml)里的默认值。

本应用是对 fix_codex_thread_models.py 的图形化封装，并新增：
  - 预览(只读) / 执行同步
  - 后台监控：检测到 Codex 退出、或配置变化时自动同步
  - 开机自启（监控模式）
  - 自动备份，一键打开备份目录

重要：修改数据库必须在 Codex 完全退出的状态下进行。
"""

import ctypes
import ctypes.wintypes
import datetime
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.scrolledtext as st
import tkinter.messagebox as mb
import tkinter.ttk as ttk
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

try:
    import pystray
    from PIL import Image as PILImage
    TRAY_AVAILABLE = True
except Exception:
    pystray = None
    PILImage = None
    TRAY_AVAILABLE = False


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
APP_NAME = "Codex模型同步器"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class SyncError(Exception):
    """同步过程中的业务错误。"""


# --------------------------------------------------------------------------
# 核心逻辑（移植自 fix_codex_thread_models.py）
# --------------------------------------------------------------------------

def warn(msg, log=print):
    log(f"[警告] {msg}")


def _own_exe_names():
    """返回本应用自身的进程名(用于排除自检)。"""
    names = set()
    try:
        names.add(os.path.basename(sys.executable).lower())
    except Exception:
        pass
    try:
        names.add(os.path.basename(sys.argv[0]).lower())
    except Exception:
        pass
    return {n for n in names if n}


def _is_codex_name(name: str) -> bool:
    return "codex" in name.lower()


def find_codex_processes_fast():
    """
    用 ctypes 直接枚举进程名，不启动任何子进程（比 tasklist 轻量得多）。
    成功返回进程名列表；失败返回 None（由调用方走兜底方案）。
    """
    try:
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.wintypes.DWORD),
                ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.wintypes.ULONG)),
                ("th32ModuleID", ctypes.wintypes.DWORD),
                ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [
            ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
        ]
        kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL
        kernel32.Process32FirstW.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32NextW.restype = ctypes.wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.wintypes.HANDLE(-1).value:
            return None

        own = _own_exe_names()
        my_pid = os.getpid()
        names = set()
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.th32ProcessID != my_pid:
                    name = entry.szExeFile or ""
                    if _is_codex_name(name) and name.lower() not in own:
                        names.add(name)
                ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)
        return sorted(names)
    except Exception:
        return None


def find_codex_processes_fallback() -> tuple:
    """返回 (进程名列表, 检查是否成功, 失败原因)。用 tasklist/PowerShell 兜底。"""
    names = []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        return names, False, str(exc)
    if out.returncode != 0:
        return names, False, (out.stderr.strip() or f"tasklist 退出码 {out.returncode}")
    if "error" in out.stdout.lower():
        return names, False, out.stdout.strip()[:200]
    own = _own_exe_names()
    for line in out.stdout.splitlines():
        try:
            name = line.split('","')[0].strip('"')
        except Exception:
            name = line.split(",")[0].strip('"')
        if _is_codex_name(name) and name.lower() not in own and name not in names:
            names.append(name)

    if not names:
        try:
            ps = subprocess.run(
                [
                    "powershell", "-NoProfile", "-Command",
                    "Get-Process | Where-Object { $_.ProcessName -like '*codex*' } "
                    "| Select-Object -ExpandProperty ProcessName",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
            for n in ps.stdout.splitlines():
                n = n.strip()
                if n and _is_codex_name(n) and n.lower() not in own and n not in names:
                    names.append(n)
        except Exception:
            pass
    return names, True, ""


def find_codex_processes() -> tuple:
    """优先用轻量 ctypes 枚举，失败时退回 tasklist。"""
    fast = find_codex_processes_fast()
    if fast is not None:
        return fast, True, ""
    return find_codex_processes_fallback()


def parse_config_value(text: str, key: str):
    """从 config.toml 文本中读取 model / model_provider 等顶层字符串值。"""
    m = re.search(
        rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]\s*(?:#.*)?$",
        text,
        re.MULTILINE,
    )
    return m.group(1) if m else None


def read_config_defaults() -> tuple:
    """从 config.toml 读取 (model, model_provider)，找不到则返回 (None, None)。"""
    cfg = CODEX_HOME / "config.toml"
    if not cfg.is_file():
        return None, None
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None, None
    return parse_config_value(text, "model"), parse_config_value(text, "model_provider")


def find_state_db() -> Path:
    """自动在 CODEX_HOME 下找带 threads 表的 state_*.sqlite。"""
    if not CODEX_HOME.is_dir():
        raise SyncError(f"找不到 Codex 配置目录: {CODEX_HOME}")
    candidates = sorted(CODEX_HOME.glob("state_*.sqlite"))
    if not candidates:
        raise SyncError(f"{CODEX_HOME} 下没有 state_*.sqlite")
    for db in candidates:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
            ).fetchone()
            con.close()
            if row:
                return db
        except Exception:
            continue
    raise SyncError("没有找到包含 threads 表的 state_*.sqlite")


def backup_files(files, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        p = Path(f)
        if p.exists():
            shutil.copy2(p, backup_dir / p.name)


def normalize_rollout_path(raw: str) -> Path:
    r"""rollout_path 可能是 \\?\C:\... 形式, 去掉 \\?\ 前缀。"""
    p = raw.strip()
    if p.startswith("\\\\?\\"):
        p = p[4:]
    return Path(p)


def sync_jsonl_model_provider(path: Path, provider: str, backup_dir: Path, log=print) -> int:
    """把会话 jsonl 里 session_meta 与 thread_settings_applied 的
    model_provider 标签改成新值。返回改动的行数。"""
    if not path.is_file():
        warn(f"会话文件不存在, 跳过: {path}", log)
        return 0
    backup_files([path], backup_dir)

    changed = 0
    lines_out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()

    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            lines_out.append(line)
            continue
        payload = obj.get("payload", {})
        touched = False
        if obj.get("type") == "session_meta" and isinstance(payload, dict):
            if payload.get("model_provider") not in (None, provider):
                payload["model_provider"] = provider
                touched = True
        elif (
            obj.get("type") == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "thread_settings_applied"
        ):
            ts = payload.get("thread_settings")
            if isinstance(ts, dict) and ts.get("model_provider") not in (None, provider):
                ts["model_provider"] = provider
                touched = True
        if touched:
            obj["payload"] = payload
            lines_out.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
            changed += 1
        else:
            lines_out.append(line)

    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines_out) + "\n")
    return changed


def project_roots_from_config() -> list:
    """从 config.toml 的 [projects.'路径'] 段收集可信项目根目录。"""
    roots = []
    cfg = CODEX_HOME / "config.toml"
    if not cfg.is_file():
        return roots
    text = cfg.read_text(encoding="utf-8", errors="replace")
    pat = re.compile(
        r"^\s*\[projects\.(?P<q>['\"]?)(?P<path>.+?)(?P=q)\]\s*$",
        re.MULTILINE,
    )
    for m in pat.finditer(text):
        p = m.group("path").strip()
        if p:
            roots.append(Path(p))
    return roots


def find_automation_files() -> list:
    """查找所有定时任务配置文件。"""
    candidates = [CODEX_HOME / "automations"]
    candidates += [r / ".codex" / "automations" for r in project_roots_from_config()]
    candidates.append(Path.cwd() / ".codex" / "automations")

    found = []
    for d in candidates:
        if d.is_dir():
            found.extend(d.rglob("automation.toml"))

    seen = set()
    uniq = []
    for f in found:
        key = os.path.normcase(os.path.abspath(f))
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return sorted(uniq, key=lambda p: os.path.normcase(os.path.abspath(p)))


def automation_model_change(path: Path, model: str, log=print):
    """读取 automation.toml 里顶层的 model, 返回 (旧模型, 新模型) 或 None。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        warn(f"无法读取 {path}: {exc}", log)
        return None
    m = re.search(r"^\s*model\s*=\s*['\"]([^'\"]+)['\"]\s*$", text, re.MULTILINE)
    if not m:
        warn(f"{path} 里没找到顶层 model 字段, 跳过", log)
        return None
    if m.group(1) == model:
        return None
    return m.group(1), model


def apply_automation_model(path: Path, model: str, backup_dir: Path) -> None:
    """把 automation.toml 顶层 model 改写为指定模型(改写前备份)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    new_text = re.sub(
        r"^\s*model\s*=\s*['\"]([^'\"]+)['\"]\s*$",
        f'model = "{model}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if new_text != text:
        backup_files([path], backup_dir)
        path.write_text(new_text, encoding="utf-8")


def run_sync(
    model=None,
    provider=None,
    *,
    only=None,
    old_model=None,
    sync_jsonl=False,
    skip_process_check=False,
    check_processes=True,
    state_db=None,
    backup_dir=None,
    dry_run=False,
    log=print,
):
    """
    核心同步逻辑。返回结果 dict；出错抛 SyncError。
    """
    # 1. 检查 Codex 是否已退出
    if check_processes and not skip_process_check:
        procs, check_ok, reason = find_codex_processes()
        if procs:
            raise SyncError(
                "检测到 Codex 相关进程仍在运行: " + ", ".join(procs)
                + "\n请先完全退出 Codex 桌面端(以及 codex 命令行进程)后再执行。\n"
                "确认没有其他会话占用时, 可勾选“跳过进程检查”强制运行。"
            )
        if not check_ok:
            raise SyncError(
                "无法确认 Codex 是否已退出(tasklist 无权限: " + reason + ")。\n"
                "请确认 Codex 已完全退出后, 勾选“跳过进程检查”再运行。"
            )

    # 2. 读取目标模型/供应商
    config_path = CODEX_HOME / "config.toml"
    if config_path.is_file():
        cfg = config_path.read_text(encoding="utf-8", errors="replace")
        model = model or parse_config_value(cfg, "model")
        provider = provider or parse_config_value(cfg, "model_provider")
    if not model:
        raise SyncError("无法确定目标模型: 请填写模型名, 或确认 config.toml 里有 model 字段")
    if not provider:
        raise SyncError("无法确定目标供应商: 请填写供应商, 或确认 config.toml 里有 model_provider 字段")

    # 3. 模型名可用性提醒(不阻断)
    catalog = CODEX_HOME / "cc-switch-model-catalog.json"
    if catalog.is_file():
        try:
            data = json.loads(catalog.read_text(encoding="utf-8", errors="replace"))
            slugs = {m.get("slug") for m in data.get("models", []) if m.get("slug")}
            if model not in slugs:
                warn(f"目标模型 {model!r} 不在 {catalog.name} 的模型目录里, 新任务可用但续聊可能被拒", log)
        except Exception:
            pass

    # 4. 定位数据库并备份
    db = Path(state_db) if state_db else find_state_db()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = (
        Path(backup_dir)
        if backup_dir
        else CODEX_HOME / "backups" / f"thread-model-update-{stamp}"
    )

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 10000")

    cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
    if "model" not in cols or "model_provider" not in cols:
        con.close()
        raise SyncError(f"{db} 的 threads 表缺少 model/model_provider 列, 请确认数据库路径")

    # 5. 确定要改哪些行
    where = ["model IS NOT NULL", "model != ?"]
    params = [model]
    if only:
        where.append(f"id IN ({','.join('?' * len(only))})")
        params.extend(only)
    if old_model:
        where.append("model = ?")
        params.append(old_model)

    rows = con.execute(
        f"SELECT id, title, model, model_provider, rollout_path FROM threads "
        f"WHERE {' AND '.join(where)} ORDER BY created_at_ms",
        params,
    ).fetchall()

    log(f"目标: model={model!r}, model_provider={provider!r}")
    log(f"数据库: {db}")
    if rows:
        log(f"将修改 {len(rows)} 个线程:")
        for r in rows:
            title = (r["title"] or "").replace("\n", " ")[:40]
            log(f"  {r['id']}  {r['model']} -> {model}  | {title}")
    else:
        log("线程: 没有需要修改的(目标模型已是当前锁定的模型, 或筛选条件无匹配)。")

    # 5b. 定时任务(自动化)也要一起看
    automation_files = find_automation_files()
    automation_changes = []
    for af in automation_files:
        change = automation_model_change(af, model, log)
        if change:
            automation_changes.append((af, change[0], change[1]))
    if automation_changes:
        log(f"\n将同步 {len(automation_changes)} 个定时任务的模型:")
        for af, old, new in automation_changes:
            log(f"  {af.name}  {old} -> {new}  ({af})")

    result = {
        "dry_run": dry_run,
        "db": str(db),
        "model": model,
        "provider": provider,
        "thread_ids": [r["id"] for r in rows],
        "threads_updated": 0,
        "automation_changes": automation_changes,
        "automations_updated": 0,
        "jsonl_changed": 0,
        "backup_dir": None,
    }

    if dry_run:
        log("\n[dry-run] 未做任何修改。确认无误后点击“执行同步”。")
        con.close()
        return result

    if not rows and not automation_changes:
        log("没有需要修改的内容。")
        con.close()
        return result

    # 6. 备份数据库(连同 WAL/SHM)
    backup_files([db, Path(str(db) + "-wal"), Path(str(db) + "-shm")], backup_dir)
    result["backup_dir"] = str(backup_dir)

    # 7. 执行修改
    ids = result["thread_ids"]
    placeholders = ",".join("?" * len(ids))
    cur = con.execute(
        f"UPDATE threads SET model = ?, model_provider = ? WHERE id IN ({placeholders})",
        [model, provider, *ids],
    )
    con.commit()
    result["threads_updated"] = cur.rowcount

    if sync_jsonl:
        for r in rows:
            path = normalize_rollout_path(r["rollout_path"] or "")
            result["jsonl_changed"] += sync_jsonl_model_provider(
                path, provider, backup_dir, log
            )

    con.close()

    log(f"\n完成: 已修改 {cur.rowcount} 个线程, 备份在 {backup_dir}")
    if automation_changes:
        for af, old, new in automation_changes:
            apply_automation_model(af, model, backup_dir)
        result["automations_updated"] = len(automation_changes)
        log(f"同步定时任务: 已修改 {len(automation_changes)} 个")
    if sync_jsonl:
        log(f"同步 jsonl 标签: 共改写 {result['jsonl_changed']} 行")
    log("现在可以重新打开 Codex, 旧任务续聊时会使用新模型。")
    log("如需恢复: 关闭 Codex 后把备份目录里的文件复制回 .codex 目录。")
    return result


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class CodexModelSyncApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.monitoring = False
        self.prev_running = False
        self.last_synced_fp = None
        self.last_backup_dir = None
        self._cfg_mtime = None
        self._last_auto = (None, None)

        root.title(APP_NAME)
        root.geometry("860x640")
        root.minsize(760, 560)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_window_icon()
        self.tray = None
        self.tray_hint_shown = False
        self.tray_var = tk.BooleanVar(value=TRAY_AVAILABLE)

        self.model_var = tk.StringVar()
        self.provider_var = tk.StringVar()
        self.jsonl_var = tk.BooleanVar(value=False)
        self.skip_var = tk.BooleanVar(value=False)
        self.autostart_var = tk.BooleanVar(value=False)

        self._setup_style()
        self._build_ui()
        self._read_config()
        self._update_info_labels()
        self._load_autostart_state()
        self.root.after(100, self._pump)
        self.root.after(2000, self._watch_config)
        self.root.bind("<Unmap>", self._on_unmap)
        self._create_tray()

    # ---------- 界面 ----------
    def _set_window_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        icon = os.path.join(base, "icon.ico")
        if os.path.isfile(icon):
            try:
                self.root.iconbitmap(icon)
            except Exception:
                pass

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        base_font = ("Microsoft YaHei UI", 9)
        style.configure(".", font=base_font)
        style.configure("TButton", padding=(14, 6))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 9, "bold"), padding=(18, 7))
        style.configure("TLabelframe", padding=(4, 4))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Subtitle.TLabel", foreground="#555555")
        style.configure("Hint.TLabel", foreground="#777777")

    def _get_config_mtime(self):
        cfg = CODEX_HOME / "config.toml"
        try:
            return cfg.stat().st_mtime if cfg.is_file() else None
        except Exception:
            return None

    def _watch_config(self):
        self.root.after(2000, self._watch_config)
        mtime = self._get_config_mtime()
        if self._cfg_mtime is None:
            self._cfg_mtime = mtime
            self._last_auto = (self.model_var.get().strip(), self.provider_var.get().strip())
            return
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        model, provider = read_config_defaults()
        cur_model = self.model_var.get().strip()
        cur_provider = self.provider_var.get().strip()
        last_model, last_provider = self._last_auto
        updated = []
        if model and (not cur_model or cur_model == last_model):
            self.model_var.set(model)
            updated.append(f"model={model}")
        if provider and (not cur_provider or cur_provider == last_provider):
            self.provider_var.set(provider)
            updated.append(f"provider={provider}")
        self._last_auto = (self.model_var.get().strip(), self.provider_var.get().strip())
        self._update_info_labels()
        if updated:
            self._log(f"检测到配置变化，已自动刷新: {', '.join(updated)}")
        else:
            self._log("检测到配置变化，但模型/供应商已被手动修改，未自动覆盖")

    def _build_ui(self):
        pad = {"padx": 14, "pady": 7}

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=14, pady=(14, 4))
        ttk.Label(header, text="Codex 模型同步器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="把旧对话的模型同步到当前配置，跟随 cc-switch 任意切换模型",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        frm_cfg = ttk.LabelFrame(self.root, text="目标配置（跟随 ~/.codex/config.toml）")
        frm_cfg.pack(fill="x", **pad)
        frm_cfg.columnconfigure(1, weight=1)

        ttk.Label(frm_cfg, text="模型").grid(row=0, column=0, sticky="e", padx=(10, 8), pady=(10, 4))
        ttk.Entry(frm_cfg, textvariable=self.model_var, width=40).grid(
            row=0, column=1, sticky="ew", padx=4, pady=(10, 4)
        )
        ttk.Label(frm_cfg, text="供应商").grid(row=1, column=0, sticky="e", padx=(10, 8), pady=4)
        ttk.Entry(frm_cfg, textvariable=self.provider_var, width=40).grid(
            row=1, column=1, sticky="ew", padx=4, pady=4
        )
        self.btn_read = ttk.Button(frm_cfg, text="重新读取", command=self._read_config, width=10)
        self.btn_read.grid(row=0, column=2, rowspan=2, padx=(12, 10), pady=6, sticky="ns")

        ttk.Label(
            frm_cfg,
            text="配置变化时自动刷新（每 2 秒检测），手动修改不会被覆盖",
            style="Hint.TLabel",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(2, 4))

        self.lbl_info = ttk.Label(frm_cfg, foreground="#555555", wraplength=800)
        self.lbl_info.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))

        frm_opts = ttk.LabelFrame(self.root, text="选项")
        frm_opts.pack(fill="x", **pad)
        frm_opts.columnconfigure(0, weight=1)
        ttk.Checkbutton(
            frm_opts,
            text="同时同步会话 jsonl 的 model_provider 标签",
            variable=self.jsonl_var,
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 3))
        ttk.Checkbutton(
            frm_opts,
            text="跳过 Codex 进程检查（仅在确认无其他会话占用时勾选）",
            variable=self.skip_var,
        ).grid(row=1, column=0, sticky="w", padx=12, pady=3)
        if TRAY_AVAILABLE:
            ttk.Checkbutton(
                frm_opts,
                text="关闭/最小化时最小化到系统托盘（后台运行）",
                variable=self.tray_var,
            ).grid(row=2, column=0, sticky="w", padx=12, pady=(3, 8))

        frm_auto = ttk.LabelFrame(self.root, text="自动同步")
        frm_auto.pack(fill="x", **pad)
        self.btn_monitor = ttk.Button(frm_auto, text="开始监控", command=self._toggle_monitor, width=12)
        self.btn_monitor.pack(side="left", padx=12, pady=8)
        ttk.Checkbutton(
            frm_auto,
            text="开机自启（启动后进入监控模式）",
            variable=self.autostart_var,
            command=self._set_autostart,
        ).pack(side="left", padx=16, pady=8)
        self.lbl_monitor = ttk.Label(frm_auto, foreground="#555555")
        self.lbl_monitor.pack(side="left", padx=10, pady=8)

        frm_actions = ttk.Frame(self.root)
        frm_actions.pack(fill="x", **pad)
        self.btn_preview = ttk.Button(frm_actions, text="预览修改", command=self._do_preview, width=16)
        self.btn_preview.pack(side="left", padx=(0, 8), pady=2)
        self.btn_sync = ttk.Button(
            frm_actions, text="执行同步", command=self._do_sync, width=16, style="Primary.TButton"
        )
        self.btn_sync.pack(side="left", padx=8, pady=2)
        self.btn_backup = ttk.Button(
            frm_actions, text="打开备份目录", command=self._open_backup, width=16, state="disabled"
        )
        self.btn_backup.pack(side="left", padx=8, pady=2)

        self.txt_log = st.ScrolledText(
            self.root, height=13, state="disabled", font=("Consolas", 9),
            wrap="word",
            relief="solid", borderwidth=1,
        )
        self.txt_log.pack(fill="both", expand=True, padx=14, pady=(2, 6))
        self.txt_log.tag_configure("err", foreground="#b3261e")
        self.txt_log.tag_configure("ok", foreground="#1e7a3c")
        self.txt_log.tag_configure("warn", foreground="#9a6700")

        self.lbl_status = ttk.Label(self.root, anchor="w", padding=(2, 2))
        self.lbl_status.pack(fill="x", padx=14, pady=(0, 8))

    # ---------- 配置读取 ----------
    def _read_config(self):
        model, provider = read_config_defaults()
        if model:
            self.model_var.set(model)
        if provider:
            self.provider_var.set(provider)
        self._last_auto = (self.model_var.get().strip(), self.provider_var.get().strip())
        self._cfg_mtime = self._get_config_mtime()
        self._update_info_labels()

    def _update_info_labels(self):
        cfg = CODEX_HOME / "config.toml"
        try:
            db = find_state_db()
        except SyncError:
            db = None
        cfg_txt = cfg if cfg.is_file() else f"{cfg}（不存在）"
        db_txt = db or "未找到 state_*.sqlite"
        self.lbl_info.config(text=f"配置文件: {cfg_txt}    数据库: {db_txt}")

    # ---------- 日志 ----------
    def _log(self, msg, tag=None):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", msg + "\n", tag or ())
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _log_err(self, msg):
        self._log(f"[错误] {msg}", "err")

    def _set_status(self, text):
        self.lbl_status.config(text=text)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_preview.config(state=state)
        self.btn_sync.config(state=state)
        self.btn_read.config(state=state)
        self.btn_monitor.config(state=state)

    # ---------- 预览 / 执行 ----------
    def _do_preview(self):
        if self.busy:
            return
        self._log("========== 预览修改（只读，不会改动任何文件） ==========")
        self._set_busy(True)
        threading.Thread(target=self._preview_worker, daemon=True).start()

    def _preview_worker(self):
        try:
            res = run_sync(
                self.model_var.get().strip() or None,
                self.provider_var.get().strip() or None,
                dry_run=True,
                check_processes=False,
                log=lambda m: self.q.put(("log", m)),
            )
            self.q.put(("done", res, None))
        except Exception as exc:
            self.q.put(("done", None, str(exc)))

    def _do_sync(self):
        if self.busy:
            return
        if not self.skip_var.get():
            procs, ok, reason = find_codex_processes()
            if procs:
                mb.showwarning(
                    "Codex 正在运行",
                    "检测到 Codex 相关进程仍在运行:\n"
                    + ", ".join(procs)
                    + "\n\n请先完全退出 Codex 桌面端后再执行同步。\n"
                    "若确认没有其他会话占用，可勾选“跳过进程检查”。",
                )
                return
        self._log("========== 执行同步 ==========")
        self._set_busy(True)
        threading.Thread(target=self._sync_worker, daemon=True).start()

    def _sync_worker(self):
        try:
            res = run_sync(
                self.model_var.get().strip() or None,
                self.provider_var.get().strip() or None,
                sync_jsonl=self.jsonl_var.get(),
                skip_process_check=self.skip_var.get(),
                check_processes=True,
                log=lambda m: self.q.put(("log", m)),
            )
            self.q.put(("done", res, None))
        except Exception as exc:
            self.q.put(("done", None, str(exc)))

    def _open_backup(self):
        if self.last_backup_dir and os.path.isdir(self.last_backup_dir):
            os.startfile(self.last_backup_dir)

    # ---------- 自动监控 ----------
    def _toggle_monitor(self):
        if self.monitoring:
            self.monitoring = False
            self.btn_monitor.config(text="开始监控")
            self.lbl_monitor.config(text="监控已停止")
            self._set_status("监控已停止")
            self._log("自动监控已停止。")
        else:
            self.monitoring = True
            self.prev_running = bool(find_codex_processes()[0])
            self.last_synced_fp = None
            self.btn_monitor.config(text="停止监控")
            self.lbl_monitor.config(text="监控中：等待 Codex 退出或配置变化...")
            self._set_status("自动监控运行中（每 2 秒检测一次）")
            self._log("自动监控已启动：检测到 Codex 退出或模型/供应商变化时将自动同步。")
            self.root.after(2000, self._tick)

    def _tick(self):
        if not self.monitoring:
            return
        self.root.after(2000, self._tick)
        if self.busy:
            return

        procs, ok, reason = find_codex_processes()
        running = bool(procs)
        fp = read_config_defaults()

        if running:
            self.prev_running = True
            self.lbl_monitor.config(text="监控中：Codex 正在运行（退出后自动同步）")
            return

        # Codex 未运行
        if self.prev_running:
            # 刚退出：稍等文件释放后同步一次
            self.prev_running = False
            self.lbl_monitor.config(text="检测到 Codex 已退出，3 秒后自动同步...")
            self._log("检测到 Codex 已退出，3 秒后自动同步。")
            self.root.after(3000, lambda: self._auto_sync(fp, force=True))
            return

        # 一直未运行：配置变化时同步
        if fp is not None and fp != self.last_synced_fp:
            self._auto_sync(fp, force=False)

    def _auto_sync(self, fp, force):
        if not self.monitoring or self.busy:
            return
        if fp is None or fp[0] is None or fp[1] is None:
            self._log_err("自动同步：config.toml 里没读到 model / model_provider，已跳过。")
            self.last_synced_fp = fp
            return
        if not force and fp == self.last_synced_fp:
            return
        self._set_busy(True)
        self._log(f"自动同步: 目标 model={fp[0]!r}, provider={fp[1]!r}")
        threading.Thread(target=lambda: self._auto_sync_worker(fp), daemon=True).start()

    def _auto_sync_worker(self, fp):
        try:
            # 写库前再确认一次 Codex 确实没开（避免用户刚好打开）
            procs, _, _ = find_codex_processes()
            if procs:
                self.q.put(("log", f"自动同步取消：Codex 已重新打开（{', '.join(procs)}）。"))
                self.q.put(("auto_aborted", None, None))
                return
            res = run_sync(
                fp[0], fp[1],
                sync_jsonl=self.jsonl_var.get(),
                skip_process_check=True,
                check_processes=False,
                log=lambda m: self.q.put(("log", m)),
            )
            self.q.put(("auto_done", res, None))
        except Exception as exc:
            self.q.put(("auto_done", None, str(exc)))

    # ---------- 开机自启 ----------
    def _load_autostart_state(self):
        if winreg is None:
            return
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ,
            )
            try:
                val, _ = winreg.QueryValueEx(key, APP_NAME)
                self.autostart_var.set(val != "")
            except FileNotFoundError:
                pass
            winreg.CloseKey(key)
        except OSError:
            pass

    def _set_autostart(self):
        if winreg is None:
            self._log_err("当前环境不支持写入开机自启注册表。")
            self.autostart_var.set(False)
            return
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE,
            )
            if self.autostart_var.get():
                exe = Path(sys.executable).resolve()
                cmd = f'"{exe}" --monitor'
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                self._log(f"已设置开机自启: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                    self._log("已取消开机自启。")
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except OSError as exc:
            self._log_err(f"写入开机自启失败: {exc}")
            self.autostart_var.set(False)

    # ---------- 消息泵 ----------
    def _pump(self):
        try:
            while True:
                kind, payload, err = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "done":
                    self._set_busy(False)
                    self._finish_manual(payload, err)
                elif kind == "auto_done":
                    self._set_busy(False)
                    self._finish_auto(payload, err)
                elif kind == "auto_aborted":
                    self._set_busy(False)
                    self.lbl_monitor.config(text="监控中：等待 Codex 退出或配置变化...")
                    self._set_status("自动同步已取消（Codex 重新打开）")
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _finish_manual(self, res, err):
        if err:
            self._log_err(err)
            self._set_status("执行失败")
            return
        self._finish_result(res)
        if not res["dry_run"]:
            self._set_status("同步完成")
        else:
            self._set_status("预览完成（未做任何修改）")

    def _finish_auto(self, res, err):
        if err:
            self._log_err(err)
            self._set_status("自动同步失败")
            self.lbl_monitor.config(text="监控中（上次自动同步失败，稍后重试）")
            return
        self.last_synced_fp = (res["model"], res["provider"])
        self._finish_result(res)
        self._set_status("自动同步完成")
        self.lbl_monitor.config(text="监控中：等待 Codex 退出或配置变化...")
        if self.root.state() == "iconic":
            self.root.deiconify()
            self.root.lift()
        self._log("自动同步完成，下次打开 Codex 时旧对话将使用新模型。")

    def _finish_result(self, res):
        if res.get("backup_dir"):
            self.last_backup_dir = res["backup_dir"]
            self.btn_backup.config(state="normal")
        changed = res.get("threads_updated", 0)
        if res["dry_run"]:
            n = len(res.get("thread_ids", []))
            self._log(f"[预览] 有 {n} 个线程将被修改。")
        else:
            self._log(f"[结果] 已修改 {changed} 个线程"
                      + (f"，{res.get('automations_updated', 0)} 个定时任务" if res.get("automations_updated") else "")
                      + (f"，{res.get('jsonl_changed')} 行 jsonl" if res.get("jsonl_changed") else "")
                      + (f"，备份目录: {res.get('backup_dir')}" if res.get("backup_dir") else "")
                      + "。", "ok")

    # ---------- 系统托盘 ----------
    def _resource_icon_path(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        for cand in (
            os.path.join(base, "icon.ico"),
            os.path.join(base, "assets", "icon.ico"),
        ):
            if os.path.isfile(cand):
                return cand
        return None

    def _create_tray(self):
        if self.tray is not None or not TRAY_AVAILABLE:
            return
        try:
            path = self._resource_icon_path()
            if path:
                image = PILImage.open(path)
            else:
                image = PILImage.new("RGBA", (64, 64), (0, 0, 0, 0))
            menu = pystray.Menu(
                pystray.MenuItem(
                    "显示主界面",
                    lambda icon, item: self.root.after(0, self._show_window),
                    default=True,
                ),
                pystray.MenuItem(
                    "立即同步",
                    lambda icon, item: self.root.after(0, self._do_sync),
                ),
                pystray.MenuItem(
                    "自动同步监控",
                    self._tray_toggle_monitor,
                    checked=lambda item: self.monitoring,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "退出",
                    lambda icon, item: self.root.after(0, self._quit_app),
                ),
            )
            self.tray = pystray.Icon(APP_NAME, image, APP_NAME, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception as exc:
            self._log_err(f"托盘初始化失败: {exc}")
            self.tray = None

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_to_tray(self):
        if self.tray is None:
            return
        self.root.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            try:
                self.tray.notify("应用仍在后台运行。右键托盘图标可显示主界面或退出。", APP_NAME)
            except Exception:
                pass

    def _tray_toggle_monitor(self, icon, item):
        self.root.after(0, self._toggle_monitor)

    def _quit_app(self):
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        self.root.destroy()

    def _on_unmap(self, event):
        if self.tray_var.get() and self.tray is not None and self.root.state() == "iconic":
            self.root.after(60, self._hide_to_tray)

    # ---------- 关闭 ----------
    def _on_close(self):
        if self.tray_var.get() and self.tray is not None:
            self._hide_to_tray()
            return
        if self.monitoring:
            if not mb.askyesno("确认退出", "自动同步监控正在运行，退出后就不再自动同步。\n确定要退出吗？"):
                return
        self.root.destroy()


def _main():
    # 单实例互斥，避免重复启动导致重复同步
    if not any(
        f in sys.argv
        for f in ("--self-test", "--smoke-gui", "--import-test", "--run-test")
    ):
        try:
            mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\CodexModelSyncApp")
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                try:
                    Path(os.environ.get("TEMP", ".")).joinpath(
                        "codex-sync-mutex.txt"
                    ).write_text("MUTEX_EXISTS", encoding="utf-8")
                except Exception:
                    pass
                mb.showinfo(APP_NAME, "程序已在运行中。")
                return
        except Exception:
            pass

    if "--import-test" in sys.argv:
        out = None
        for a in sys.argv:
            if a.startswith("--out="):
                out = a[len("--out="):]
        lines = []
        try:
            import pystray
            import PIL
            lines.append("IMPORT_OK")
        except Exception as exc:
            lines.append(f"IMPORT_ERROR: {exc}")
        if out:
            Path(out).write_text("\n".join(lines), encoding="utf-8")
        sys.exit(0 if lines[-1] == "IMPORT_OK" else 1)

    if "--self-test" in sys.argv:
        out = None
        for a in sys.argv:
            if a.startswith("--out="):
                out = a[len("--out="):]
        lines = []
        try:
            res = run_sync(None, None, dry_run=True, check_processes=False,
                           log=lambda m: lines.append(m))
            lines.append("SELFTEST_OK")
        except Exception as exc:
            lines.append(f"SELFTEST_ERROR: {exc}")
        if out:
            Path(out).write_text("\n".join(lines), encoding="utf-8")
        sys.exit(0 if lines[-1] == "SELFTEST_OK" else 1)

    root = tk.Tk()
    app = CodexModelSyncApp(root)

    if "--smoke-gui" in sys.argv:
        root.withdraw()
        root.update()
        root.destroy()
        sys.exit(0)

    if "--run-test" in sys.argv:
        def _finish_run_test():
            try:
                Path(os.environ.get("TEMP", ".")).joinpath("codex-sync-run-ok.txt").write_text(
                    "RUN_OK", encoding="utf-8"
                )
            except Exception:
                pass
            root.destroy()
        root.after(8000, _finish_run_test)

    if "--monitor" in sys.argv:
        app.monitoring = True
        app.prev_running = bool(find_codex_processes()[0])
        app.last_synced_fp = None
        app.btn_monitor.config(text="停止监控")
        app.lbl_monitor.config(text="监控中：等待 Codex 退出或配置变化...")
        app._set_status("自动监控运行中（每 2 秒检测一次）")
        app._log("开机自启模式：自动监控已启动。")
        app.root.after(2000, app._tick)
        if app.tray is not None:
            app.root.withdraw()
            try:
                app.tray.notify("已在后台运行，自动同步监控已启动。", APP_NAME)
            except Exception:
                pass
        else:
            root.iconify()

    root.mainloop()


def main():
    import traceback
    try:
        _main()
    except Exception:
        try:
            Path(os.environ.get("TEMP", ".")).joinpath("codex-sync-crash.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
