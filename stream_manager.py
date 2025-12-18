import sys
import os
import subprocess
import threading
import json
import socket
import winreg
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QFormLayout, QTextEdit, QGroupBox,
    QMessageBox, QComboBox, QCheckBox, QTabWidget, QFileDialog, QSizePolicy
)
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices

# --- Function to list available dshow devices ---
def list_media_devices(ffmpeg_path):
    """
    Uses FFmpeg to list available video and audio devices for dshow.
    Returns three values: (video_devices, audio_devices, raw_output)
    Each device list contains dictionaries: {'name': 'Friendly Name', 'alt': 'Alternative Name'}
    """
    video_devices = []
    audio_devices = []
    output = ""
    try:
        command = [ffmpeg_path, '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy']
        
        # Use CREATE_NO_WINDOW flag to hide the console window
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore',
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        
        output = result.stderr  # dshow lists devices in stderr
        lines = output.splitlines()
        
        current_device_type = None
        last_device_list = None

        for line in lines:
            # Check for device type headers
            if "DirectShow video devices" in line:
                current_device_type = "video"
                last_device_list = video_devices
                continue
            elif "DirectShow audio devices" in line:
                current_device_type = "audio"
                last_device_list = audio_devices
                continue
            
            # Check for Alternative name
            if 'Alternative name' in line and last_device_list:
                if '"' in line:
                    parts = line.split('"')
                    if len(parts) >= 2:
                        alt_name = parts[1]
                        # Update the last entry in the list
                        if last_device_list:
                            last_device_list[-1]['alt'] = alt_name
                continue

            # Parse device names
            if '"' in line:
                try:
                    parts = line.split('"')
                    if len(parts) >= 2:
                        device_name = parts[1]
                        
                        target_list = None
                        if "(video)" in line:
                            target_list = video_devices
                        elif "(audio)" in line:
                            target_list = audio_devices
                        elif current_device_type == "video":
                            target_list = video_devices
                        elif current_device_type == "audio":
                            target_list = audio_devices
                        
                        if target_list is not None:
                            # Default alt is name, will be updated if next line has Alternative name
                            target_list.append({'name': device_name, 'alt': device_name})
                            last_device_list = target_list
                except IndexError:
                    continue 
                    
    except FileNotFoundError:
        output = "ffmpeg.exe not found. Cannot list devices."
        print(output)
    except Exception as e:
        output = f"An error occurred while listing devices: {e}"
        print(output)
        
    return video_devices, audio_devices, output


# --- Worker thread to run FFmpeg ---
class FFmpegWorker(QThread):
    """
    Runs the FFmpeg command in a separate thread to avoid freezing the GUI.
    Emits signals to update the GUI with logs and status.
    """
    log_message = Signal(str)
    process_finished = Signal(int)

    def __init__(self, command):
        super().__init__()
        self.command = command
        self.process = None
        self.running = False

    def run(self):
        self.running = True
        try:
            # Use CREATE_NO_WINDOW flag to hide the console window on Windows
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, # To be able to send 'q'
                text=True,
                encoding='utf-8',
                errors='replace',
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW
            )

            # Read output line by line
            for line in iter(self.process.stdout.readline, ''):
                if not self.running:
                    break
                self.log_message.emit(line.strip())
            
            self.process.stdout.close()
            return_code = self.process.wait()
            if self.running: # If it finished on its own
                self.process_finished.emit(return_code)

        except FileNotFoundError:
            self.log_message.emit("Error: ffmpeg.exe not found. Please check the path.")
            self.process_finished.emit(-1)
        except Exception as e:
            self.log_message.emit(f"An unexpected error occurred: {e}")
            self.process_finished.emit(-1)
        
        self.running = False

    def stop(self):
        self.running = False
        if self.process and self.process.poll() is None:
            self.log_message.emit("Stopping FFmpeg process...")
            try:
                # FFmpeg can be gracefully stopped by sending 'q' to its stdin
                self.process.stdin.write('q\n')
                self.process.stdin.flush()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log_message.emit("FFmpeg did not stop gracefully, terminating.")
                self.process.terminate()
            except Exception as e:
                self.log_message.emit(f"Error stopping FFmpeg: {e}")
                self.process.terminate() # Force terminate
            self.process = None
            self.log_message.emit("FFmpeg process stopped.")
        self.process_finished.emit(0)


# --- Stream Tab Class ---
class StreamTab(QWidget):
    def __init__(self, channel_name, main_window):
        super().__init__()
        self.channel_name = channel_name
        self.main_window = main_window
        self.ffmpeg_worker = None
        self.is_streaming = False

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        # --- Settings Section ---
        settings_group = QGroupBox("Settings")
        settings_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QFormLayout()
        layout.setContentsMargins(15, 20, 15, 15)
        layout.setSpacing(10)
        
        self.input_video_device = QComboBox()
        self.input_audio_device = QComboBox()
        self.refresh_devices_button = QPushButton("Refresh Devices")
        self.refresh_devices_button.clicked.connect(self.populate_devices)

        self.input_video_size = QComboBox()
        self.input_video_size.setEditable(True)
        self.input_video_size.addItems(["1920x1080", "1280x720", "854x480", "640x360"])

        self.input_framerate = QComboBox()
        self.input_framerate.setEditable(True)
        self.input_framerate.addItems(["60", "30", "25", "24"])

        self.input_video_bitrate = QComboBox()
        self.input_video_bitrate.setEditable(True)
        self.input_video_bitrate.addItems(["6000k", "4500k", "3000k", "1200k", "800k"])

        self.input_audio_bitrate = QComboBox()
        self.input_audio_bitrate.setEditable(True)
        self.input_audio_bitrate.addItems(["192k", "128k", "96k", "64k"])

        self.cb_auto_start = QCheckBox("Start on Launch")

        self.output_playback_url = QLineEdit()
        self.output_playback_url.setReadOnly(True)
        self.btn_copy_url = QPushButton("Copy")
        self.btn_copy_url.setFixedWidth(60)
        self.btn_copy_url.clicked.connect(self.copy_playback_url)

        video_device_layout = QHBoxLayout()
        video_device_layout.addWidget(self.input_video_device)
        video_device_layout.addWidget(self.refresh_devices_button)
        layout.addRow(QLabel("Video Device:"), video_device_layout)

        layout.addRow(QLabel("Audio Device:"), self.input_audio_device)
        layout.addRow(QLabel("Video Size:"), self.input_video_size)
        layout.addRow(QLabel("Framerate:"), self.input_framerate)
        layout.addRow(QLabel("Video Bitrate:"), self.input_video_bitrate)
        layout.addRow(QLabel("Audio Bitrate:"), self.input_audio_bitrate)
        layout.addRow(self.cb_auto_start)
        
        url_layout = QHBoxLayout()
        url_layout.addWidget(self.output_playback_url)
        url_layout.addWidget(self.btn_copy_url)
        layout.addRow(QLabel("Playback URL:"), url_layout)

        self.toggle_button = QPushButton("Start Stream")
        self.toggle_button.clicked.connect(self.toggle_stream)
        self.toggle_button.setStyleSheet("background-color: #4CAF50; color: white; height: 40px; font-size: 16px; font-weight: bold;")
        layout.addRow(self.toggle_button)

        settings_group.setLayout(layout)
        main_layout.addWidget(settings_group, 1)

        # --- Log Section ---
        log_group = QGroupBox("Channel Log")
        log_layout = QVBoxLayout()
        log_layout.setContentsMargins(10, 20, 10, 10)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        # Removed inline style to use global stylesheet
        log_layout.addWidget(self.log_view)
        log_group.setLayout(log_layout)
        main_layout.addWidget(log_group, 1)

        self.setLayout(main_layout)
        
        # Initial population
        self.populate_devices()
        self.update_playback_url()

    def log(self, message):
        self.log_view.append(message)
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    def populate_devices(self):
        ffmpeg_path = self.main_window.path_ffmpeg.text()
        if not os.path.exists(ffmpeg_path):
            return
            
        current_video = self.input_video_device.currentText()
        current_audio = self.input_audio_device.currentText()
        
        self.input_video_device.clear()
        self.input_audio_device.clear()
        
        video_devices, audio_devices, _ = list_media_devices(ffmpeg_path)
        
        for i, dev in enumerate(video_devices, start=1):
            label = f"{dev['name']}  [#{i}]"
            self.input_video_device.addItem(label, dev['alt'])
 
        for i, dev in enumerate(audio_devices, start=1):
            label = f"{dev['name']}  [#{i}]"
            self.input_audio_device.addItem(label, dev['alt'])

        
        # Restore selection (simple text match)
        index = self.input_video_device.findText(current_video)
        if index >= 0:
            self.input_video_device.setCurrentIndex(index)
            
        index = self.input_audio_device.findText(current_audio)
        if index >= 0:
            self.input_audio_device.setCurrentIndex(index)

    def update_playback_url(self):
        ip = self.main_window.get_local_ip()
        # Use a simpler safe name to match likely folder structures
        safe_name = self.channel_name.replace(" ", "").lower()
        self.output_playback_url.setText(f"http://{ip}:5001/hls/{safe_name}/index.m3u8")

    def copy_playback_url(self):
        clipboard = QApplication.clipboard()
        clipboard.setText(self.output_playback_url.text())

    def toggle_stream(self):
        if self.is_streaming:
            self.stop_stream()
        else:
            self.start_stream()

    def start_stream(self):
        if not self.main_window.ensure_nginx_running():
            return

        self.log(f"Starting stream...")
        video_alt = self.input_video_device.currentData()
        used_in = self.main_window.is_video_device_in_use(video_alt, self)

        if used_in:
            QMessageBox.warning(
                self,
                "Video Device In Use",
                f"This video device is already used in channel:\n\n{used_in}\n\n"
                "Please select a different capture device."
            )
            return

        
        hls_root = self.main_window.path_hls.text()
        # Use a simpler safe name to match likely folder structures
        safe_name = self.channel_name.replace(" ", "").lower()
        channel_dir = os.path.join(hls_root, safe_name)
        
        try:
            os.makedirs(channel_dir, exist_ok=True)
        except Exception as e:
            self.log(f"Error creating directory: {e}")
            return

        command = self.build_ffmpeg_command(channel_dir)
        if not command:
            return

        self.ffmpeg_worker = FFmpegWorker(command)
        self.ffmpeg_worker.log_message.connect(self.log)
        self.ffmpeg_worker.process_finished.connect(self.on_ffmpeg_finished)
        self.ffmpeg_worker.start()
        
        self.is_streaming = True
        self.update_ui_status()

    def stop_stream(self):
        self.log("Stopping stream...")

        if self.ffmpeg_worker:
           self.ffmpeg_worker.stop()

        # 🧹 تنظيف ملفات HLS القديمة
        hls_root = self.main_window.path_hls.text()
        safe_name = self.channel_name.replace(" ", "").lower()
        channel_dir = os.path.join(hls_root, safe_name)

        try:
            for f in os.listdir(channel_dir):
                if f.endswith('.ts') or f.endswith('.m3u8'):
                    os.remove(os.path.join(channel_dir, f))
        except Exception:
            pass

        self.is_streaming = False
        self.update_ui_status()
        self.main_window.check_and_stop_nginx()


    def on_ffmpeg_finished(self, code):
        self.log(f"FFmpeg finished with code {code}")
        if self.is_streaming:
             self.is_streaming = False
             self.update_ui_status()

    def update_ui_status(self):
        if self.is_streaming:
            self.toggle_button.setText("Stop Stream")
            self.toggle_button.setStyleSheet("background-color: #f44336; color: white; height: 40px; font-size: 16px; font-weight: bold;")
            self.input_video_device.setEnabled(False)
            self.input_audio_device.setEnabled(False)
            self.input_video_size.setEnabled(False)
            self.input_framerate.setEnabled(False)
            self.input_video_bitrate.setEnabled(False)
            self.input_audio_bitrate.setEnabled(False)
        else:
            self.toggle_button.setText("Start Stream")
            self.toggle_button.setStyleSheet("background-color: #4CAF50; color: white; height: 40px; font-size: 16px; font-weight: bold;")
            self.input_video_device.setEnabled(True)
            self.input_audio_device.setEnabled(True)
            self.input_video_size.setEnabled(True)
            self.input_framerate.setEnabled(True)
            self.input_video_bitrate.setEnabled(True)
            self.input_audio_bitrate.setEnabled(True)

    def build_ffmpeg_command(self, output_dir):
        ffmpeg_exe = self.main_window.path_ffmpeg.text()
        if not os.path.exists(ffmpeg_exe):
            self.log_message.emit(f"FFmpeg executable not found at: {ffmpeg_exe}")
            return None

        command = [
            ffmpeg_exe,
            '-hide_banner', '-loglevel', 'info',
            '-f', 'dshow',
            '-rtbufsize', '512M',
        ]

        if self.input_framerate.currentText().strip():
            command.extend(['-framerate', self.input_framerate.currentText().strip()])

        # Use alternative name (userData) if available, otherwise text
        video_dev = self.input_video_device.currentData()
        audio_dev = self.input_audio_device.currentData()

        command.extend([
            '-thread_queue_size', '1024',
            '-i', f'video={video_dev}:audio={audio_dev}',
            '-map', '0:v:0', '-map', '0:a:0',
            '-c:v', 'libx264',
            '-preset', 'superfast',
            '-tune', 'zerolatency',
            '-profile:v', 'baseline',
            '-level', '3.1',
            '-pix_fmt', 'yuv420p',
            '-fps_mode', 'cfr',
        ])

        # --- Video filter ---
        if self.input_video_size.currentText().strip():
            command.extend([
                '-vf',
                f'scale={self.input_video_size.currentText().strip()}:flags=lanczos'
    ])

        # --- Dynamic values ---
        bitrate = self.input_video_bitrate.currentText()
        fps = int(self.input_framerate.currentText() or 25)
        bufsize = str(int(bitrate.replace('k', '')) * 2) + 'k'
        gop = fps * 2

        command.extend([
            # --- Video ---
            '-b:v', bitrate,
            '-maxrate', bitrate,
            '-bufsize', bufsize,
            '-g', str(gop),
            '-keyint_min', str(gop),

            # --- Audio ---
            '-c:a', 'aac',
            '-b:a', self.input_audio_bitrate.currentText(),
            '-ar', '44100',

            # --- HLS ---
            '-f', 'hls',
            '-hls_time', '2',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+program_date_time+independent_segments',
            '-hls_segment_filename', os.path.join(output_dir, 'segment_%d.ts'),
            os.path.join(output_dir, 'index.m3u8')
        ])

        return command


    def get_config(self):
        return {
            "channel_name": self.channel_name,
            "video_device_alt": self.input_video_device.currentData(),
            "audio_device_alt": self.input_audio_device.currentData(),
            "video_device_label": self.input_video_device.currentText(),
            "audio_device_label": self.input_audio_device.currentText(),
            "video_size": self.input_video_size.currentText(),
            "framerate": self.input_framerate.currentText(),
            "video_bitrate": self.input_video_bitrate.currentText(),
            "audio_bitrate": self.input_audio_bitrate.currentText(),
            "auto_start": self.cb_auto_start.isChecked()
        }

    def load_config(self, config):
        self.populate_devices()

        self.input_video_size.setCurrentText(config.get("video_size", "1280x720"))
        self.input_framerate.setCurrentText(config.get("framerate", "30"))
        self.input_video_bitrate.setCurrentText(config.get("video_bitrate", "1200k"))
        self.input_audio_bitrate.setCurrentText(config.get("audio_bitrate", "96k"))
        self.cb_auto_start.setChecked(config.get("auto_start", False))
        video_alt = config.get("video_device_alt")
        if video_alt:
           index = self.input_video_device.findData(video_alt)
           if index >= 0:
              self.input_video_device.setCurrentIndex(index)

        audio_alt = config.get("audio_device_alt")
        if audio_alt:
           index = self.input_audio_device.findData(audio_alt)
           if index >= 0:
              self.input_audio_device.setCurrentIndex(index)

# --- Main Application Window ---
class StreamManagerApp(QMainWindow):
    CONFIG_FILE = "stream_config.json"
    
    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def check_startup_status(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, "StreamManagerApp")
            key.Close()
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def toggle_startup(self, checked):
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "StreamManagerApp"
        try:
            if checked:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
                python_exe = sys.executable
                pythonw_exe = os.path.join(os.path.dirname(python_exe), 'pythonw.exe')
                exe_to_use = pythonw_exe if os.path.exists(pythonw_exe) else python_exe
                script_path = os.path.abspath(__file__)
                command = f'"{exe_to_use}" "{script_path}"'
                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
                key.Close()
                self.log("Added to Windows Startup.")
            else:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
                winreg.DeleteValue(key, app_name)
                key.Close()
                self.log("Removed from Windows Startup.")
        except Exception as e:
            self.log(f"Error changing startup settings: {e}")
            self.cb_run_on_startup.blockSignals(True)
            self.cb_run_on_startup.setChecked(not checked)
            self.cb_run_on_startup.blockSignals(False)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Stream Manager - Multi-Channel")
        self.setGeometry(100, 100, 1000, 600)
        self.apply_stylesheet()

        self.nginx_process = None
        self.stream_tabs = []

        # --- Main Layout ---
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        # Hidden Global Config (Variables kept for logic)
        self.path_ffmpeg = QLineEdit()
        self.path_nginx = QLineEdit()
        self.path_hls = QLineEdit()
        
        # Tabs for Channels
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # Add/Remove Channel Buttons
        btn_layout = QHBoxLayout()
        self.btn_add_tab = QPushButton("Add Channel")
        self.btn_add_tab.clicked.connect(self.add_new_tab)
        self.btn_remove_tab = QPushButton("Remove Current Channel")
        self.btn_remove_tab.clicked.connect(self.remove_current_tab)
        
        self.cb_run_on_startup = QCheckBox("Run on Windows Startup")
        self.cb_run_on_startup.setChecked(self.check_startup_status())
        self.cb_run_on_startup.toggled.connect(self.toggle_startup)
        
        btn_layout.addWidget(self.btn_add_tab)
        btn_layout.addWidget(self.btn_remove_tab)
        btn_layout.addStretch()
        btn_layout.addWidget(self.cb_run_on_startup)
        
        main_layout.addLayout(btn_layout)

        # --- Initial State ---
        self.load_config()
        self.check_dependencies()

    def apply_stylesheet(self):
        style = """
        QMainWindow, QWidget {
            background-color: #2b2b2b;
            color: #f0f0f0;
            font-family: "Segoe UI", sans-serif;
            font-size: 10pt;
        }
        QGroupBox {
            border: 1px solid #555;
            border-radius: 6px;
            margin-top: 12px;
            font-weight: bold;
            background-color: #333;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 5px;
            left: 10px;
            color: #4CAF50;
        }
        QPushButton {
            background-color: #444;
            border: 1px solid #555;
            border-radius: 5px;
            padding: 6px 12px;
            color: #fff;
        }
        QPushButton:hover {
            background-color: #555;
            border-color: #666;
        }
        QPushButton:pressed {
            background-color: #222;
        }
        QLineEdit, QComboBox {
            background-color: #222;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 5px;
            color: #fff;
            selection-background-color: #4CAF50;
        }
        QComboBox::drop-down {
            border: none;
            width: 20px;
        }
        QTabWidget::pane {
            border: 1px solid #444;
            background-color: #333;
            border-radius: 5px;
        }
        QTabBar::tab {
            background-color: #2b2b2b;
            color: #aaa;
            padding: 8px 20px;
            border-top-left-radius: 5px;
            border-top-right-radius: 5px;
            margin-right: 2px;
        }
        QTabBar::tab:selected {
            background-color: #333;
            color: #fff;
            border-bottom: 2px solid #4CAF50;
        }
        QTabBar::tab:hover {
            background-color: #383838;
        }
        QScrollBar:vertical {
            border: none;
            background: #2b2b2b;
            width: 10px;
            margin: 0px;
        }
        QScrollBar::handle:vertical {
            background: #555;
            min-height: 20px;
            border-radius: 5px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
        """
        self.setStyleSheet(style)

    def check_dependencies(self):
        # Check FFmpeg
        ffmpeg_path = self.path_ffmpeg.text()
        if not os.path.exists(ffmpeg_path):
            msg = QMessageBox(self)
            msg.setWindowTitle("Missing Dependency")
            msg.setIcon(QMessageBox.Warning)
            msg.setText("FFmpeg is missing.")
            msg.setInformativeText("FFmpeg executable was not found. It is required for streaming.\n\n"
                                   "Please locate 'ffmpeg.exe' or download it.")
            btn_locate = msg.addButton("Locate FFmpeg", QMessageBox.AcceptRole)
            btn_download = msg.addButton("Download", QMessageBox.ActionRole)
            btn_cancel = msg.addButton(QMessageBox.Cancel)
            msg.exec()
            
            if msg.clickedButton() == btn_locate:
                file_path, _ = QFileDialog.getOpenFileName(self, "Select FFmpeg Executable", "C:\\", "Executables (*.exe)")
                if file_path:
                    self.path_ffmpeg.setText(file_path)
                    self.save_config()
            elif msg.clickedButton() == btn_download:
                QDesktopServices.openUrl(QUrl("https://www.gyan.dev/ffmpeg/builds/"))

        # Check Nginx
        nginx_path = self.path_nginx.text()
        nginx_exe = os.path.join(nginx_path, "nginx.exe")
        if not os.path.exists(nginx_exe):
            msg = QMessageBox(self)
            msg.setWindowTitle("Missing Dependency")
            msg.setIcon(QMessageBox.Warning)
            msg.setText("Nginx is missing.")
            msg.setInformativeText("Nginx executable was not found in the specified folder.\n\n"
                                   "Please locate the Nginx folder or download it.")
            btn_locate = msg.addButton("Locate Nginx Folder", QMessageBox.AcceptRole)
            btn_download = msg.addButton("Download", QMessageBox.ActionRole)
            btn_cancel = msg.addButton(QMessageBox.Cancel)
            msg.exec()
            
            if msg.clickedButton() == btn_locate:
                folder_path = QFileDialog.getExistingDirectory(self, "Select Nginx Folder", "C:\\")
                if folder_path:
                    self.path_nginx.setText(folder_path)
                    self.save_config()
            elif msg.clickedButton() == btn_download:
                QDesktopServices.openUrl(QUrl("https://nginx.org/en/download.html"))

    def add_new_tab(self, name=None, config=None):
        if not name:
            count = self.tabs.count() + 1
            name = f"Channel {count}"
        
        tab = StreamTab(name, self)
        self.tabs.addTab(tab, name)
        self.stream_tabs.append(tab)
        
        if config:
            tab.load_config(config)
            
        return tab

    def remove_current_tab(self):
        index = self.tabs.currentIndex()
        if index != -1:
            tab = self.tabs.widget(index)
            if tab.is_streaming:
                QMessageBox.warning(self, "Cannot Remove", "Stop the stream before removing the channel.")
                return
            
            reply = QMessageBox.question(self, "Confirm Removal", 
                                       f"Are you sure you want to remove '{tab.channel_name}'?",
                                       QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            
            if reply == QMessageBox.Yes:
                self.tabs.removeTab(index)
                self.stream_tabs.remove(tab)

    def log(self, message):
        # Log to the currently active tab if possible
        current_tab = self.tabs.currentWidget()
        if isinstance(current_tab, StreamTab):
            current_tab.log(f"[System] {message}")
        else:
            print(f"[System] {message}")

    def ensure_nginx_running(self):
        if self.nginx_process and self.nginx_process.poll() is None:
            return True
            
        nginx_dir = self.path_nginx.text()
        nginx_exe = os.path.join(nginx_dir, "nginx.exe")
        if not os.path.exists(nginx_exe):
            self.log(f"Nginx not found at: {nginx_exe}")
            QMessageBox.critical(self, "Error", f"nginx.exe not found in:\n{nginx_dir}")
            return False
        
        try:
            # Kill any existing nginx first to be safe
            subprocess.run(["taskkill", "/F", "/IM", "nginx.exe"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)

            self.log("Starting Nginx...")
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            self.nginx_process = subprocess.Popen(
                [nginx_exe], cwd=nginx_dir, startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            self.log(f"Nginx started with PID: {self.nginx_process.pid}")
            return True
        except Exception as e:
            self.log(f"Failed to start Nginx: {e}")
            return False

    def load_config(self):
        defaults = {
            "ffmpeg_path": "C:\\ffmpeg\\bin\\ffmpeg.exe",
            "nginx_path": "C:\\nginx",
            "hls_path": "C:\\hls",
            "streams": []
        }
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, 'r') as f:
                    config = json.load(f)
            else:
                config = defaults
                self.log("No config file found, loaded default settings.")

            self.path_ffmpeg.setText(config.get("ffmpeg_path", defaults["ffmpeg_path"]))
            self.path_nginx.setText(config.get("nginx_path", defaults["nginx_path"]))
            
            # Fix legacy path issue: if path ends with 'channel1', strip it to get the root
            hls_path = config.get("hls_path", defaults["hls_path"])
            if hls_path.endswith("channel1") or hls_path.endswith("channel1\\") or hls_path.endswith("channel1/"):
                hls_path = os.path.dirname(hls_path.rstrip("\\/"))
                self.log(f"Updated HLS path from legacy config to: {hls_path}")
            
            self.path_hls.setText(hls_path)
            
            # Load streams
            streams = config.get("streams", [])
            if not streams:
                # Add one default tab
                self.add_new_tab("Channel 1")
            else:
                for stream_conf in streams:
                    self.add_new_tab(stream_conf.get("channel_name"), stream_conf)

            self.log("Configuration loaded.")

            # Auto-start streams if enabled
            QTimer.singleShot(2000, self.start_all_streams)

        except Exception as e:
            self.log(f"Error loading config: {e}")
            # Fallback
            if self.tabs.count() == 0:
                self.add_new_tab("Channel 1")

    def save_config(self):
        streams_config = [tab.get_config() for tab in self.stream_tabs]
        config = {
            "ffmpeg_path": self.path_ffmpeg.text(),
            "nginx_path": self.path_nginx.text(),
            "hls_path": self.path_hls.text(),
            "streams": streams_config
        }
        try:
            with open(self.CONFIG_FILE, 'w') as f:
                json.dump(config, f, indent=4)
            self.log("Configuration saved automatically.")
        except Exception as e:
            self.log(f"Error saving config: {e}")

    def start_all_streams(self):
        self.log("Checking for auto-start streams...")
        for tab in self.stream_tabs:
            if tab.cb_auto_start.isChecked() and not tab.is_streaming:
                tab.start_stream()

    def check_and_stop_nginx(self):
        # Check if any tab is streaming
        any_streaming = any(tab.is_streaming for tab in self.stream_tabs)
        if not any_streaming and self.nginx_process:
            self.log("No active streams. Stopping Nginx...")
            try:
                nginx_exe = os.path.join(self.path_nginx.text(), "nginx.exe")
                subprocess.run(
                    [nginx_exe, "-s", "stop"],
                    cwd=self.path_nginx.text(),
                    capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                self.nginx_process.wait(timeout=2)
            except Exception as e:
                self.log(f"Error stopping Nginx gracefully: {e}")
                if self.nginx_process:
                    self.nginx_process.terminate()
            self.nginx_process = None
            self.log("Nginx stopped.")

    def closeEvent(self, event):
        self.save_config()
        
        # Stop all streams
        for tab in self.stream_tabs:
            if tab.is_streaming:
                tab.stop_stream()
        
        # Stop Nginx
        if self.nginx_process:
            try:
                self.nginx_process.terminate()
            except:
                pass
            # Also try taskkill to be sure
            subprocess.run(["taskkill", "/F", "/IM", "nginx.exe"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        
        event.accept()
    def is_video_device_in_use(self, device_alt, current_tab):
        for tab in self.stream_tabs:
            if tab is current_tab:
               continue
            if tab.is_streaming and tab.input_video_device.currentData() == device_alt:
                return tab.channel_name
        return None



# --- Auto-update from GitHub before launching the app ---
import time
def auto_update_from_github():
    repo_url = "https://github.com/bassamaljaafari1/stream-manager.git"
    try:
        # Check if .git exists
        if os.path.exists('.git'):
            # Fetch latest changes
            fetch = subprocess.run(["git", "fetch"], capture_output=True, text=True)
            # Check if there are new commits
            status = subprocess.run(["git", "status", "-uno"], capture_output=True, text=True)
            if "Your branch is behind" in status.stdout:
                print("تحديث جديد متوفر، سيتم التحديث وإعادة التشغيل...")
                pull = subprocess.run(["git", "pull"], capture_output=True, text=True)
                print(pull.stdout)
                # إعادة تشغيل البرنامج
                python = sys.executable
                os.execl(python, python, *sys.argv)
        else:
            print("لم يتم العثور على مستودع git في هذا المجلد.")
    except Exception as e:
        print(f"خطأ أثناء التحقق من التحديثات: {e}")

if __name__ == '__main__':
    auto_update_from_github()
    app = QApplication(sys.argv)
    window = StreamManagerApp()
    window.show()
    sys.exit(app.exec())
