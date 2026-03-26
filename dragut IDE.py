# ============================================================
# Advanced IDE (ESP32-friendly)
# Version : 4.0d15
# Build   : 2026-02-26 10:38 (+03:00 Europe/Istanbul)
# ============================================================

import sys
import os
import re
import json
import time
import socket
import threading
import queue
from dataclasses import dataclass, asdict
from typing import Optional, List, Tuple

import serial
import serial.tools.list_ports

from PyQt6.QtCore import QTimer, Qt, QRect, QSize, QThread, pyqtSignal
from PyQt6.QtGui import (
    QLinearGradient, QPixmap, QColor, QFont, QPainter,
    QTextCharFormat, QSyntaxHighlighter, QAction, QTextCursor
)
from PyQt6.QtWidgets import (
    QGraphicsBlurEffect, QSplashScreen,
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QTextEdit, QPushButton, QComboBox, QLabel, QLineEdit,
    QFileDialog, QMessageBox, QCheckBox, QDialog, QFormLayout,
    QDialogButtonBox, QStatusBar, QTabWidget, QProgressBar, QFrame,
    QSplitter, QMenu, QStyle, QToolButton
)

APP_NAME = "Advanced IDE"
VERSION  = "4.0d14"
BUILD    = "2026-02-26 10:38 (+03:00 Europe/Istanbul)"

ANSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


# =========================
# Settings (JSON)
# =========================
def _default_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".advanced_ide_v4_config.json")


@dataclass
class Settings:
    default_mode: str        = "Serial"
    default_baud: int        = 115200
    default_tcp_ip: str      = "127.0.0.1"
    default_tcp_port: int    = 9000
    send_mode: str           = "ASCII"
    eol: str                 = "CRLF"
    custom_eol: str          = r"\n"
    per_line_upload: bool    = True
    delay_enabled: bool      = False
    delay_ms: int            = 0
    ack_enabled: bool        = False
    ack_text: str            = "ok"
    ack_timeout_ms: int      = 1000
    ack_eol: str             = "CRLF"
    ack_custom_eol: str      = r"\n"
    terminal_view: str       = "Text"
    terminal_timestamp: bool = False
    terminal_autoscroll: bool= True
    strip_ansi: bool         = True
    normalize_crlf_for_display: bool = True
    terminal_max_lines: int  = 6000
    terminal_prune_lines: int= 1200
    esp32_toggle_dtr_rts_on_connect: bool = False
    esp32_reset_on_monitor_start: bool    = True
    esp32_reset_before_upload: bool       = False
    esp32_reset_method: str  = "DTR/RTS"
    default_language: str    = "BASIC"
    custom_keywords: str     = ""
    theme: str               = "Dark"
    send_new_before_upload: bool = True   # NEW: auto-send NEW before upload


class SettingsManager:
    def __init__(self, path: Optional[str] = None):
        self.path = path or _default_config_path()
        self.settings = Settings()

    def load(self) -> Settings:
        if not os.path.isfile(self.path):
            return self.settings
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = asdict(Settings())
            base.update({k: v for k, v in data.items() if k in base})
            self.settings = Settings(**base)
        except Exception:
            self.settings = Settings()
        return self.settings

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(asdict(self.settings), f, indent=2, ensure_ascii=False)
        except Exception:
            pass


# =========================
# HEX dump
# =========================
def hex_dump_lines(data: bytes, offset_start: int) -> Tuple[List[str], int]:
    lines: List[str] = []
    off = offset_start
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part  = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{off:08X}  {hex_part:<47}  |{ascii_part}|")
        off += len(chunk)
    return lines, off


# =========================
# TerminalView
# =========================
class TerminalView(QPlainTextEdit):
    def __init__(self, color: Optional[str] = None):
        super().__init__()
        self.setReadOnly(True)
        self.setFont(QFont("Consolas", 10))
        if color:
            self.setStyleSheet(f"color: {color};")
        self._user_scroll_lock = False
        self._track_scroll = True
        self.verticalScrollBar().valueChanged.connect(self._on_scrollbar_changed)

    def set_max_lines(self, n: int):
        self.document().setMaximumBlockCount(max(200, int(n)))

    def _on_scrollbar_changed(self, _v: int):
        if not self._track_scroll:
            return
        sb = self.verticalScrollBar()
        self._user_scroll_lock = (sb.value() < sb.maximum())

    def smart_should_autoscroll(self, setting_autoscroll: bool) -> bool:
        return bool(setting_autoscroll) and (not self._user_scroll_lock)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy_act   = menu.addAction("Copy")
        select_act = menu.addAction("Select All")
        menu.addSeparator()
        clear_act  = menu.addAction("Clear")
        save_act   = menu.addAction("Save Log...")
        act = menu.exec(event.globalPos())
        if act == copy_act:
            self.copy()
        elif act == select_act:
            self.selectAll()
        elif act == clear_act:
            self.clear()
        elif act == save_act:
            path, _ = QFileDialog.getSaveFileName(self, "Save Log", "", "Text (*.txt)")
            if path:
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(self.toPlainText())
                except Exception:
                    pass


# =========================
# Editor + line numbers
# =========================
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self._lineNumberArea = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self.setFont(QFont("Consolas", 11))
        self._update_line_number_area_width(0)
        self._highlight_current_line()

    def line_number_area_width(self):
        digits = len(str(max(1, self.blockCount())))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_line_number_area_width(self, _):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect, dy):
        if dy:
            self._lineNumberArea.scroll(0, dy)
        else:
            self._lineNumberArea.update(0, rect.y(), self._lineNumberArea.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._lineNumberArea.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height()))

    def line_number_area_paint_event(self, event):
        painter = QPainter(self._lineNumberArea)
        painter.fillRect(event.rect(), QColor("#2d2d30"))
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top    = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor("#b0b0b0"))
                painter.drawText(0, top, self._lineNumberArea.width() - 6,
                                 self.fontMetrics().height(),
                                 Qt.AlignmentFlag.AlignRight, str(block_number + 1))
            block = block.next()
            top   = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def _highlight_current_line(self):
        if self.isReadOnly():
            self.setExtraSelections([])
            return
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor("#2a2d2e"))
        sel.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])


# =========================
# Syntax highlighter
# =========================
class MultiLangHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.lang = "BASIC"
        self.custom_keywords: List[str] = []
        self.rules = []
        self._init_formats()
        self.set_language("BASIC", "")

    def _init_formats(self):
        def fmt(color, bold=False, italic=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:   f.setFontWeight(QFont.Weight.Bold)
            if italic: f.setFontItalic(True)
            return f
        self.f_keyword   = fmt("#569cd6", bold=True)
        self.f_type      = fmt("#4ec9b0", bold=True)
        self.f_number    = fmt("#b5cea8")
        self.f_string    = fmt("#ce9178")
        self.f_comment   = fmt("#6a9955", italic=True)
        self.f_label     = fmt("#c586c0", bold=True)
        self.f_directive = fmt("#dcdcaa", bold=True)
        self.f_preproc   = fmt("#c8c8c8", bold=True)

    def set_language(self, lang: str, custom_keywords_text: str):
        self.lang = lang
        self.custom_keywords = [p.strip() for p in re.split(r"[,\s]+", (custom_keywords_text or "").strip()) if p.strip()]
        self.rules = self._make_rules(lang)
        self.rehighlight()

    def _make_rules(self, lang):
        rules = [
            (re.compile(r'"([^"\\]|\\.)*"'), self.f_string),
            (re.compile(r"'([^'\\]|\\.)*'"), self.f_string),
            (re.compile(r"\b\d+(\.\d+)?\b"),  self.f_number),
            (re.compile(r"\b0x[0-9a-fA-F]+\b"), self.f_number),
        ]
        if lang == "BASIC":
            rules.append((re.compile(r"\bREM\b.*$", re.IGNORECASE), self.f_comment))
            rules.append((re.compile(r"'.*$"), self.f_comment))
            for k in ["PRINT","INPUT","IF","THEN","ELSE","FOR","TO","STEP","NEXT",
                      "GOTO","GOSUB","RETURN","DIM","LET","END","STOP","RUN","LIST",
                      "NEW","CLS","RANDOMIZE","AND","OR","NOT","MOD","XOR","PREC",
                      "WHILE","WEND","SWAP","RESTORE","READ","DATA","MILLIS"]:
                rules.append((re.compile(rf"\b{k}\b", re.IGNORECASE), self.f_keyword))
        elif lang == "ASM":
            rules.append((re.compile(r";.*$"), self.f_comment))
            rules.append((re.compile(r"^\s*[A-Za-z_][\w.]*\s*:"), self.f_label))
            for k in ["LD","JP","JR","CALL","RET","PUSH","POP","ADD","ADC","SUB",
                      "SBC","AND","OR","XOR","CP","INC","DEC","BIT","SET","RES",
                      "RL","RLC","RR","RRC","SLA","SRA","SRL","IN","OUT","HALT","NOP"]:
                rules.append((re.compile(rf"\b{k}\b", re.IGNORECASE), self.f_keyword))
        elif lang == "C":
            rules.append((re.compile(r"//.*$"), self.f_comment))
            for k in ["if","else","for","while","do","switch","case","break",
                      "continue","return","struct","union","enum","typedef",
                      "static","extern","const","volatile","sizeof","goto"]:
                rules.append((re.compile(rf"\b{k}\b"), self.f_keyword))
            for t in ["void","char","short","int","long","float","double",
                      "uint8_t","uint16_t","uint32_t","int8_t","int16_t","int32_t","size_t"]:
                rules.append((re.compile(rf"\b{t}\b"), self.f_type))
            for p in ["#include","#define","#ifdef","#ifndef","#endif","#if","#else","#pragma"]:
                rules.append((re.compile(rf"^\s*{re.escape(p)}.*$", re.MULTILINE), self.f_preproc))
        elif lang == "Custom":
            rules.append((re.compile(r"//.*$"), self.f_comment))
            rules.append((re.compile(r";.*$"),  self.f_comment))
            for k in self.custom_keywords:
                rules.append((re.compile(rf"\b{re.escape(k)}\b", re.IGNORECASE), self.f_keyword))
        return rules

    def highlightBlock(self, text):
        for pattern, form in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), form)


# =========================
# Config dialog
# =========================
class ConfigDialog(QDialog):
    def __init__(self, parent, settings: Settings):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} v{VERSION} — Settings")
        self.settings = settings
        layout = QFormLayout(self)

        self.send_mode    = QComboBox(); self.send_mode.addItems(["ASCII","HEX"])
        self.eol          = QComboBox(); self.eol.addItems(["None","CR","LF","CRLF","Custom"])
        self.custom_eol   = QLineEdit()
        self.delay_enabled = QCheckBox("Enable delay between lines")
        self.delay_ms     = QLineEdit()
        self.ack_enabled  = QCheckBox("Wait for ACK")
        self.ack_text     = QLineEdit()
        self.ack_timeout  = QLineEdit()
        self.ack_eol      = QComboBox(); self.ack_eol.addItems(["None","CR","LF","CRLF","Custom"])
        self.ack_custom_eol = QLineEdit()
        self.send_new_before_upload = QCheckBox("Send NEW before upload (clears old program)")
        self.terminal_view = QComboBox(); self.terminal_view.addItems(["Text","Hex","Both"])
        self.terminal_timestamp  = QCheckBox("Timestamp")
        self.terminal_autoscroll = QCheckBox("Auto scroll")
        self.strip_ansi   = QCheckBox("Strip ANSI")
        self.norm_crlf    = QCheckBox("Normalize CR/LF for display")
        self.term_max_lines   = QLineEdit()
        self.default_language = QComboBox(); self.default_language.addItems(["BASIC","ASM","C","Custom"])
        self.custom_keywords  = QLineEdit()
        self.esp32_toggle_dtr = QCheckBox("ESP32: Toggle DTR/RTS on connect")
        self.esp32_reset_mon  = QCheckBox("ESP32: Reset on monitor start")
        self.esp32_reset_up   = QCheckBox("ESP32: Reset before upload")

        layout.addRow("Send Mode:", self.send_mode)
        layout.addRow("Line Ending:", self.eol)
        layout.addRow("Custom EOL:", self.custom_eol)
        layout.addRow(self.delay_enabled)
        layout.addRow("Delay (ms):", self.delay_ms)
        layout.addRow(self.ack_enabled)
        layout.addRow("ACK Text:", self.ack_text)
        layout.addRow("ACK Timeout (ms):", self.ack_timeout)
        layout.addRow("ACK EOL:", self.ack_eol)
        layout.addRow("ACK Custom EOL:", self.ack_custom_eol)
        layout.addRow(self.send_new_before_upload)
        layout.addRow("Text tab view:", self.terminal_view)
        layout.addRow(self.terminal_timestamp)
        layout.addRow(self.terminal_autoscroll)
        layout.addRow(self.strip_ansi)
        layout.addRow(self.norm_crlf)
        layout.addRow("Terminal max lines:", self.term_max_lines)
        layout.addRow("Default Language:", self.default_language)
        layout.addRow("Custom keywords:", self.custom_keywords)
        layout.addRow(self.esp32_toggle_dtr)
        layout.addRow(self.esp32_reset_mon)
        layout.addRow(self.esp32_reset_up)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

        self._load()
        self.eol.currentIndexChanged.connect(self._toggle_custom)
        self.ack_eol.currentIndexChanged.connect(self._toggle_custom)
        self._toggle_custom()

    def _toggle_custom(self):
        self.custom_eol.setEnabled(self.eol.currentText() == "Custom")
        self.ack_custom_eol.setEnabled(self.ack_eol.currentText() == "Custom")

    def _load(self):
        s = self.settings
        self.send_mode.setCurrentText(s.send_mode)
        self.eol.setCurrentText(s.eol)
        self.custom_eol.setText(s.custom_eol)
        self.delay_enabled.setChecked(s.delay_enabled)
        self.delay_ms.setText(str(s.delay_ms))
        self.ack_enabled.setChecked(s.ack_enabled)
        self.ack_text.setText(s.ack_text)
        self.ack_timeout.setText(str(s.ack_timeout_ms))
        self.ack_eol.setCurrentText(s.ack_eol)
        self.ack_custom_eol.setText(s.ack_custom_eol)
        self.send_new_before_upload.setChecked(s.send_new_before_upload)
        self.terminal_view.setCurrentText(s.terminal_view)
        self.terminal_timestamp.setChecked(s.terminal_timestamp)
        self.terminal_autoscroll.setChecked(s.terminal_autoscroll)
        self.strip_ansi.setChecked(s.strip_ansi)
        self.norm_crlf.setChecked(s.normalize_crlf_for_display)
        self.term_max_lines.setText(str(s.terminal_max_lines))
        self.default_language.setCurrentText(s.default_language)
        self.custom_keywords.setText(s.custom_keywords)
        self.esp32_toggle_dtr.setChecked(s.esp32_toggle_dtr_rts_on_connect)
        self.esp32_reset_mon.setChecked(s.esp32_reset_on_monitor_start)
        self.esp32_reset_up.setChecked(s.esp32_reset_before_upload)

    def apply_to_settings(self):
        s = self.settings
        s.send_mode      = self.send_mode.currentText()
        s.eol            = self.eol.currentText()
        s.custom_eol     = self.custom_eol.text()
        s.delay_enabled  = self.delay_enabled.isChecked()
        s.delay_ms       = int(self.delay_ms.text() or "0")
        s.ack_enabled    = self.ack_enabled.isChecked()
        s.ack_text       = self.ack_text.text()
        s.ack_timeout_ms = int(self.ack_timeout.text() or "1000")
        s.ack_eol        = self.ack_eol.currentText()
        s.ack_custom_eol = self.ack_custom_eol.text()
        s.send_new_before_upload = self.send_new_before_upload.isChecked()
        s.terminal_view  = self.terminal_view.currentText()
        s.terminal_timestamp  = self.terminal_timestamp.isChecked()
        s.terminal_autoscroll = self.terminal_autoscroll.isChecked()
        s.strip_ansi     = self.strip_ansi.isChecked()
        s.normalize_crlf_for_display = self.norm_crlf.isChecked()
        s.terminal_max_lines  = max(500, int(self.term_max_lines.text() or "6000"))
        s.default_language    = self.default_language.currentText()
        s.custom_keywords     = self.custom_keywords.text()
        s.esp32_toggle_dtr_rts_on_connect = self.esp32_toggle_dtr.isChecked()
        s.esp32_reset_on_monitor_start    = self.esp32_reset_mon.isChecked()
        s.esp32_reset_before_upload       = self.esp32_reset_up.isChecked()


# =========================
# IO Manager
# =========================
class IOManager:
    def __init__(self):
        self.serial_conn: Optional[serial.Serial] = None
        self.tcp_sock: Optional[socket.socket]    = None
        self._lock = threading.Lock()

    def is_connected(self) -> bool:
        if self.serial_conn and self.serial_conn.is_open: return True
        if self.tcp_sock: return True
        return False

    def connect_serial(self, port: str, baud: int) -> None:
        self.disconnect()
        self.serial_conn = serial.Serial(port, baud, timeout=0)

    def connect_tcp(self, ip: str, port: int) -> None:
        self.disconnect()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3.0)
        s.connect((ip, port))
        s.setblocking(False)
        self.tcp_sock = s

    def disconnect(self) -> None:
        with self._lock:
            if self.serial_conn:
                try: self.serial_conn.close()
                except Exception: pass
                self.serial_conn = None
            if self.tcp_sock:
                try: self.tcp_sock.close()
                except Exception: pass
                self.tcp_sock = None

    def write(self, data: bytes) -> None:
        with self._lock:
            if self.serial_conn and self.serial_conn.is_open:
                self.serial_conn.write(data); return
            if self.tcp_sock:
                self.tcp_sock.sendall(data); return
        raise RuntimeError("Not connected")

    def read_nonblocking(self, max_bytes: int = 4096) -> bytes:
        with self._lock:
            if self.serial_conn and self.serial_conn.is_open:
                try: return self.serial_conn.read(max_bytes)
                except Exception: return b""
            if self.tcp_sock:
                try: return self.tcp_sock.recv(max_bytes)
                except BlockingIOError: return b""
                except Exception: return b""
        return b""


# =========================
# RX Worker
# =========================
class RXWorker(QThread):
    data_received = pyqtSignal(bytes)

    def __init__(self, io_mgr: IOManager):
        super().__init__()
        self.io_mgr    = io_mgr
        self._stop     = False
        # ack_queue: UploadWorker tarafından set edilir; RX verisi buraya da yazılır
        self.ack_queue: Optional[queue.Queue] = None

    def stop(self): self._stop = True

    def run(self):
        while not self._stop:
            if not self.io_mgr.is_connected():
                time.sleep(0.02); continue
            data = self.io_mgr.read_nonblocking(4096)
            if data:
                self.data_received.emit(data)
                if self.ack_queue is not None:
                    try: self.ack_queue.put_nowait(data)
                    except Exception: pass
            else:
                time.sleep(0.01)


# =========================
# Log Writer
# =========================
class LogWriter(QThread):
    def __init__(self):
        super().__init__()
        self._queue:   List[str] = []
        self._lock     = threading.Lock()
        self._running  = False
        self._file     = None

    def start_logging(self, path: str):
        self._file    = open(path, "a", encoding="utf-8", errors="replace")
        self._running = True
        if not self.isRunning(): self.start()

    def stop_logging(self):
        self._running = False
        time.sleep(0.05)
        if self._file:
            try: self._file.flush(); self._file.close()
            except Exception: pass
        self._file = None

    def log(self, line: str):
        if not self._running: return
        with self._lock: self._queue.append(line)

    def run(self):
        while True:
            if not self._running: time.sleep(0.05); continue
            with self._lock:
                batch = self._queue[:]; self._queue.clear()
            if batch and self._file:
                try: self._file.writelines(batch); self._file.flush()
                except Exception: pass
            time.sleep(0.02)


# =========================
# Payload helpers
# =========================
def parse_custom_char(s: str) -> bytes:
    s = (s or "").strip()
    if not s:           return b""
    if s == r"\n":      return b"\n"
    if s == r"\r":      return b"\r"
    if s == r"\t":      return b"\t"
    m = re.fullmatch(r"\\x([0-9a-fA-F]{2})", s)
    if m: return bytes([int(m.group(1), 16)])
    m = re.fullmatch(r"0x([0-9a-fA-F]{1,2})", s)
    if m: return bytes([int(m.group(1), 16)])
    if re.fullmatch(r"\d{1,3}", s):
        v = int(s)
        if 0 <= v <= 255: return bytes([v])
    if len(s) == 1: return s.encode("latin-1", errors="replace")
    raise ValueError("Custom EOL formatı geçersiz")

def ack_eol_bytes(settings: Settings) -> bytes:
    e = settings.ack_eol or "None"
    if e == "CR":   return b"\r"
    if e == "LF":   return b"\n"
    if e == "CRLF": return b"\r\n"
    if e == "Custom":
        try: return parse_custom_char(settings.ack_custom_eol)
        except Exception: return b""
    return b""

def build_payload_from_line(line: str, settings: Settings) -> bytes:
    if settings.send_mode == "ASCII":
        payload = line.encode("utf-8", errors="replace")
    else:
        t = re.sub(r"\s+", " ", line.strip()).strip()
        if not t: payload = b""
        elif " " in t: payload = bytes(int(p,16) for p in t.split() if p)
        else:
            if len(t) % 2 != 0: raise ValueError("HEX uzunluğu çift olmalı")
            payload = bytes.fromhex(t)

    eol = settings.eol
    if eol == "CR":     payload += b"\r"
    elif eol == "LF":   payload += b"\n"
    elif eol == "CRLF": payload += b"\r\n"
    elif eol == "Custom":
        payload += parse_custom_char(settings.custom_eol)
    return payload

def bytes_to_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


# =========================
# Upload Worker
# =========================
class UploadWorker(QThread):
    progress     = pyqtSignal(int, int)
    finished_ok  = pyqtSignal()
    finished_fail= pyqtSignal(str)

    def __init__(self, io_mgr: IOManager, settings: Settings,
                 lines: List[str], ack_queue: "queue.Queue"):
        super().__init__()
        self.io_mgr    = io_mgr
        self.settings  = settings
        self.lines     = lines
        self.ack_queue = ack_queue
        self._cancel   = False

    def cancel(self): self._cancel = True

    def run(self):
        try:
            total = len(self.lines)
            if total == 0: self.finished_ok.emit(); return

            ack_text_only = (self.settings.ack_text or "ok").encode("utf-8", errors="replace")
            timeout_s = max(1.0, self.settings.ack_timeout_ms / 1000.0)

            # Kuyruktaki eski/stale veriyi temizle (NEW cevabı vs.)
            time.sleep(0.05)
            while not self.ack_queue.empty():
                try: self.ack_queue.get_nowait()
                except queue.Empty: break

            for i, line in enumerate(self.lines, start=1):
                if self._cancel:
                    self.finished_fail.emit("Upload cancelled"); return

                payload = build_payload_from_line(line, self.settings)
                self.io_mgr.write(payload)

                if self.settings.delay_enabled and self.settings.delay_ms > 0:
                    time.sleep(self.settings.delay_ms / 1000.0)

                if self.settings.ack_enabled:
                    if not self._wait_for_ack(ack_text_only, timeout_s):
                        self.finished_fail.emit("ACK gelmedi (timeout)"); return

                self.progress.emit(i, total)

            self.finished_ok.emit()
        except Exception as e:
            self.finished_fail.emit(str(e))

    def _wait_for_ack(self, ack_text_only: bytes, timeout_s: float) -> bool:
        # ack_text boşsa: herhangi bir veri gelince True say
        if not ack_text_only:
            try:
                self.ack_queue.get(timeout=timeout_s)
                return True
            except queue.Empty:
                return False
        # Normal eşleştirme: \r sıyır (ESP32 CRLF -> LF), büyük/küçük harf yok say
        ack_lower = ack_text_only.lower()
        start = time.time()
        buf   = b""
        while time.time() - start < timeout_s:
            if self._cancel: return False
            try:
                chunk = self.ack_queue.get(timeout=0.02)
                buf  += chunk.replace(b"\r", b"")
                if len(buf) > 65536: buf = buf[-65536:]
                if ack_lower in buf.lower():
                    return True
            except queue.Empty:
                pass
        return False


# =========================
# Editor Tab
# =========================
class EditorTab(QWidget):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings  = settings
        self.file_path: Optional[str] = None
        self.language  = settings.default_language
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.editor      = CodeEditor()
        self.highlighter = MultiLangHighlighter(self.editor.document())
        self.apply_language(self.language)
        layout.addWidget(self.editor)

    def apply_language(self, lang: str):
        self.language = lang
        self.highlighter.set_language(lang, self.settings.custom_keywords)

    def title(self) -> str:
        return os.path.basename(self.file_path) if self.file_path else "Untitled"

    def is_modified(self) -> bool:
        return self.editor.document().isModified()


# =========================
# Main Window
# =========================
class AdvancedIDEWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{VERSION}")
        self.resize(1300, 850)

        self.settings_mgr = SettingsManager()
        self.settings     = self.settings_mgr.load()

        self.io_mgr        = IOManager()
        self.upload_worker: Optional[UploadWorker] = None

        self.rx_total = 0;  self.tx_total = 0
        self._rx_last = 0;  self._tx_last = 0
        self.rx_kbs   = 0.0; self.tx_kbs = 0.0
        self.hex_rx_offset = 0
        self.hex_tx_offset = 0

        self.rx_worker = RXWorker(self.io_mgr)
        self.rx_worker.data_received.connect(self._on_rx_data)
        self.rx_worker.start()

        self.log_writer      = LogWriter()
        self.logging_enabled = False
        self.log_file_path   = ""

        self._build_ui()
        self._apply_theme()
        self._apply_terminal_limits()

        self.rate_timer = QTimer()
        self.rate_timer.timeout.connect(self._update_rates)
        self.rate_timer.start(1000)
        self._update_statusbar()

    # ---- UI ----
    def _build_ui(self):
        self.setStatusBar(QStatusBar())
        mb = self.menuBar()

        file_menu  = mb.addMenu("File")
        cfg_menu   = mb.addMenu("Config")
        tools_menu = mb.addMenu("Tools")
        help_menu  = mb.addMenu("Help")

        def act(menu, label, shortcut=None, slot=None):
            a = QAction(label, self)
            if shortcut: a.setShortcut(shortcut)
            if slot:     a.triggered.connect(slot)
            menu.addAction(a)
            return a

        act(file_menu, "New Tab",   "Ctrl+N",       self._new_tab)
        act(file_menu, "Open",      "Ctrl+O",       self._open_file)
        act(file_menu, "Save",      "Ctrl+S",       self._save_file)
        act(file_menu, "Save As",   "Ctrl+Shift+S", self._save_as_file)
        act(file_menu, "Close Tab", "Ctrl+W",       self._close_current_tab)
        act(cfg_menu,  "Settings",  None,           self._open_config)
        act(tools_menu,"Upload Editor","Ctrl+U",    self._upload_editor)
        act(tools_menu,"Cancel Upload",None,        self._cancel_upload)
        tools_menu.addSeparator()
        act(tools_menu,"Send NEW",     None, lambda: self._send_command("NEW"))
        act(tools_menu,"Send LIST",    None, lambda: self._send_command("LIST"))
        act(tools_menu,"Send RUN",     None, lambda: self._send_command("RUN"))
        tools_menu.addSeparator()
        act(tools_menu,"Reset RX/TX counters",None, self._reset_counters)
        act(tools_menu,"Clear Screen","Ctrl+L",     self._clear_screen)
        act(help_menu, "About",     None,           self._about)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8,8,8,8)
        root.setSpacing(8)

        # ---- Top bar ----
        top = QHBoxLayout()
        top.setSpacing(6)

        def lbl(t): top.addWidget(QLabel(t))

        lbl("Mode")
        self.mode_combo = QComboBox(); self.mode_combo.addItems(["Serial","TCP"])
        self.mode_combo.setCurrentText(self.settings.default_mode)
        self.mode_combo.currentIndexChanged.connect(self._update_mode_ui)
        top.addWidget(self.mode_combo)

        lbl("View")
        self.view_combo = QComboBox(); self.view_combo.addItems(["Text","Hex","Both"])
        self.view_combo.setCurrentText(self.settings.terminal_view)
        self.view_combo.currentIndexChanged.connect(self._view_mode_changed)
        top.addWidget(self.view_combo)

        lbl("Port")
        self.port_combo = QComboBox(); self._refresh_ports()
        top.addWidget(self.port_combo)

        self.refresh_btn = QToolButton()
        self.refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_btn.setToolTip("Refresh ports")
        self.refresh_btn.setFixedSize(28,28)
        self.refresh_btn.clicked.connect(self._refresh_ports)
        top.addWidget(self.refresh_btn)

        lbl("Baud")
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600","19200","38400","57600","115200","230400","460800","921600"])
        self.baud_combo.setCurrentText(str(self.settings.default_baud))
        top.addWidget(self.baud_combo)

        lbl("EOL")
        self.eol_combo_quick = QComboBox(); self.eol_combo_quick.addItems(["None","CR","LF","CRLF","Custom"])
        self.eol_combo_quick.setCurrentText(self.settings.eol)
        self.eol_combo_quick.currentIndexChanged.connect(self._quick_eol_changed)
        top.addWidget(self.eol_combo_quick)

        self.ip_input = QLineEdit(self.settings.default_tcp_ip)
        self.ip_input.setPlaceholderText("IP"); top.addWidget(self.ip_input)

        self.tcp_port_input = QLineEdit(str(self.settings.default_tcp_port))
        self.tcp_port_input.setPlaceholderText("Port"); top.addWidget(self.tcp_port_input)

        self.connect_btn = QPushButton("Start Monitoring")
        self.connect_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.connect_btn.clicked.connect(self._toggle_connect)
        top.addWidget(self.connect_btn)

        self.upload_btn = QPushButton("Upload")
        self.upload_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowUp))
        self.upload_btn.clicked.connect(self._upload_editor)
        top.addWidget(self.upload_btn)

        top.addWidget(self._vsep())

        lbl("Lang")
        self.lang_combo = QComboBox(); self.lang_combo.addItems(["BASIC","ASM","C","Custom"])
        self.lang_combo.setCurrentText(self.settings.default_language)
        self.lang_combo.currentIndexChanged.connect(self._apply_language_to_current_tab)
        top.addWidget(self.lang_combo)

        self.btn_cfg = QToolButton()
        self.btn_cfg.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
        self.btn_cfg.setToolTip("Settings"); self.btn_cfg.clicked.connect(self._open_config)
        top.addWidget(self.btn_cfg)

        self.btn_new_pgm = QToolButton()
        self.btn_new_pgm.setToolTip("Send NEW (clear ESP32 program)")
        self.btn_new_pgm.setText("NEW")
        self.btn_new_pgm.clicked.connect(lambda: self._send_command("NEW"))
        top.addWidget(self.btn_new_pgm)

        self.btn_run = QToolButton()
        self.btn_run.setToolTip("Send RUN")
        self.btn_run.setText("RUN")
        self.btn_run.clicked.connect(lambda: self._send_command("RUN"))
        top.addWidget(self.btn_run)

        self.btn_list = QToolButton()
        self.btn_list.setToolTip("Send LIST (show BASIC program)")
        self.btn_list.setText("LIST")
        self.btn_list.clicked.connect(lambda: self._send_command("LIST"))
        top.addWidget(self.btn_list)

        self.btn_info = QToolButton()
        self.btn_info.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        self.btn_info.setToolTip("About")
        self.btn_info.clicked.connect(self._about)
        top.addWidget(self.btn_info)

        top.addWidget(self._vsep())

        self.btn_clear_editor = QPushButton("Clear")
        self.btn_clear_editor.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton))
        self.btn_clear_editor.setToolTip("Clear active editor tab")
        self.btn_clear_editor.clicked.connect(self._clear_editor)
        top.addWidget(self.btn_clear_editor)

        top.addStretch(1)

        # ---- Splitter ----
        self.main_split = QSplitter(Qt.Orientation.Vertical)
        root.addWidget(self.main_split, 1)

        self.tabs = QTabWidget()
        self.tabs.tabBar().setExpanding(False)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab_index)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.main_split.addWidget(self.tabs)

        # ---- Bottom panel ----
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0,0,0,0); bl.setSpacing(6)

        cmd_row = QHBoxLayout(); cmd_row.setSpacing(6)
        cmd_row.addWidget(QLabel(">"))
        self.cmd_input = QLineEdit()
        self.cmd_input.setPlaceholderText("Komut yaz (Enter = gönder)")
        self.cmd_input.returnPressed.connect(self._send_terminal_line)
        cmd_row.addWidget(self.cmd_input, 1)

        self.cmd_send_btn = QPushButton("Send")
        self.cmd_send_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        self.cmd_send_btn.clicked.connect(self._send_terminal_line)
        cmd_row.addWidget(self.cmd_send_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogResetButton))
        self.clear_btn.clicked.connect(self._clear_screen)
        cmd_row.addWidget(self.clear_btn)

        bl.addLayout(top)

        self.term_tabs = QTabWidget()

        # Text tab
        self.terminal_text = TerminalView()
        self.term_tabs.addTab(self.terminal_text, "Text")

        # HEX tab
        hex_w = QWidget(); hex_l = QHBoxLayout(hex_w)
        hex_l.setContentsMargins(0,0,0,0); hex_l.setSpacing(8)
        self.hex_rx = TerminalView("#98c379")
        self.hex_tx = TerminalView("#d19a66")
        hex_l.addWidget(self.hex_rx, 1)
        hex_l.addWidget(self.hex_tx, 1)
        self.term_tabs.addTab(hex_w, "HEX")

        # Log tab
        log_w = QWidget(); log_l = QVBoxLayout(log_w)
        log_l.setContentsMargins(0,0,0,0); log_l.setSpacing(6)
        log_ctrl = QHBoxLayout(); log_ctrl.setSpacing(6)
        self.log_start_btn = QPushButton("Start Logging")
        self.log_start_btn.clicked.connect(self._toggle_logging)
        log_ctrl.addWidget(self.log_start_btn)
        self.log_ts_cb = QCheckBox("Timestamp"); self.log_ts_cb.setChecked(True)
        log_ctrl.addWidget(self.log_ts_cb)
        self.log_select_btn = QPushButton("Select File")
        self.log_select_btn.clicked.connect(self._select_log_file)
        log_ctrl.addWidget(self.log_select_btn)
        self.log_file_label = QLabel("No file selected")
        log_ctrl.addWidget(self.log_file_label)
        log_ctrl.addStretch(1)
        log_l.addLayout(log_ctrl)
        self.log_view = TerminalView()
        log_l.addWidget(self.log_view, 1)
        self.term_tabs.addTab(log_w, "Log")

        bl.addWidget(self.term_tabs, 1)
        bl.addLayout(cmd_row)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(14)
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress.setFormat("0/0")
        bl.addWidget(self.progress)

        self.main_split.addWidget(bottom)
        self.main_split.setStretchFactor(0, 3)
        self.main_split.setStretchFactor(1, 2)

        self._new_tab()
        self._update_mode_ui()

    def _vsep(self):
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color: #333333;")
        return line

    def _apply_theme(self):
        self.setStyleSheet("""
        QMainWindow { background: #1e1e1e; }
        QLabel { color: #ffffff; font-size: 12px; font-weight: 600; }
        QCheckBox { color: #ffffff; font-size: 12px; font-weight: 600; }
        QCheckBox::indicator { width: 14px; height: 14px; }
        QPlainTextEdit, QTextEdit {
            background: #1e1e1e; color: #d4d4d4;
            border: 1px solid #2d2d30;
            selection-background-color: #264f78;
        }
        QLineEdit, QComboBox {
            background: #252526; color: #ffffff;
            border: 1px solid #3a3a3a;
            padding: 3px 6px; border-radius: 2px; min-height: 22px;
        }
        QLineEdit:focus, QComboBox:focus { border: 1px solid #0078D4; }
        QComboBox::drop-down { border: none; }
        QPushButton {
            background: #0078D4; color: #ffffff;
            border: 1px solid #1f1f1f;
            padding: 3px 8px; border-radius: 2px;
            font-weight: 700; min-height: 20px; max-height: 24px;
        }
        QPushButton:hover   { background: #1890ff; }
        QPushButton:pressed { background: #005a9e; }
        QPushButton:disabled { background: #2d2d30; color: #777777; border: 1px solid #333333; }
        QToolButton {
            background: #252526; color: #ffffff;
            border: 1px solid #3a3a3a; border-radius: 2px;
            padding: 3px; min-width: 26px; min-height: 26px;
        }
        QToolButton:hover { border: 1px solid #0078D4; }
        QMenuBar { background: #252526; color: #ffffff; font-weight: 800; }
        QMenuBar::item { background: transparent; padding: 4px 10px; }
        QMenuBar::item:selected { background: #0078D4; color: #ffffff; }
        QMenu { background: #2d2d30; color: #ffffff; border: 1px solid #3a3a3a; font-weight: 700; }
        QMenu::item { padding: 6px 20px; }
        QMenu::item:selected { background: #0078D4; color: #ffffff; }
        QStatusBar { background: #252526; color: #ffffff; }
        QProgressBar {
            background: #252526; color: #ffffff;
            border: 1px solid #3a3a3a; border-radius: 3px; text-align: center;
        }
        QProgressBar::chunk { background-color: #0078D4; }
        QDialog { background: #1e1e1e; }
        QDialog QLabel { color: #ffffff; font-size: 12px; font-weight: 600; }
        QDialog QCheckBox { color: #ffffff; font-weight: 600; }
        QDialog QLineEdit, QDialog QComboBox {
            background: #2d2d30; color: #ffffff;
            border: 1px solid #3a3a3a; padding: 4px 6px;
        }
        QDialog QPushButton { min-height: 24px; font-weight: 700; }
        QSplitter::handle { background: #333333; }
        QTabWidget::pane { border: 1px solid #3a3a3a; }
        QTabBar::tab {
            background: #2d2d30; color: #ffffff;
            padding: 6px 12px; border: 1px solid #3a3a3a;
            border-bottom: none;
            border-top-left-radius: 2px; border-top-right-radius: 2px;
            font-weight: 700;
        }
        QTabBar::tab:selected { background: #1e1e1e; border-top: 2px solid #0078D4; }
        """)
        self.terminal_text.setFont(QFont("Consolas", 10))
        self.hex_rx.setFont(QFont("Consolas", 9))
        self.hex_tx.setFont(QFont("Consolas", 9))
        self.log_view.setFont(QFont("Consolas", 10))

    def _apply_terminal_limits(self):
        n = max(200, self.settings.terminal_max_lines)
        for w in [self.terminal_text, self.hex_rx, self.hex_tx, self.log_view]:
            w.set_max_lines(n)

    # ---- Quick bindings ----
    def _view_mode_changed(self):
        self.settings.terminal_view = self.view_combo.currentText()
        self.settings_mgr.save()

    def _quick_eol_changed(self):
        self.settings.eol = self.eol_combo_quick.currentText()
        self.settings_mgr.save()

    # ---- Tabs ----
    def _new_tab(self):
        tab = EditorTab(self.settings)
        idx = self.tabs.addTab(tab, tab.title())
        self.tabs.setCurrentIndex(idx)
        tab.editor.document().modificationChanged.connect(lambda _=False: self._refresh_tab_titles())

    def _current_tab(self) -> Optional[EditorTab]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, EditorTab) else None

    def _refresh_tab_titles(self):
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if not isinstance(tab, EditorTab): continue
            title = ("* " if tab.is_modified() else "") + tab.title()
            self.tabs.setTabText(i, title)

    def _close_current_tab(self):
        self._close_tab_index(self.tabs.currentIndex())

    def _close_tab_index(self, idx: int):
        if idx < 0 or idx >= self.tabs.count(): return
        tab = self.tabs.widget(idx)
        if not isinstance(tab, EditorTab): return
        if tab.is_modified():
            res = QMessageBox.question(self, "Unsaved", f"{tab.title()} kaydedilmedi. Kapatılsın mı?")
            if res != QMessageBox.StandardButton.Yes: return
        self.tabs.removeTab(idx)
        if self.tabs.count() == 0: self._new_tab()

    def _tab_changed(self, index):
        w = self.tabs.widget(index)
        if not isinstance(w, EditorTab): return
        self.lang_combo.blockSignals(True)
        self.lang_combo.setCurrentText(w.language)
        self.lang_combo.blockSignals(False)

    def _apply_language_to_current_tab(self):
        tab = self._current_tab()
        if tab:
            tab.apply_language(self.lang_combo.currentText())

    # ---- File ----
    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open file", "", "All files (*.*)")
        if not path: return
        try:
            with open(path, "r", encoding="utf-8") as f: content = f.read()
            tab = self._current_tab()
            if not tab: return
            tab.editor.setPlainText(content)
            tab.file_path = path
            tab.editor.document().setModified(False)
            self._refresh_tab_titles()
        except Exception as e:
            QMessageBox.critical(self, "Open error", str(e))

    def _save_file(self):
        tab = self._current_tab()
        if not tab: return
        if not tab.file_path: self._save_as_file(); return
        self._save_to_path(tab.file_path)

    def _save_as_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save as", "", "All files (*.*)")
        if not path: return
        tab = self._current_tab()
        if not tab: return
        tab.file_path = path
        self._save_to_path(path)

    def _save_to_path(self, path: str):
        tab = self._current_tab()
        if not tab: return
        try:
            with open(path, "w", encoding="utf-8") as f: f.write(tab.editor.toPlainText())
            tab.editor.document().setModified(False)
            self._refresh_tab_titles()
        except Exception as e:
            QMessageBox.critical(self, "Save error", str(e))

    # ---- Config ----
    def _open_config(self):
        dlg = ConfigDialog(self, self.settings)
        if dlg.exec():
            dlg.apply_to_settings()
            self.settings_mgr.save()
            self.lang_combo.setCurrentText(self.settings.default_language)
            self.view_combo.setCurrentText(self.settings.terminal_view)
            self.eol_combo_quick.setCurrentText(self.settings.eol)
            for i in range(self.tabs.count()):
                tab = self.tabs.widget(i)
                if isinstance(tab, EditorTab): tab.apply_language(tab.language)
            self._apply_terminal_limits()

    # ---- Connect ----
    def _refresh_ports(self):
        self.port_combo.clear()
        for p in serial.tools.list_ports.comports():
            self.port_combo.addItem(p.device)

    def _update_mode_ui(self):
        is_tcp = self.mode_combo.currentText() == "TCP"
        self.port_combo.setVisible(not is_tcp)
        self.baud_combo.setVisible(not is_tcp)
        self.refresh_btn.setVisible(not is_tcp)
        self.ip_input.setVisible(is_tcp)
        self.tcp_port_input.setVisible(is_tcp)

    def _toggle_connect(self):
        if self.io_mgr.is_connected(): self._disconnect()
        else: self._connect()

    def _connect(self):
        mode = self.mode_combo.currentText()
        try:
            if mode == "Serial":
                port = self.port_combo.currentText()
                baud = int(self.baud_combo.currentText())
                self.io_mgr.connect_serial(port, baud)
                if self.settings.esp32_reset_on_monitor_start:
                    self._esp32_reset()
            else:
                self.io_mgr.connect_tcp(self.ip_input.text().strip(),
                                        int(self.tcp_port_input.text().strip()))
            self.connect_btn.setText("Stop Monitoring")
            self.connect_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
            self.settings.default_mode = mode
            if mode == "Serial": self.settings.default_baud = int(self.baud_combo.currentText())
            else:
                self.settings.default_tcp_ip   = self.ip_input.text().strip()
                self.settings.default_tcp_port  = int(self.tcp_port_input.text().strip())
            self.settings_mgr.save()
            self._update_statusbar()
        except Exception as e:
            QMessageBox.critical(self, "Connect error", str(e))

    def _disconnect(self):
        self._cancel_upload()
        self.io_mgr.disconnect()
        self.connect_btn.setText("Start Monitoring")
        self.connect_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self._update_statusbar()

    def _esp32_reset(self):
        try:
            s = self.io_mgr.serial_conn
            if not s: return
            s.dtr = False
            s.rts = True;  time.sleep(0.12)
            s.rts = False; time.sleep(0.12)
            s.dtr = False; s.rts = False
        except Exception: pass

    # ---- Send command ----
    def _send_command(self, cmd: str):
        try:
            payload = (cmd + "\r\n").encode("utf-8")
            self.io_mgr.write(payload)
            self.tx_total += len(payload)
            self._insert_text(self.terminal_text, f"> {cmd}\n", True)
            self._update_statusbar()
        except Exception as e:
            self._insert_text(self.terminal_text, f"[!] {e}\n", True)

    # ---- Terminal insert ----
    def _insert_text(self, w: QPlainTextEdit, s: str, autoscroll: bool):
        if isinstance(w, TerminalView): w._track_scroll = False
        cur = w.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        w.setTextCursor(cur)
        w.insertPlainText(s)
        if isinstance(w, TerminalView):
            w._track_scroll = True
            if w.smart_should_autoscroll(autoscroll):
                w.moveCursor(QTextCursor.MoveOperation.End)
        elif autoscroll:
            w.moveCursor(QTextCursor.MoveOperation.End)

    # ---- RX ----
    def _on_rx_data(self, data: bytes):
        if not data: return
        self.rx_total += len(data)

        hex_lines, self.hex_rx_offset = hex_dump_lines(data, self.hex_rx_offset)
        for ln in hex_lines:
            self._insert_text(self.hex_rx, ln + "\n", self.settings.terminal_autoscroll)

        decoded = data.decode("utf-8", errors="replace")
        text    = decoded
        if self.settings.strip_ansi:
            text = ANSI_RE.sub("", text)
        if self.settings.normalize_crlf_for_display:
            text = text.replace("\r\n", "\n").replace("\r", "\n")

        ts = time.strftime("[ %H:%M:%S ] ") if self.settings.terminal_timestamp else ""
        view = self.settings.terminal_view
        if view in ("Text","Both"):
            self._insert_text(self.terminal_text, ts + text, self.settings.terminal_autoscroll)
        if view in ("Hex","Both"):
            self._insert_text(self.terminal_text, ts + bytes_to_hex(data) + "\n", self.settings.terminal_autoscroll)

        if self.logging_enabled:
            ts_full = time.strftime("[ %Y-%m-%d %H:%M:%S ] ") if self.log_ts_cb.isChecked() else ""
            payload = ts_full + text
            self.log_writer.log(payload)
            self._insert_text(self.log_view, payload, True)

        self._update_statusbar()

    # ---- Send line ----
    def _send_terminal_line(self):
        line = self.cmd_input.text()
        if not line: return
        try:
            payload = build_payload_from_line(line, self.settings)
            self.io_mgr.write(payload)
            self.tx_total += len(payload)
            self._insert_text(self.terminal_text, "> " + line + "\n", True)
            hex_lines, self.hex_tx_offset = hex_dump_lines(payload, self.hex_tx_offset)
            for ln in hex_lines:
                self._insert_text(self.hex_tx, ln + "\n", True)
            self.cmd_input.clear()
            self._update_statusbar()
        except Exception as e:
            self._insert_text(self.terminal_text, f"[!] Send error: {e}\n", True)

    # ---- Upload ----
    def _upload_editor(self):
        if not self.io_mgr.is_connected():
            QMessageBox.information(self, "Not connected", "Önce bağlan."); return
        if self.upload_worker and self.upload_worker.isRunning():
            QMessageBox.information(self, "Upload", "Zaten upload çalışıyor."); return

        if self.settings.esp32_reset_before_upload:
            self._esp32_reset()

        # Send NEW before upload to clear old program
        if self.settings.send_new_before_upload:
            try:
                self.io_mgr.write(b"NEW\r\n")
                self._insert_text(self.terminal_text, "> NEW\n", True)
                time.sleep(0.4)   # wait for ESP32 to process and print "Ok."
            except Exception as e:
                self._insert_text(self.terminal_text, f"[!] NEW failed: {e}\n", True)

        tab   = self._current_tab()
        if not tab: return
        lines = [l for l in tab.editor.toPlainText().splitlines() if l.strip()]

        self.progress.setMaximum(max(1, len(lines)))
        self.progress.setValue(0)
        self.progress.setFormat(f"0/{len(lines)}")

        # ACK queue: rx_worker -> UploadWorker veri köprüsü
        self._ack_queue: queue.Queue = queue.Queue()
        # Önce queue'yu temizle (eski / artık geçersiz veri olmasın)
        while not self._ack_queue.empty():
            try: self._ack_queue.get_nowait()
            except queue.Empty: break
        self.rx_worker.ack_queue = self._ack_queue   # şimdi aktif et

        self.upload_worker = UploadWorker(self.io_mgr, self.settings, lines, self._ack_queue)
        self.upload_worker.progress.connect(self._on_upload_progress)
        self.upload_worker.finished_ok.connect(self._on_upload_ok)
        self.upload_worker.finished_fail.connect(self._on_upload_fail)
        self.upload_worker.start()
        self._insert_text(self.terminal_text, f"[*] Upload started: {len(lines)} lines\n", True)

    def _cancel_upload(self):
        if self.upload_worker and self.upload_worker.isRunning():
            self.upload_worker.cancel()

    def _on_upload_progress(self, cur: int, total: int):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(cur)
        self.progress.setFormat(f"{cur}/{total}")

    def _on_upload_ok(self):
        self.rx_worker.ack_queue = None
        self.progress.setValue(self.progress.maximum())
        self._insert_text(self.terminal_text, "[*] Upload OK\n", True)

    def _on_upload_fail(self, reason: str):
        self.rx_worker.ack_queue = None
        self._insert_text(self.terminal_text, f"[!] Upload FAIL: {reason}\n", True)

    # ---- Counters ----
    def _reset_counters(self):
        self.rx_total = self.tx_total = self._rx_last = self._tx_last = 0
        self.rx_kbs   = self.tx_kbs = 0.0
        self.hex_rx_offset = self.hex_tx_offset = 0
        self.hex_rx.clear(); self.hex_tx.clear(); self.log_view.clear()
        self._update_statusbar()

    def _update_rates(self):
        rx_d = self.rx_total - self._rx_last
        tx_d = self.tx_total - self._tx_last
        self._rx_last = self.rx_total
        self._tx_last = self.tx_total
        self.rx_kbs   = rx_d / 1024.0
        self.tx_kbs   = tx_d / 1024.0
        self._update_statusbar()

    def _update_statusbar(self):
        if self.io_mgr.serial_conn and self.io_mgr.serial_conn.is_open:
            conn = f"Serial {self.io_mgr.serial_conn.port} @ {self.io_mgr.serial_conn.baudrate}"
        elif self.io_mgr.tcp_sock:
            conn = f"TCP {self.ip_input.text().strip()}:{self.tcp_port_input.text().strip()}"
        else:
            conn = "Not connected"
        self.statusBar().showMessage(
            f"{conn}   |   RX: {self.rx_total} B ({self.rx_kbs:.1f} KB/s)   "
            f"TX: {self.tx_total} B ({self.tx_kbs:.1f} KB/s)"
        )

    # ---- Clear editor ----
    def _clear_editor(self):
        tab = self._current_tab()
        if not tab: return
        if tab.is_modified():
            res = QMessageBox.question(self, "Clear Editor",
                "Editör içeriği silinecek. Devam?")
            if res != QMessageBox.StandardButton.Yes:
                return
        tab.editor.clear()
        tab.editor.document().setModified(False)
        self._refresh_tab_titles()

    # ---- Clear terminal ----
    def _clear_screen(self):
        idx  = self.term_tabs.currentIndex()
        name = self.term_tabs.tabText(idx).lower()
        if name == "text":
            self.terminal_text.clear()
        elif name == "hex":
            self.hex_rx.clear(); self.hex_tx.clear()
            self.hex_rx_offset = self.hex_tx_offset = 0
        elif name == "log":
            self.log_view.clear()
        else:
            self.terminal_text.clear()

    # ---- Log ----
    def _select_log_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Select Log File", "", "Text (*.txt)")
        if path:
            self.log_file_path = path
            self.log_file_label.setText(os.path.basename(path))

    def _toggle_logging(self):
        if not self.logging_enabled:
            if not self.log_file_path:
                QMessageBox.information(self, "Log", "Önce dosya seç."); return
            try: self.log_writer.start_logging(self.log_file_path)
            except Exception as e:
                QMessageBox.critical(self, "Log", f"Log açılamadı: {e}"); return
            self.logging_enabled = True
            self.log_start_btn.setText("Stop Logging")
        else:
            self.log_writer.stop_logging()
            self.logging_enabled = False
            self.log_start_btn.setText("Start Logging")

    # ---- About ----
    def _about(self):
        QMessageBox.information(self, "About",
            f"{APP_NAME} v{VERSION}\nBuild: {BUILD}\n\n"
            f"PyQt6 + pyserial\n"
            f"ACK-based upload | RX thread | HEX dump | Log tab")

    # ---- Close ----
    def closeEvent(self, event):
        modified = [self.tabs.widget(i).title()
                    for i in range(self.tabs.count())
                    if isinstance(self.tabs.widget(i), EditorTab)
                    and self.tabs.widget(i).is_modified()]
        if modified:
            res = QMessageBox.question(self, "Unsaved",
                "Kaydedilmemiş sekmeler:\n- " + "\n- ".join(modified) + "\n\nÇıkılsın mı?")
            if res != QMessageBox.StandardButton.Yes:
                event.ignore(); return
        self._cancel_upload()
        try:
            if self.logging_enabled: self.log_writer.stop_logging()
        except Exception: pass
        try: self.rx_worker.stop(); self.rx_worker.wait(300)
        except Exception: pass
        try: self.io_mgr.disconnect()
        except Exception: pass
        self.settings_mgr.save()
        super().closeEvent(event)


# =========================
# Entry point
# =========================
def main():
    app = QApplication(sys.argv)

    splash_path = os.path.join(os.path.dirname(__file__), "dragut.png")
    splash = None

    if os.path.exists(splash_path):
        original = QPixmap(splash_path)
        scaled   = original.scaledToWidth(480, Qt.TransformationMode.SmoothTransformation)
        canvas   = QPixmap(scaled.width()+80, scaled.height()+140)
        canvas.fill(QColor("black"))
        painter  = QPainter(canvas)
        x = (canvas.width()-scaled.width())//2
        painter.drawPixmap(x, 60, scaled)
        gradient = QLinearGradient(0,0,canvas.width(),0)
        gradient.setColorAt(0.0, QColor(255,255,255,0))
        gradient.setColorAt(0.5, QColor(255,255,255,60))
        gradient.setColorAt(1.0, QColor(255,255,255,0))
        painter.fillRect(canvas.rect(), gradient)
        painter.setPen(QColor("#ffffff"))
        painter.setFont(QFont("Consolas",10))
        painter.drawText(canvas.width()-120, canvas.height()-10, f"v{VERSION}")
        painter.end()
        splash = QSplashScreen(canvas)
        splash.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                              Qt.WindowType.WindowStaysOnTopHint |
                              Qt.WindowType.SplashScreen)
        splash.setWindowOpacity(0.0)
        splash.show()
        app.processEvents()
        for i in range(21):
            splash.setWindowOpacity(i/20); time.sleep(0.02); app.processEvents()
        stages = ["Initializing UI...","Loading modules...",
                  "Preparing terminal...","Starting services...","Finalizing..."]
        pct = 0
        for stage in stages:
            for _ in range(20):
                pct += 1
                splash.showMessage(f"{stage} {pct}%",
                    Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignCenter,
                    QColor("#00BFFF"))
                time.sleep(0.03); app.processEvents()

    win = AdvancedIDEWindow()

    if splash:
        blur = QGraphicsBlurEffect()
        splash.setGraphicsEffect(blur)
        for i in range(16):
            blur.setBlurRadius(i*2)
            splash.setWindowOpacity(1-i/15)
            time.sleep(0.03); app.processEvents()
        splash.finish(win)

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()