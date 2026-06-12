"""批量打包控制台：管理多个项目的并发多进程打包与专属日志查看。"""
import os
import shutil
import json
from PyQt5.QtCore import Qt, QProcess, QProcessEnvironment, QDateTime, QTimer, QUrl
from PyQt5.QtGui import QFont, QDesktopServices, QColor
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QTextBrowser, QProgressBar, QSpinBox,
    QSplitter, QMessageBox, QWidget, QMenu
)

class BatchBuildDialog(QDialog):
    def __init__(self, project_paths: list, parent=None):
        super().__init__(parent)
        self.project_paths = project_paths
        self.setWindowTitle("批量打包控制台 - builderTool")
        self.resize(1000, 600)
        self.setMinimumSize(800, 500)
        
        # 缓存每个项目的状态与进程数据
        self.tasks = {}
        self.queue = []      # 排队项目路径列表
        self.running_count = 0
        
        # 初始化任务数据结构
        for path in self.project_paths:
            name = os.path.basename(path)
            vue_ver, node_ver = self._detect_vue_and_node(path)
            self.tasks[path] = {
                "name": name,
                "vue": vue_ver,
                "node": node_ver,
                "status": "pending",  # pending, building, success, failed, aborted
                "process": None,
                "log_buffer": "",
                "start_time": None,
                "elapsed": 0,
                "timer": None
            }
            self.queue.append(path)

        self._build_ui()
        self._update_abort_button_state()
        
        # 启动状态刷新定时器 (计算运行耗时)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._on_tick)
        self.refresh_timer.start(1000)

        # 延时自动启动调度引擎
        QTimer.singleShot(200, self._schedule)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # 顶部工具栏
        top_layout = QHBoxLayout()
        
        # 并发控制
        top_layout.addWidget(QLabel("最大并发数："))
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 8)
        self.parallel_spin.setValue(3)
        self.parallel_spin.setToolTip("限制同时打包的最大项目数，避免 CPU/内存假死")
        self.parallel_spin.valueChanged.connect(self._schedule)
        top_layout.addWidget(self.parallel_spin)
        
        top_layout.addSpacing(20)

        # 全局进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(self.project_paths))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("已完成 %v/%m")
        top_layout.addWidget(self.progress_bar, 1)

        # 全部中止按钮
        self.abort_all_btn = QPushButton("🛑 全部中止")
        self.abort_all_btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold; padding: 5px 12px;")
        self.abort_all_btn.clicked.connect(self._on_abort_click)
        top_layout.addWidget(self.abort_all_btn)

        layout.addLayout(top_layout)

        # 主界面 QSplitter 左右布局
        splitter = QSplitter(Qt.Horizontal, self)
        
        # 左侧列表
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["项目名", "版本", "状态", "耗时"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_row_changed)
        self.table.doubleClicked.connect(self._on_table_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_context_menu)
        left_layout.addWidget(self.table)
        
        left_layout.addWidget(QLabel("💡 双击打包成功项可直接打开产物文件夹"))
        
        # 填充表格
        self.table.setRowCount(len(self.project_paths))
        for idx, path in enumerate(self.project_paths):
            task = self.tasks[path]
            ver_str = f"Vue {task['vue']}" if task["vue"] else "未知"
            
            item_name = QTableWidgetItem(task["name"])
            item_name.setData(Qt.UserRole, path)
            
            self.table.setItem(idx, 0, item_name)
            self.table.setItem(idx, 1, QTableWidgetItem(ver_str))
            self.table.setItem(idx, 2, QTableWidgetItem("⏳ 排队中"))
            self.table.setItem(idx, 3, QTableWidgetItem("0s"))
            
            # 对单元格状态着色
            self._update_row_status_color(idx, "pending")

        splitter.addWidget(left_widget)

        # 右侧日志区
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        self.log_title = QLabel("请在左侧选择项目以查看日志")
        self.log_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        right_layout.addWidget(self.log_title)
        
        self.log_view = QTextBrowser()
        self.log_view.setReadOnly(True)
        self.log_view.setOpenLinks(False)
        self.log_view.anchorClicked.connect(self._on_log_link_clicked)
        self.log_view.setFont(QFont("Consolas", 9))
        right_layout.addWidget(self.log_view, 1)

        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        
        layout.addWidget(splitter, 1)

        # 默认选择第一行
        self.table.selectRow(0)

    def _detect_vue_and_node(self, repo_path: str):
        """读取 package.json，检测 vue 版本"""
        pkg_path = os.path.join(repo_path, "package.json")
        if not os.path.exists(pkg_path):
            return None, None
        try:
            with open(pkg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            deps = data.get("dependencies", {})
            dev_deps = data.get("devDependencies", {})
            vue_ver = deps.get("vue") or dev_deps.get("vue")
            if vue_ver:
                clean_ver = str(vue_ver).lstrip("^~>=< ")
                if clean_ver.startswith("2"):
                    return 2, "8.12.0"
                elif clean_ver.startswith("3"):
                    return 3, "20.18.0"
        except Exception:
            pass
        return None, None

    def _schedule(self):
        """核心任务调度：将排队任务填充到最大并发数中"""
        max_parallel = self.parallel_spin.value()
        while self.running_count < max_parallel and self.queue:
            path = self.queue.pop(0)
            self._start_build(path)

    def _start_build(self, path: str):
        task = self.tasks[path]
        task["status"] = "building"
        task["start_time"] = QDateTime.currentDateTime()
        self.running_count += 1
        self._update_abort_button_state()
        
        # 1. 更新表格状态
        idx = self._find_row_by_path(path)
        if idx >= 0:
            self.table.setItem(idx, 2, QTableWidgetItem("⚙ 打包中..."))
            self._update_row_status_color(idx, "building")

        # 2. 清理旧产物
        self._clear_old_outputs(path)
        
        # 3. 构造子进程
        process = QProcess(self)
        process.setWorkingDirectory(path)
        process.setProcessChannelMode(QProcess.MergedChannels)
        
        # 注入 NVM 环境变量
        env = QProcessEnvironment.systemEnvironment()
        node_ver = task["node"]
        if node_ver:
            nvm_home = os.environ.get("NVM_HOME", r"C:\Users\Administrator\AppData\Roaming\nvm")
            node_dir = os.path.join(nvm_home, f"v{node_ver}")
            if os.path.exists(node_dir):
                current_path = env.value("PATH")
                env.insert("PATH", f"{node_dir};{current_path}")
                process.setProcessEnvironment(env)
                task["log_buffer"] += f"[builderTool] 已挂载局部环境变量 Node: v{node_ver} ({node_dir})\n"
            else:
                task["log_buffer"] += f"[builderTool] 警告：未找到 Node {node_ver} 目录，回退至系统默认 Node。\n"
        
        # 绑定日志输出
        process.readyReadStandardOutput.connect(lambda p=path: self._on_process_output(p))
        process.finished.connect(lambda code, status, p=path: self._on_process_finished(p, code, status))
        
        task["process"] = process
        task["log_buffer"] += f"[builderTool] 开始执行命令: npm run build\n"
        
        # 如果当前正在查看这个项目的日志，立刻更新日志标题
        if self._get_current_selected_path() == path:
            self.log_title.setText(f"项目日志：{task['name']} [⚙ 打包中...]")
            self.log_view.setHtml(task["log_buffer"].replace("\n", "<br>"))
            self._scroll_log_to_bottom()
            
        process.start("cmd.exe", ["/c", "npm run build"])

    def _clear_old_outputs(self, repo_path: str):
        cleaned_paths = []
        for path_rel in [r"dist\disk", r"dist.zip", r"disk"]:
            full_path = os.path.join(repo_path, path_rel)
            if os.path.exists(full_path):
                try:
                    if os.path.isdir(full_path):
                        shutil.rmtree(full_path)
                    else:
                        os.remove(full_path)
                    cleaned_paths.append(path_rel)
                except Exception as e:
                    self.tasks[repo_path]["log_buffer"] += f"[builderTool] 清理旧产物失败 {path_rel}: {e}\n"
        if cleaned_paths:
            self.tasks[repo_path]["log_buffer"] += f"[builderTool] 清理旧打包产物: {', '.join(cleaned_paths)}\n"

    def _on_process_output(self, path: str):
        task = self.tasks[path]
        process = task["process"]
        if not process:
            return
        data = process.readAllStandardOutput().data()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("gbk", errors="ignore")
            
        # 追加到缓冲区
        task["log_buffer"] += text
        
        # 如果当前界面选中的是该项目，实时滚动渲染
        if self._get_current_selected_path() == path:
            self.log_view.moveCursor(self.log_view.textCursor().End)
            self.log_view.insertPlainText(text)
            self._scroll_log_to_bottom()

    def _on_process_finished(self, path: str, exit_code: int, exit_status):
        task = self.tasks[path]
        self.running_count -= 1
        
        # 1. 测定时间
        duration = task["elapsed"]
        duration_str = self.format_duration(duration)
        
        # 2. 判断状态
        if task["status"] == "aborted" or exit_status == QProcess.CrashExit:
            task["status"] = "aborted"
            status_text = "🛑 已中止"
        elif exit_code == 0:
            task["status"] = "success"
            status_text = "🟢 成功"
        else:
            task["status"] = "failed"
            status_text = f"🔴 失败 ({exit_code})"

        # 3. 追加打包产物超链接
        if task["status"] == "success":
            target_dir = path
            dist_path = os.path.join(path, "dist")
            disk_path = os.path.join(path, "disk")
            dist_disk_path = os.path.join(dist_path, "disk")
            if os.path.exists(dist_disk_path):
                target_dir = dist_disk_path
            elif os.path.exists(dist_path):
                target_dir = dist_path
            elif os.path.exists(disk_path):
                target_dir = disk_path
            
            path_url = QUrl.fromLocalFile(target_dir).toString()
            link_html = f'<br><b>[builderTool] 打包成功！产物输出路径:</b> <a href="{path_url}" style="color: #27ae60; font-weight: bold;">{target_dir}</a> (点击直达)<br>'
            task["log_buffer"] += f"\n[builderTool] 打包成功！输出路径: {target_dir}\n"
        else:
            link_html = ""

        # 4. 更新表格与颜色
        idx = self._find_row_by_path(path)
        if idx >= 0:
            self.table.setItem(idx, 2, QTableWidgetItem(status_text))
            self.table.setItem(idx, 3, QTableWidgetItem(duration_str))
            self._update_row_status_color(idx, task["status"])
            
        # 5. 如果当前选中此行，刷新显示
        if self._get_current_selected_path() == path:
            self.log_title.setText(f"项目日志：{task['name']} [{status_text}]")
            # 重新加载（因为添加了富文本超链接）
            self.log_view.setHtml(task["log_buffer"].replace("\n", "<br>") + link_html)
            self._scroll_log_to_bottom()
        
        # 更新总进度条
        finished_tasks = sum(1 for t in self.tasks.values() if t["status"] in ("success", "failed", "aborted"))
        self.progress_bar.setValue(finished_tasks)
        self._update_abort_button_state()
        
        # 检查是否全部任务完成
        if finished_tasks == len(self.tasks):
            from PyQt5.QtWidgets import QApplication
            # 1. 闪烁任务栏
            QApplication.alert(self)
            
            # 2. 弹出置顶的提示框
            msg = QMessageBox(self)
            msg.setWindowTitle("打包完成")
            success_count = sum(1 for t in self.tasks.values() if t["status"] == "success")
            failed_count = sum(1 for t in self.tasks.values() if t["status"] == "failed")
            aborted_count = sum(1 for t in self.tasks.values() if t["status"] == "aborted")
            
            summary_text = f"批量打包任务已全部完成！\n\n🟢 成功: {success_count} 个\n🔴 失败: {failed_count} 个"
            if aborted_count > 0:
                summary_text += f"\n🛑 中止: {aborted_count} 个"
                
            msg.setText(summary_text)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
            # 使用 non-blocking 或者 blocking exec_? exec_ 是 blocking 的，能够确保用户必须点击确认，有利于置顶提醒。
            msg.exec_()
        
        # 6. 继续调度队列
        self._schedule()

    def _abort_all(self, show_msg=True):
        """中止所有当前运行或等待中的打包进程"""
        # 清空排队队列
        self.queue.clear()
        
        # 中止所有正在运行的进程
        import subprocess
        for path, task in self.tasks.items():
            if task["status"] in ("building", "pending"):
                old_status = task["status"]
                task["status"] = "aborted"
                
                if old_status == "building" and task["process"]:
                    process = task["process"]
                    task["process"] = None
                    try:
                        process.finished.disconnect()
                    except Exception:
                        pass
                        
                    pid = process.processId()
                    if pid > 0:
                        try:
                            # Windows 下强杀进程树
                            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], 
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, 
                                           creationflags=0x08000000) # CREATE_NO_WINDOW
                        except Exception:
                            pass
                    try:
                        process.kill()
                    except Exception:
                        pass
                
                # 立即更新 UI 状态与颜色展示
                idx = self._find_row_by_path(path)
                if idx >= 0:
                    self.table.setItem(idx, 2, QTableWidgetItem("🛑 已中止"))
                    self._update_row_status_color(idx, "aborted")
                
                # 如果当前选中了该项目，更新日志标题与显示
                if self._get_current_selected_path() == path:
                    self.log_title.setText(f"项目日志：{task['name']} [🛑 已中止]")
        
        self.running_count = 0
        if show_msg:
            QMessageBox.information(self, "已中止", "所有打包任务已中止。")
        self._update_abort_button_state()

    def _on_abort_click(self):
        """中止按钮点击响应：如果任务在运行中则全部中止，否则直接关闭窗口"""
        any_building = any(t["status"] in ("building", "pending") for t in self.tasks.values())
        if any_building:
            self._abort_all(show_msg=True)
        else:
            self.close()

    def _update_abort_button_state(self):
        """根据当前任务状态动态更新中止/关闭按钮的外观与文字"""
        any_building = any(t["status"] in ("building", "pending") for t in self.tasks.values())
        if any_building:
            self.abort_all_btn.setText("🛑 全部中止")
            self.abort_all_btn.setStyleSheet("background-color: #c0392b; color: white; font-weight: bold; padding: 5px 12px;")
        else:
            self.abort_all_btn.setText("关闭")
            self.abort_all_btn.setStyleSheet("background-color: #7f8c8d; color: white; font-weight: bold; padding: 5px 12px;")

    def format_duration(self, seconds: int) -> str:
        """格式化秒数为人性化的分秒形式，如 79s -> 1m 19s"""
        if seconds < 60:
            return f"{seconds}s"
        m = seconds // 60
        s = seconds % 60
        return f"{m}m {s}s"

    def _on_tick(self):
        """每一秒更新一次正在打包的任务耗时展示"""
        for path, task in self.tasks.items():
            if task["status"] == "building":
                task["elapsed"] += 1
                idx = self._find_row_by_path(path)
                if idx >= 0:
                    formatted_time = self.format_duration(task["elapsed"])
                    self.table.setItem(idx, 3, QTableWidgetItem(formatted_time))

    def _on_row_changed(self):
        """当用户在左侧表格选择不同项目时，展示对应日志"""
        path = self._get_current_selected_path()
        if not path:
            return
        task = self.tasks[path]
        
        # 更新标题
        status_map = {
            "pending": "⏳ 排队中",
            "building": "⚙ 打包中...",
            "success": "🟢 成功",
            "failed": "🔴 失败",
            "aborted": "🛑 已中止"
        }
        status_text = status_map.get(task["status"], "未知")
        self.log_title.setText(f"项目日志：{task['name']} [{status_text}]")
        
        # 组装 HTML 日志流
        log_content = task["log_buffer"].replace("\n", "<br>")
        
        # 如果打包成功，附加上输出路径超链接
        if task["status"] == "success":
            target_dir = path
            dist_path = os.path.join(path, "dist")
            disk_path = os.path.join(path, "disk")
            dist_disk_path = os.path.join(dist_path, "disk")
            if os.path.exists(dist_disk_path):
                target_dir = dist_disk_path
            elif os.path.exists(dist_path):
                target_dir = dist_path
            elif os.path.exists(disk_path):
                target_dir = disk_path
            
            path_url = QUrl.fromLocalFile(target_dir).toString()
            log_content += f'<br><b>[builderTool] 打包成功！产物输出路径:</b> <a href="{path_url}" style="color: #27ae60; font-weight: bold;">{target_dir}</a> (点击直达)<br>'

        self.log_view.setHtml(log_content)
        self._scroll_log_to_bottom()

    def _on_table_double_clicked(self, index):
        """双击表格行打开成果目录"""
        row = index.row()
        item = self.table.item(row, 0)
        if not item:
            return
        path = item.data(Qt.UserRole)
        task = self.tasks.get(path)
        if task and task["status"] == "success":
            target_dir = path
            dist_path = os.path.join(path, "dist")
            disk_path = os.path.join(path, "disk")
            dist_disk_path = os.path.join(dist_path, "disk")
            if os.path.exists(dist_disk_path):
                target_dir = dist_disk_path
            elif os.path.exists(dist_path):
                target_dir = dist_path
            elif os.path.exists(disk_path):
                target_dir = disk_path
            
            if os.path.exists(target_dir):
                os.startfile(target_dir)

    def _on_log_link_clicked(self, url: QUrl):
        local_path = url.toLocalFile()
        if not local_path and url.scheme() == "file":
            local_path = url.path()
            if local_path.startswith("/") and os.name == "nt":
                local_path = local_path.lstrip("/")
        if not local_path:
            local_path = url.toString()
            if local_path.startswith("file:///"):
                local_path = local_path[8:]
                
        local_path = os.path.normpath(local_path)
        if os.path.exists(local_path):
            os.startfile(local_path)
        else:
            QDesktopServices.openUrl(url)

    def _scroll_log_to_bottom(self):
        self.log_view.moveCursor(self.log_view.textCursor().End)

    def _get_current_selected_path(self) -> str:
        selected_ranges = self.table.selectedRanges()
        if not selected_ranges:
            return ""
        row = selected_ranges[0].topRow()
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else ""

    def _find_row_by_path(self, path: str) -> int:
        for idx in range(self.table.rowCount()):
            item = self.table.item(idx, 0)
            if item and item.data(Qt.UserRole) == path:
                return idx
        return -1

    def _update_row_status_color(self, row_idx: int, status: str):
        color_map = {
            "pending": QColor(149, 165, 166, 60),    # 灰色
            "building": QColor(241, 196, 15, 65),   # 橙黄色
            "success": QColor(46, 204, 113, 65),    # 翠绿色
            "failed": QColor(231, 76, 60, 65),      # 暗红色
            "aborted": QColor(149, 165, 166, 120),  # 深灰
        }
        bg_color = color_map.get(status, QColor(0, 0, 0, 0))
        for col in range(self.table.columnCount()):
            item = self.table.item(row_idx, col)
            if item:
                item.setBackground(bg_color)

    def append_projects(self, project_paths: list):
        """动态追加打包项目到队列，如果存在且已结束则重置"""
        added_paths = []
        for path in project_paths:
            path = os.path.normpath(path)
            if path in self.tasks:
                task = self.tasks[path]
                if task["status"] in ("success", "building", "pending"):
                    # 已经是打包成功、打包中或排队中状态，在此弹层会话中无需重复打包
                    continue
                elif task["status"] in ("failed", "aborted"):
                    task["status"] = "pending"
                    task["elapsed"] = 0
                    task["log_buffer"] = f"[builderTool] 重新添加至队列排队打包\n"
                    idx = self._find_row_by_path(path)
                    if idx >= 0:
                        self.table.setItem(idx, 2, QTableWidgetItem("⏳ 排队中"))
                        self.table.setItem(idx, 3, QTableWidgetItem("0s"))
                        self._update_row_status_color(idx, "pending")
                    self.queue.append(path)
                    added_paths.append(path)
            else:
                name = os.path.basename(path)
                vue_ver, node_ver = self._detect_vue_and_node(path)
                self.tasks[path] = {
                    "name": name,
                    "vue": vue_ver,
                    "node": node_ver,
                    "status": "pending",
                    "process": None,
                    "log_buffer": "",
                    "start_time": None,
                    "elapsed": 0,
                    "timer": None
                }
                self.queue.append(path)
                added_paths.append(path)
                
                row_idx = self.table.rowCount()
                self.table.insertRow(row_idx)
                ver_str = f"Vue {vue_ver}" if vue_ver else "未知"
                item_name = QTableWidgetItem(name)
                item_name.setData(Qt.UserRole, path)
                self.table.setItem(row_idx, 0, item_name)
                self.table.setItem(row_idx, 1, QTableWidgetItem(ver_str))
                self.table.setItem(row_idx, 2, QTableWidgetItem("⏳ 排队中"))
                self.table.setItem(row_idx, 3, QTableWidgetItem("0s"))
                self._update_row_status_color(row_idx, "pending")
                
        if added_paths:
            self.progress_bar.setRange(0, len(self.tasks))
            finished_tasks = sum(1 for t in self.tasks.values() if t["status"] in ("success", "failed", "aborted"))
            self.progress_bar.setValue(finished_tasks)
            self._update_abort_button_state()
            self._schedule()

    def _show_table_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
            
        row = self.table.row(item)
        name_item = self.table.item(row, 0)
        if not name_item:
            return
            
        path = name_item.data(Qt.UserRole)
        task = self.tasks.get(path)
        if not task:
            return
            
        menu = QMenu(self)
        act_retry = menu.addAction("⚡ 重新打包当前项目")
        # 如果打包中或排队中，禁用重试
        if task["status"] in ("building", "pending"):
            act_retry.setEnabled(False)
            
        act_abort_single = None
        if task["status"] in ("building", "pending"):
            act_abort_single = menu.addAction("🛑 中止当前项目打包")
            
        act_open_dir = None
        if task["status"] == "success":
            act_open_dir = menu.addAction("📂 打开产物目录")
            
        action = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if not action:
            return
            
        if action is act_retry:
            self._retry_task(path)
        elif action is act_open_dir:
            self._open_task_dir(path)
        elif action is act_abort_single:
            self._abort_task(path)

    def _abort_task(self, path: str):
        if path not in self.tasks:
            return
            
        task = self.tasks[path]
        if task["status"] not in ("building", "pending"):
            return
            
        old_status = task["status"]
        task["status"] = "aborted"
        
        if old_status == "building":
            self.running_count -= 1
            if task["process"]:
                process = task["process"]
                task["process"] = None
                try:
                    process.finished.disconnect()
                except Exception:
                    pass
                pid = process.processId()
                if pid > 0:
                    import subprocess
                    try:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], 
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, 
                                       creationflags=0x08000000)
                    except Exception:
                        pass
                try:
                    process.kill()
                except Exception:
                    pass
        elif old_status == "pending":
            if path in self.queue:
                self.queue.remove(path)
                
        # 更新 UI
        idx = self._find_row_by_path(path)
        if idx >= 0:
            self.table.setItem(idx, 2, QTableWidgetItem("🛑 已中止"))
            self._update_row_status_color(idx, "aborted")
            
        # 刷新总进度条值
        finished_tasks = sum(1 for t in self.tasks.values() if t["status"] in ("success", "failed", "aborted"))
        self.progress_bar.setValue(finished_tasks)
        
        self._update_abort_button_state()
        
        if self._get_current_selected_path() == path:
            self.log_title.setText(f"项目日志：{task['name']} [🛑 已中止]")
            
        # 触发调度
        self._schedule()
        
        # 检查是否因为中止这一个，导致所有的任务都完成了
        if finished_tasks == len(self.tasks):
            from PyQt5.QtWidgets import QApplication
            QApplication.alert(self)
            
            msg = QMessageBox(self)
            msg.setWindowTitle("打包完成")
            success_count = sum(1 for t in self.tasks.values() if t["status"] == "success")
            failed_count = sum(1 for t in self.tasks.values() if t["status"] == "failed")
            aborted_count = sum(1 for t in self.tasks.values() if t["status"] == "aborted")
            
            summary_text = f"批量打包任务已全部完成！\n\n🟢 成功: {success_count} 个\n🔴 失败: {failed_count} 个"
            if aborted_count > 0:
                summary_text += f"\n🛑 中止: {aborted_count} 个"
                
            msg.setText(summary_text)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
            msg.exec_()

    def _retry_task(self, path: str):
        if path not in self.tasks:
            return
            
        task = self.tasks[path]
        if task["status"] in ("building", "pending"):
            return
            
        # 重置状态
        task["status"] = "pending"
        task["elapsed"] = 0
        task["log_buffer"] = f"[builderTool] 重新开始打包项目: {task['name']}\n"
        
        # 更新表格行
        idx = self._find_row_by_path(path)
        if idx >= 0:
            self.table.setItem(idx, 2, QTableWidgetItem("⏳ 排队中"))
            self.table.setItem(idx, 3, QTableWidgetItem("0s"))
            self._update_row_status_color(idx, "pending")
            
        # 重新排队
        self.queue.append(path)
        
        # 刷新总进度条范围和值
        self.progress_bar.setRange(0, len(self.tasks))
        finished_tasks = sum(1 for t in self.tasks.values() if t["status"] in ("success", "failed", "aborted"))
        self.progress_bar.setValue(finished_tasks)
        
        self._update_abort_button_state()
        
        # 如果当前选中了该项目，刷新显示其日志
        if self._get_current_selected_path() == path:
            self.log_title.setText(f"项目日志：{task['name']} [⏳ 排队中]")
            self.log_view.setPlainText(task["log_buffer"])
            self._scroll_log_to_bottom()
            
        self._schedule()

    def _open_task_dir(self, path: str):
        target_dir = path
        dist_path = os.path.join(path, "dist")
        disk_path = os.path.join(path, "disk")
        dist_disk_path = os.path.join(dist_path, "disk")
        if os.path.exists(dist_disk_path):
            target_dir = dist_disk_path
        elif os.path.exists(dist_path):
            target_dir = dist_path
        elif os.path.exists(disk_path):
            target_dir = disk_path
        
        if os.path.exists(target_dir):
            try:
                os.startfile(target_dir)
            except Exception as e:
                QMessageBox.warning(self, "错误", f"无法打开目录:\n{e}")

    def close_and_terminate(self):
        """由主窗口关闭时强行终止并真正关闭"""
        self.refresh_timer.stop()
        self._abort_all(show_msg=False)
        self._force_close = True
        self.close()

    def closeEvent(self, event):
        """关闭时如果仍有打包任务，不终止，而是隐藏窗口在后台继续运行"""
        if getattr(self, "_force_close", False):
            event.accept()
            return
            
        any_building = any(t["status"] in ("building", "pending") for t in self.tasks.values())
        if any_building:
            self.hide()
            event.ignore()
            if self.parent() and hasattr(self.parent(), "statusBar"):
                self.parent().statusBar().showMessage("批量打包控制台已转入后台持续运行...", 5000)
        else:
            self.refresh_timer.stop()
            self._abort_all(show_msg=False)
            event.accept()
