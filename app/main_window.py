"""主窗口：左侧项目列表 + 右侧分支操作/常用操作/新建分支/IP替换/日志。"""
import os
from datetime import datetime

from PyQt5.QtCore import Qt, QUrl, QProcess, QProcessEnvironment, QDate, QMimeData
from PyQt5.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QRadioButton, QSplitter, QTextBrowser, QTextEdit,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QStackedWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QDateEdit,
)

from . import config as cfg_mod
from . import git_ops, ip_replace
from .worker import Worker


# ─────────────────────────────────────────────────────────────────────────────
# 项目树控件：支持分组折叠 + 组内/跨组拖拽排序
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_GROUP = "未分组"
_GROUP_ROLE   = Qt.UserRole + 10   # 标记节点是否为分组头


class ProjectTreeWidget(QTreeWidget):
    """替代 QListWidget 的项目分组树：
    - 叶节点：Qt.UserRole = 项目名, checkable
    - 根节点（分组头）：_GROUP_ROLE = True, 不可 check, 可折叠
    - 支持组内/跨组拖拽重排
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setIndentation(14)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setExpandsOnDoubleClick(False)   # 双击留给外部选中项目
        self.setStyleSheet("""
            QTreeWidget { border: 1px solid #555; border-radius: 4px; }
            QTreeWidget::item { padding: 4px 4px; }
            QTreeWidget::item:hover { background: #1769d6; color: white; }
            QTreeWidget::item:selected {
                background: #1769d6;
                color: white;
                font-weight: bold;
                border-left: 3px solid #ff9800;
            }
        """)

    # ── 拖拽：只允许叶节点（项目）被拖拽，分组头不可拖 ──────────────────────
    def startDrag(self, supportedActions):
        item = self.currentItem()
        if item and not item.data(0, _GROUP_ROLE):
            super().startDrag(supportedActions)

    def dropEvent(self, event):
        target = self.itemAt(event.pos())
        dragged = self.currentItem()
        if not dragged or dragged.data(0, _GROUP_ROLE):
            event.ignore()
            return

        # 确定目标分组根节点
        if target is None:
            dest_root = self.invisibleRootItem().child(self.invisibleRootItem().childCount() - 1)
        elif target.data(0, _GROUP_ROLE):
            dest_root = target
        else:
            dest_root = target.parent() or self.invisibleRootItem()

        if dest_root is None:
            event.ignore()
            return

        # 从原分组移除
        src_root = dragged.parent() or self.invisibleRootItem()
        src_root.removeChild(dragged)

        # 插入目标分组
        if target and not target.data(0, _GROUP_ROLE) and target.parent() == dest_root:
            idx = dest_root.indexOfChild(target)
            dest_root.insertChild(idx, dragged)
        else:
            dest_root.addChild(dragged)

        dest_root.setExpanded(True)
        self.setCurrentItem(dragged)
        event.accept()

    # ── 辅助：遍历所有叶节点（项目条目）────────────────────────────────────
    def all_project_items(self):
        """返回所有叶节点 QTreeWidgetItem 列表。"""
        result = []
        root = self.invisibleRootItem()
        for gi in range(root.childCount()):
            grp = root.child(gi)
            for pi in range(grp.childCount()):
                result.append(grp.child(pi))
        return result

    def find_or_create_group(self, group_name: str) -> QTreeWidgetItem:
        """按名称找分组根节点，不存在则创建。"""
        root = self.invisibleRootItem()
        for i in range(root.childCount()):
            g = root.child(i)
            if g.text(0) == group_name:
                return g
        return self._create_group(group_name)

    def _create_group(self, group_name: str) -> QTreeWidgetItem:
        grp = QTreeWidgetItem(self)
        grp.setText(0, group_name)
        grp.setData(0, _GROUP_ROLE, True)
        grp.setFlags(Qt.ItemIsEnabled)   # 分组头：不可选、不可 check
        grp.setExpanded(True)
        f = grp.font(0)
        f.setBold(True)
        grp.setFont(0, f)
        return grp

    def group_names(self) -> list:
        root = self.invisibleRootItem()
        return [root.child(i).text(0) for i in range(root.childCount())]

    def dump_order(self) -> dict:
        """导出当前分组结构 {group: [proj, ...]} 用于持久化。"""
        result = {}
        root = self.invisibleRootItem()
        for gi in range(root.childCount()):
            grp = root.child(gi)
            name = grp.text(0)
            result[name] = [
                grp.child(pi).data(0, Qt.UserRole)
                for pi in range(grp.childCount())
            ]
        return result


class CommitDialog(QDialog):
    """commit 确认弹窗：展示 status + 最近 log，填写 message。"""

    def __init__(self, status_text: str, log_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认提交 (git commit)")
        self.resize(640, 480)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("暂存区中将提交的修改（git diff --cached --name-status）："))
        status_box = QPlainTextEdit(status_text or "(无修改)")
        status_box.setReadOnly(True)
        layout.addWidget(status_box, 3)

        layout.addWidget(QLabel("最近 3 条提交记录："))
        log_box = QPlainTextEdit(log_text or "(无记录)")
        log_box.setReadOnly(True)
        log_box.setMaximumHeight(90)
        layout.addWidget(log_box, 1)

        layout.addWidget(QLabel("Commit message："))
        self.msg_edit = QLineEdit()
        self.msg_edit.setPlaceholderText("例如：feat: 调整吉林农大配置")
        layout.addWidget(self.msg_edit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_ok(self):
        if not self.msg_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写 commit message")
            return
        self.accept()

    def message(self) -> str:
        return self.msg_edit.text().strip()


class QuickPublishDialog(QDialog):
    """一键发布弹窗：展示所有变更（包含未暂存与已暂存），填写 message，确认后一次性执行 add + commit + push。"""

    def __init__(self, changes_text: str, log_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("一键提交并推送 (Add -> Commit -> Push)")
        self.resize(640, 480)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("将要暂存并提交的文件变更（git status --short）："))
        status_box = QPlainTextEdit(changes_text or "(无任何修改)")
        status_box.setReadOnly(True)
        layout.addWidget(status_box, 3)

        layout.addWidget(QLabel("最近 3 条提交记录："))
        log_box = QPlainTextEdit(log_text or "(无记录)")
        log_box.setReadOnly(True)
        log_box.setMaximumHeight(90)
        layout.addWidget(log_box, 1)

        layout.addWidget(QLabel("Commit message："))
        self.msg_edit = QLineEdit()
        self.msg_edit.setPlaceholderText("例如：feat: 调整配置并发布")
        layout.addWidget(self.msg_edit)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("一键提交推送")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_ok(self):
        if not self.msg_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写 commit message")
            return
        self.accept()

    def message(self) -> str:
        return self.msg_edit.text().strip()


class CloneDialog(QDialog):
    """克隆仓库弹窗：仓库地址 + 目标目录。"""

    def __init__(self, default_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("克隆仓库 (git clone)")
        self.resize(560, 150)
        grid = QGridLayout(self)

        grid.addWidget(QLabel("仓库地址："), 0, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://... 或 git@...")
        grid.addWidget(self.url_edit, 0, 1, 1, 2)

        grid.addWidget(QLabel("目标目录："), 1, 0)
        self.dir_edit = QLineEdit(default_dir)
        grid.addWidget(self.dir_edit, 1, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        grid.addWidget(browse, 1, 2)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        grid.addWidget(btns, 2, 0, 1, 3)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "选择目标目录", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _on_ok(self):
        if not self.url_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写仓库地址")
            return
        if not os.path.isdir(self.dir_edit.text().strip()):
            QMessageBox.warning(self, "提示", "目标目录不存在")
            return
        self.accept()

    def values(self):
        return self.url_edit.text().strip(), self.dir_edit.text().strip()



class BranchConfigDialog(QDialog):
    """新建分支配置选择对话框：产业/实训模块选择，生成备注标签。"""

    EDU_PRODUCT_LINES = [
        "教育产品线-数据分析软件",
        "教育产品线-可视化软件",
    ]
    EDU_PRODUCT_LABELS = {
        "教育产品线-数据分析软件": "数据分析软件",
        "教育产品线-可视化软件": "可视化软件",
    }

    # (分类名, 序号, [(模块名, 序号), ...])
    SHIXUN_GROUPS = [
        ("其他",  1, [("数据中心", 1), ("传播分析", 1), ("报告中心", 1)]),
        ("舆情",  2, [("舆情监测", 2), ("游客满意度", 2),
                     ("24版舆情监测", 2), ("景区游客满意度", 2)]),
        ("客流",  3, [("实时客流监测", 3), ("客流趋势分析", 3),
                     ("客流分布分析", 3), ("游客行为画像", 3)]),
        ("消费",  4, [("涉旅消费分析", 4), ("游客消费画像", 4)]),
    ]

    def __init__(self, parent=None, initial_note: str = ""):
        super().__init__(parent)
        self.setWindowTitle("选择分支配置模块")
        self.setMinimumWidth(680)
        self._build_ui()
        if initial_note:
            self._restore(initial_note)
        self._update_preview()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── 顶层选择 ──────────────────────────────────────────────────────
        top_grp = QGroupBox("包含范围")
        top_grid = QGridLayout(top_grp)
        self.chk_chanye  = QCheckBox("产业（教学）")
        self.chk_shixun  = QCheckBox("实训（大屏）")
        self.chk_chanye.stateChanged.connect(self._update_preview)
        self.chk_shixun.stateChanged.connect(self._toggle_shixun)
        top_grid.addWidget(self.chk_chanye, 0, 0)
        sep_scope = QLabel("|")
        sep_scope.setAlignment(Qt.AlignCenter)
        top_grid.addWidget(sep_scope, 0, 1)
        top_grid.addWidget(self.chk_shixun, 0, 2)
        sep_product = QLabel("|")
        sep_product.setAlignment(Qt.AlignCenter)
        top_grid.addWidget(sep_product, 0, 3)

        self.edu_product_checks = []
        top_grid.addWidget(QLabel("教育产品线"), 0, 4)
        for idx, product_name in enumerate(self.EDU_PRODUCT_LINES, start=5):
            chk = QCheckBox(self.EDU_PRODUCT_LABELS.get(product_name, product_name))
            chk.setProperty("note_value", product_name)
            chk.stateChanged.connect(self._update_preview)
            self.edu_product_checks.append(chk)
            top_grid.addWidget(chk, 0, idx)
        top_grid.setColumnStretch(7, 1)
        layout.addWidget(top_grp)

        # ── 实训子模块（默认隐藏）────────────────────────────────────────
        self.shixun_panel = QWidget()
        panel_layout = QVBoxLayout(self.shixun_panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(4)

        self.module_checks: dict[str, "QCheckBox"] = {}   # 模块名 -> checkbox
        self._all_checks: list[tuple] = []                 # (全选chk, [子chk])

        for cat_name, cat_order, modules in self.SHIXUN_GROUPS:
            grp = QGroupBox(f"{cat_order}. {cat_name}")
            row = QHBoxLayout(grp)
            row.setSpacing(10)

            chk_all = QCheckBox("全选")
            row.addWidget(chk_all)

            sep = QLabel("│")
            sep.setFixedWidth(10)
            row.addWidget(sep)

            sub_checks = []
            for mod_name, mod_order in modules:
                chk = QCheckBox(f"{mod_order}-{mod_name}")
                chk.stateChanged.connect(self._update_preview)
                chk.stateChanged.connect(lambda s, a=chk_all, sc=sub_checks: self._sync_all(a, sc))
                self.module_checks[mod_name] = chk
                row.addWidget(chk)
                sub_checks.append(chk)

            def _make_toggle(checks):
                def toggle(state):
                    for c in checks:
                        c.blockSignals(True)
                        c.setChecked(state == Qt.Checked)
                        c.blockSignals(False)
                    self._update_preview()
                return toggle

            chk_all.stateChanged.connect(_make_toggle(sub_checks))
            self._all_checks.append((chk_all, sub_checks))
            row.addStretch()
            panel_layout.addWidget(grp)

        self.shixun_panel.setVisible(False)
        layout.addWidget(self.shixun_panel)

        # ── 预览 ─────────────────────────────────────────────────────────
        prev_grp = QGroupBox("生成的备注（Tab 标签）")
        prev_row = QHBoxLayout(prev_grp)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        prev_row.addWidget(self.preview_label)
        layout.addWidget(prev_grp)

        # ── 按钮 ─────────────────────────────────────────────────────────
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btn_box.button(QDialogButtonBox.Ok).setText("确定")
        btn_box.button(QDialogButtonBox.Cancel).setText("取消")
        btn_box.accepted.connect(self._on_ok)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def _toggle_shixun(self, state):
        self.shixun_panel.setVisible(state == Qt.Checked)
        self.adjustSize()
        self._update_preview()

    def _sync_all(self, all_chk, sub_checks):
        """根据子项状态同步全选 checkbox（不触发全选信号）。"""
        checked = sum(1 for c in sub_checks if c.isChecked())
        all_chk.blockSignals(True)
        all_chk.setChecked(checked == len(sub_checks))
        all_chk.blockSignals(False)

    def _update_preview(self):
        parts = []
        if self.chk_chanye.isChecked():
            parts.append("产业（教学）")
        parts.extend(
            chk.property("note_value") or chk.text()
            for chk in getattr(self, "edu_product_checks", [])
            if chk.isChecked()
        )
        if self.chk_shixun.isChecked():
            group_parts = []
            for cat_name, cat_order, modules in self.SHIXUN_GROUPS:
                selected_in_cat = [
                    mod_name for mod_name, _ in modules
                    if self.module_checks[mod_name].isChecked()
                ]
                if selected_in_cat:
                    group_parts.append(f"[实训-{cat_name}]{'，'.join(selected_in_cat)}")
            if group_parts:
                parts.append("实训：" + " | ".join(group_parts))
            else:
                parts.append("实训（大屏）")
        text = " | ".join(parts) if parts else "（未选择任何模块）"
        self.preview_label.setText(text)

    def _on_ok(self):
        has_edu_product = any(chk.isChecked() for chk in getattr(self, "edu_product_checks", []))
        if not self.chk_chanye.isChecked() and not self.chk_shixun.isChecked() and not has_edu_product:
            QMessageBox.warning(self, "提示", "请至少选择一项配置")
            return
        self.accept()

    def get_note(self) -> str:
        return self.preview_label.text()

    def _restore(self, note: str):
        """从已保存的备注字符串还原选中状态。"""
        if "产业（教学）" in note:
            self.chk_chanye.setChecked(True)
        for chk in getattr(self, "edu_product_checks", []):
            note_value = chk.property("note_value") or chk.text()
            if note_value in note or chk.text() in note:
                chk.setChecked(True)
        if "实训" in note:
            self.chk_shixun.setChecked(True)
            for mod_name, chk in self.module_checks.items():
                if mod_name in note:
                    chk.setChecked(True)


class ImagePreviewDialog(QDialog):
    def __init__(self, image_path, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self._delete_callback = None
        self.setWindowTitle("图片预览")
        self.setMinimumSize(600, 450)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            self.label.setText("图片加载失败，请检查文件是否存在")
            self.label.setStyleSheet("color: red; font-size: 14px;")
        else:
            scaled_pixmap = pixmap.scaled(800, 600, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.label.setPixmap(scaled_pixmap)
            
        layout.addWidget(self.label, 1)
        
        btn_box = QHBoxLayout()
        btn_open_external = QPushButton("🌐 使用系统查看器打开")
        btn_open_external.setStyleSheet("background-color: #1769d6; color: white; padding: 6px 12px; border: none; border-radius: 4px;")
        btn_open_external.clicked.connect(lambda: self._open_external(image_path))
        btn_box.addWidget(btn_open_external)

        self.btn_delete = QPushButton("🗑 删除当前")
        self.btn_delete.setStyleSheet("background-color: #c62828; color: white; padding: 6px 12px; border: none; border-radius: 4px;")
        self.btn_delete.clicked.connect(self._delete_current)
        self.btn_delete.setVisible(False)
        btn_box.addWidget(self.btn_delete)
        
        btn_close = QPushButton("关闭")
        btn_close.setStyleSheet("background-color: #333333; color: white; padding: 6px 12px; border: none; border-radius: 4px;")
        btn_close.clicked.connect(self.accept)
        btn_box.addWidget(btn_close)
        
        layout.addLayout(btn_box)

    def _open_external(self, path):
        import os
        try:
            os.startfile(os.path.abspath(path))
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开文件: {e}")

    def set_delete_callback(self, callback):
        self._delete_callback = callback
        self.btn_delete.setVisible(True)

    def _delete_current(self):
        if not self._delete_callback:
            return
        if QMessageBox.question(self, "确认删除", f"确定删除当前图片吗？\n\n{os.path.basename(self.image_path)}") != QMessageBox.Yes:
            return
        if self._delete_callback(self.image_path):
            self.accept()


class MultiImagePreviewDialog(QDialog):
    def __init__(self, image_paths, parent=None):
        super().__init__(parent)
        self.image_paths = list(image_paths)
        self.setWindowTitle("图片预览")
        self.setMinimumSize(780, 520)
        self._delete_callback = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self.list_widget = QListWidget()
        self.list_widget.setMaximumWidth(240)
        for path in self.image_paths:
            self.list_widget.addItem(os.path.basename(path))
        self.list_widget.currentRowChanged.connect(self._show_image)
        layout.addWidget(self.list_widget)

        right = QVBoxLayout()
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        right.addWidget(self.label, 1)

        btn_box = QHBoxLayout()
        self.btn_open_external = QPushButton("🌐 使用系统查看器打开")
        self.btn_open_external.setStyleSheet("background-color: #1769d6; color: white; padding: 6px 12px; border: none; border-radius: 4px;")
        self.btn_open_external.clicked.connect(self._open_current)
        btn_box.addWidget(self.btn_open_external)

        self.btn_delete = QPushButton("🗑 删除当前")
        self.btn_delete.setStyleSheet("background-color: #c62828; color: white; padding: 6px 12px; border: none; border-radius: 4px;")
        self.btn_delete.clicked.connect(self._delete_current)
        btn_box.addWidget(self.btn_delete)

        btn_close = QPushButton("关闭")
        btn_close.setStyleSheet("background-color: #333333; color: white; padding: 6px 12px; border: none; border-radius: 4px;")
        btn_close.clicked.connect(self.accept)
        btn_box.addWidget(btn_close)
        right.addLayout(btn_box)
        layout.addLayout(right, 1)

        if self.image_paths:
            self.list_widget.setCurrentRow(0)

    def _show_image(self, row):
        if row < 0 or row >= len(self.image_paths):
            self.label.clear()
            return
        path = self.image_paths[row]
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.label.setText("图片加载失败，请检查文件是否存在")
            self.label.setStyleSheet("color: red; font-size: 14px;")
        else:
            self.label.setStyleSheet("")
            self.label.setPixmap(pixmap.scaled(900, 650, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _open_current(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.image_paths):
            return
        try:
            os.startfile(os.path.abspath(self.image_paths[row]))
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开文件: {e}")

    def set_delete_callback(self, callback):
        self._delete_callback = callback

    def _delete_current(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self.image_paths):
            return
        path = self.image_paths[row]
        if QMessageBox.question(self, "确认删除", f"确定删除当前图片吗？\n\n{os.path.basename(path)}") != QMessageBox.Yes:
            return
        if self._delete_callback and self._delete_callback(path):
            self.image_paths.pop(row)
            self.list_widget.takeItem(row)
            if self.image_paths:
                self.list_widget.setCurrentRow(min(row, len(self.image_paths) - 1))
            else:
                self.label.clear()


class IpCellWidget(QWidget):
    def __init__(self, ip_text, main_win, global_idx):
        super().__init__()
        self.main_win = main_win
        self.global_idx = global_idx
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        
        self.edit = QLineEdit(ip_text)
        self.edit.setStyleSheet("""
            QLineEdit {
                border: none;
                background-color: transparent;
                color: inherit;
                padding: 2px;
            }
            QLineEdit:focus {
                border: 1px solid #1769d6;
                background-color: rgba(23, 105, 214, 0.1);
            }
        """)
        self.edit.editingFinished.connect(self.save_ip)
        layout.addWidget(self.edit, 1)
        
        self.copy_btn = QPushButton("📋")
        self.copy_btn.setToolTip("复制IP地址到剪贴板")
        self.copy_btn.setFixedWidth(28)
        self.copy_btn.setFixedHeight(24)
        self.copy_btn.setStyleSheet("""
            QPushButton {
                border: none;
                background-color: transparent;
                border-radius: 3px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(128, 128, 128, 0.2);
            }
        """)
        self.copy_btn.clicked.connect(self.copy_ip)
        layout.addWidget(self.copy_btn)
        
    def copy_ip(self):
        from PyQt5.QtWidgets import QApplication
        clipboard = QApplication.clipboard()
        clipboard.setText(self.edit.text().strip())
        self.main_win.statusBar().showMessage("IP地址已复制到剪贴板", 2000)
        self.main_win.log(f"已复制IP地址: {self.edit.text().strip()}", self.main_win.LOG_COLORS["hint"])
        
    def save_ip(self):
        val = self.edit.text().strip()
        if self.global_idx < len(self.main_win.teach_data):
            self.main_win.teach_data[self.global_idx]["ip"] = val
            cfg_mod.save_config(self.main_win.cfg)

class ConfigTagsCellWidget(QWidget):
    def __init__(self, note_str, main_win, parent=None):
        super().__init__(parent)
        self.note_str = note_str or ""
        self.main_win = main_win
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        
        try:
            with open("debug_tags.txt", "a", encoding="utf-8") as f:
                f.write(f"note_str: {repr(self.note_str)}\n")
                f.write(f"chanye: {repr(' 产业 ')} {[hex(ord(c)) for c in ' 产业 ']}\n")
                f.write(f"shixun: {repr(' 实训 ')} {[hex(ord(c)) for c in ' 实训 ']}\n")
        except Exception as e:
            pass

        
        has_chanye = "产业（教学）" in self.note_str
        has_edu_product = "教育产品线-" in self.note_str
        has_shixun = "实训" in self.note_str
        
        theme = getattr(main_win, "current_theme", "dark")
        
        if not has_chanye and not has_edu_product and not has_shixun:
            lbl_none = QLabel("—")
            lbl_none.setStyleSheet("color: #777777;")
            layout.addWidget(lbl_none)
            return

        if has_chanye:
            lbl_chanye = QLabel(" 产业 ")
            lbl_chanye.setToolTip("已选择模块：产业（教学）")
            lbl_chanye.setAlignment(Qt.AlignCenter)
            if theme == "light":
                lbl_chanye.setStyleSheet("""
                    QLabel {
                        background-color: #e8f5e9;
                        color: #2e7d32;
                        border: 1px solid #c8e6c9;
                        border-radius: 4px;
                        font-weight: normal;
                        font-size: 12px;
                        padding: 3px 6px;
                    }
                """)
            else:
                lbl_chanye.setStyleSheet("""
                    QLabel {
                        background-color: #1b5e20;
                        color: #c8e6c9;
                        border: 1px solid #2e7d32;
                        border-radius: 4px;
                        font-weight: normal;
                        font-size: 12px;
                        padding: 3px 6px;
                    }
                """)
            layout.addWidget(lbl_chanye)

        if has_edu_product:
            btn_edu = QPushButton(" 教育 ")
            btn_edu.setCursor(Qt.PointingHandCursor)
            btn_edu.setToolTip("已选择教育产品线。点击查看详情…")
            if theme == "light":
                btn_edu.setStyleSheet("""
                    QPushButton {
                        background-color: #fff8e1;
                        color: #ef6c00;
                        border: 1px solid #ffe0b2;
                        border-radius: 4px;
                        font-weight: bold;
                        font-size: 12px;
                        padding: 3px 6px;
                    }
                    QPushButton:hover {
                        background-color: #ef6c00;
                        color: white;
                        border-color: #ef6c00;
                    }
                """)
            else:
                btn_edu.setStyleSheet("""
                    QPushButton {
                        background-color: #5d4037;
                        color: #ffe0b2;
                        border: 1px solid #8d6e63;
                        border-radius: 4px;
                        font-weight: bold;
                        font-size: 12px;
                        padding: 3px 6px;
                    }
                    QPushButton:hover {
                        background-color: #8d6e63;
                        color: white;
                        border-color: #a1887f;
                    }
                """)
            btn_edu.clicked.connect(self._show_edu_details)
            layout.addWidget(btn_edu)
            
        if has_shixun:
            btn_shixun = QPushButton(" 实训 ")
            btn_shixun.setCursor(Qt.PointingHandCursor)
            btn_shixun.setToolTip("已选择模块：实训（大屏）。点击查看具体子模块…")
            if theme == "light":
                btn_shixun.setStyleSheet("""
                    QPushButton {
                        background-color: #e3f2fd;
                        color: #1565c0;
                        border: 1px solid #bbdefb;
                        border-radius: 4px;
                        font-weight: bold;
                        font-size: 12px;
                        padding: 3px 6px;
                    }
                    QPushButton:hover {
                        background-color: #1565c0;
                        color: white;
                        border-color: #1565c0;
                    }
                """)
            else:
                btn_shixun.setStyleSheet("""
                    QPushButton {
                        background-color: #0d47a1;
                        color: #bbdefb;
                        border: 1px solid #1565c0;
                        border-radius: 4px;
                        font-weight: bold;
                        font-size: 12px;
                        padding: 3px 6px;
                    }
                    QPushButton:hover {
                        background-color: #1565c0;
                        color: white;
                        border-color: #1565c0;
                    }
                """)
            btn_shixun.clicked.connect(self._show_details)
            layout.addWidget(btn_shixun)
            
    def _show_details(self):
        shixun_detail = self.main_win._format_shixun_detail(self.note_str)
        QMessageBox.information(self, "实训（大屏）配置详情", shixun_detail)

    def _show_edu_details(self):
        edu_detail = self.main_win._format_chanye_detail(self.note_str)
        QMessageBox.information(self, "教育产品线配置详情", edu_detail)



class ImageCellWidget(QWidget):
    def __init__(self, image_paths, main_win, global_idx):
        super().__init__()
        self.image_paths = self._normalize_paths(image_paths)
        self.main_win = main_win
        self.global_idx = global_idx
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        
        self.btn_upload = QPushButton("📤")
        self.btn_upload.setToolTip("上传图片")
        self.btn_upload.setFixedWidth(36)
        self.btn_upload.setStyleSheet("""
            QPushButton {
                background-color: #333333;
                color: #cccccc;
                border: 1px solid #555555;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #444444;
                color: white;
            }
        """)
        self.btn_upload.clicked.connect(self.upload_image)
        layout.addWidget(self.btn_upload)
        
        self.btn_view = QPushButton("👁️")
        self.btn_view.setToolTip("查看图片")
        self.btn_view.setFixedWidth(36)
        self.btn_view.setStyleSheet("""
            QPushButton {
                background-color: #2e7d32;
                color: white;
                border: none;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #388e3c;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
        """)
        self.btn_view.setEnabled(bool(self.image_paths))
        self.btn_view.clicked.connect(self.view_image)
        layout.addWidget(self.btn_view)
        
        self.btn_del = QPushButton("🗑️")
        self.btn_del.setToolTip("清空该记录的全部图片")
        self.btn_del.setFixedWidth(36)
        self.btn_del.setStyleSheet("""
            QPushButton {
                background-color: #c62828;
                color: white;
                border: none;
                border-radius: 3px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #d32f2f;
            }
            QPushButton:disabled {
                background-color: #555555;
                color: #888888;
            }
        """)
        self.btn_del.setEnabled(bool(self.image_paths))
        self.btn_del.clicked.connect(self.delete_image)
        layout.addWidget(self.btn_del)
        
        count = len(self.image_paths)
        self.lbl_info = QLabel(f"{count}张" if count else "未上传")
        self.lbl_info.setStyleSheet("color: #4caf50; font-size: 11px;" if count else "color: #888888; font-size: 11px;")
        layout.addWidget(self.lbl_info)
        layout.addStretch()

    @staticmethod
    def _normalize_paths(value):
        if isinstance(value, list):
            return [str(p) for p in value if str(p).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def upload_image(self):
        from PyQt5.QtWidgets import QFileDialog
        import shutil
        import time
        
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "选择部署图片（可多选）", "", "图片文件 (*.png *.jpg *.jpeg *.gif *.bmp)"
        )
        if not file_paths:
            return
            
        # Create assets/teach_images folder in workspace
        target_dir = os.path.join(self.main_win.work_dir(), "assets", "teach_images")
        if not os.path.exists(target_dir):
            try:
                os.makedirs(target_dir)
            except Exception as e:
                # If workspace dir is invalid, use app directory
                target_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "teach_images")
                if not os.path.exists(target_dir):
                    os.makedirs(target_dir)
                    
        try:
            rel_paths = []
            for idx, file_path in enumerate(file_paths):
                ext = os.path.splitext(file_path)[1]
                filename = f"img_{int(time.time())}_{idx}{ext}"
                dest_path = os.path.join(target_dir, filename)
                shutil.copy(file_path, dest_path)
                rel_paths.append(os.path.relpath(dest_path, self.main_win.work_dir()))
            
            # Update database
            if self.global_idx < len(self.main_win.teach_data):
                item = self.main_win.teach_data[self.global_idx]
                current_paths = self._normalize_paths(item.get("image_paths", item.get("image_path", "")))
                current_paths.extend(rel_paths)
                item["image_paths"] = current_paths
                item["image_path"] = current_paths[0] if current_paths else ""
                cfg_mod.save_config(self.main_win.cfg)
                self.main_win.log(f"已上传 {len(rel_paths)} 张图片", self.main_win.LOG_COLORS["ok"])
                
                # Refresh table
                self.main_win._render_teach_table()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"图片保存失败: {e}")

    def view_image(self):
        if not self.image_paths:
            return
        self._image_changed_in_preview = False
        full_paths = [os.path.join(self.main_win.work_dir(), p) for p in self.image_paths]
        missing = [p for p, full in zip(self.image_paths, full_paths) if not os.path.exists(full)]
        full_paths = [full for full in full_paths if os.path.exists(full)]
        if missing:
            QMessageBox.warning(self, "警告", "以下图片文件不存在：\n\n" + "\n".join(missing[:10]))
        if not full_paths:
            return
        if len(full_paths) == 1:
            dlg = ImagePreviewDialog(full_paths[0], self)
            dlg.set_delete_callback(self._delete_preview_image)
            dlg.exec_()
        else:
            dlg = MultiImagePreviewDialog(full_paths, self)
            dlg.set_delete_callback(self._delete_preview_image)
            dlg.exec_()
        if getattr(self, "_image_changed_in_preview", False):
            self.main_win._render_teach_table()

    def _delete_preview_image(self, full_path: str) -> bool:
        if self.global_idx >= len(self.main_win.teach_data):
            return False
        item = self.main_win.teach_data[self.global_idx]
        paths = self._normalize_paths(item.get("image_paths", item.get("image_path", "")))
        if not paths:
            return False
        delete_path = None
        for path in paths:
            if os.path.abspath(os.path.join(self.main_win.work_dir(), path)) == os.path.abspath(full_path):
                delete_path = path
                break
        if not delete_path:
            return False
        paths = [path for path in paths if path != delete_path]
        item["image_paths"] = paths
        item["image_path"] = paths[0] if paths else ""
        try:
            if os.path.exists(full_path):
                os.remove(full_path)
        except OSError as e:
            self.main_win.log(f"图片记录已删除，但文件删除失败：{delete_path} - {e}", self.main_win.LOG_COLORS["err"])
        cfg_mod.save_config(self.main_win.cfg)
        self.main_win.log(f"已删除部署图片：{delete_path}", self.main_win.LOG_COLORS["hint"])
        self._image_changed_in_preview = True
        return True

    def delete_image(self):
        if self.global_idx >= len(self.main_win.teach_data):
            return
        item = self.main_win.teach_data[self.global_idx]
        paths = self._normalize_paths(item.get("image_paths", item.get("image_path", "")))
        if not paths:
            return

        if QMessageBox.question(
            self, "确认清空图片", f"确定清空该记录的全部 {len(paths)} 张图片吗？"
        ) != QMessageBox.Yes:
            return
        for path in paths:
            full_path = os.path.join(self.main_win.work_dir(), path)
            try:
                if os.path.exists(full_path):
                    os.remove(full_path)
            except OSError as e:
                self.main_win.log(f"图片记录已清空，但文件删除失败：{path} - {e}", self.main_win.LOG_COLORS["err"])
        item["image_paths"] = []
        item["image_path"] = ""
        cfg_mod.save_config(self.main_win.cfg)
        self.main_win.log(f"已清空部署图片：{len(paths)} 张", self.main_win.LOG_COLORS["hint"])
        self.main_win._render_teach_table()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Git 多项目分支管理工具 v1.0.2 - builderTool")
        self.resize(1180, 760)

        self.cfg = cfg_mod.load_config()
        self.workers = []          # 持有线程引用，防止被 GC
        self._busy_buttons = []

        self._build_ui()
        self._restore_state()

    # ---------------- UI 构建 ----------------

    def _build_ui(self):
        from PyQt5.QtWidgets import QStackedWidget, QTableWidget, QTableWidgetItem, QHeaderView
        
        self.stacked_widget = QStackedWidget(self)
        
        left_widget = self._build_left()
        self.right_widget = self._build_right()
        
        # Page 0: Git 核心与打包
        git_splitter = QSplitter(Qt.Horizontal, self)
        git_splitter.addWidget(left_widget)
        git_splitter.addWidget(self.right_widget)
        git_splitter.setStretchFactor(0, 1)
        git_splitter.setStretchFactor(1, 3)
        self.stacked_widget.addWidget(git_splitter)
        
        # Page 1: 教学部署版面
        teach_panel = self._build_teach_panel()
        self.stacked_widget.addWidget(teach_panel)
        
        # 主布局容器
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # 顶层扁平化导航栏
        self.nav_frame = QFrame()
        self.nav_frame.setObjectName("NavBarFrame")
        nav_layout = QHBoxLayout(self.nav_frame)
        nav_layout.setContentsMargins(15, 6, 15, 6)
        nav_layout.setSpacing(10)
        
        # 切换按钮
        self.btn_nav_git = QPushButton("📦 打包与分支管理")
        self.btn_nav_git.setCheckable(True)
        self.btn_nav_git.setChecked(True)
        self.btn_nav_git.setFixedWidth(140)
        
        self.btn_nav_teach = QPushButton("🏫 教学部署版面")
        self.btn_nav_teach.setCheckable(True)
        self.btn_nav_teach.setFixedWidth(140)
        
        self.nav_group = QButtonGroup(self)
        self.nav_group.addButton(self.btn_nav_git)
        self.nav_group.addButton(self.btn_nav_teach)
        self.nav_group.setExclusive(True)
        
        self.btn_nav_git.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(0))
        self.btn_nav_teach.clicked.connect(lambda: self.stacked_widget.setCurrentIndex(1))
        
        nav_layout.addWidget(self.btn_nav_git)
        nav_layout.addWidget(self.btn_nav_teach)
        nav_layout.addStretch()
        
        # 全局修改密码按钮
        self.pwd_btn = QPushButton("🔑 修改密码")
        self.pwd_btn.setToolTip("修改软件的解锁密码")
        self.pwd_btn.setFixedWidth(90)
        self.pwd_btn.clicked.connect(self.change_unlock_password)
        nav_layout.addWidget(self.pwd_btn)
        
        # 全局主题切换按钮
        initial_theme = self.cfg.get("theme", "dark")
        self.theme_btn = QPushButton("☀ 浅色" if initial_theme == "light" else "☾ 深色")
        self.theme_btn.setToolTip("切换深色 / 浅色主题")
        self.theme_btn.setFixedWidth(80)
        self.theme_btn.clicked.connect(self.toggle_theme)
        nav_layout.addWidget(self.theme_btn)
        
        main_layout.addWidget(self.nav_frame)
        main_layout.addWidget(self.stacked_widget)
        
        self.setCentralWidget(container)
        
        # 应用导航栏主题样式
        self._apply_nav_style()
        self.statusBar().showMessage("就绪")

    def _build_teach_panel(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        
        # 顶部的搜索与操作栏
        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)
        
        search_label = QLabel("🔍 检索学校/备注：")
        search_label.setStyleSheet("font-weight: bold;")
        top_bar.addWidget(search_label)
        
        self.teach_search_input = QLineEdit()
        self.teach_search_input.setPlaceholderText("输入关键字过滤列表...")
        self.teach_search_input.textChanged.connect(self._filter_teach_table)
        top_bar.addWidget(self.teach_search_input, 1)
        
        top_bar.addSpacing(20)
        
        # 录入/增删/批量打包按钮
        self.btn_teach_add = QPushButton("➕ 录入部署信息")
        self.btn_teach_add.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 6px 12px; border: none; border-radius: 4px;")
        self.btn_teach_add.clicked.connect(self._add_teach_row)
        top_bar.addWidget(self.btn_teach_add)
        
        self.btn_teach_del = QPushButton("❌ 删除选中")
        self.btn_teach_del.setStyleSheet("background-color: #c62828; color: white; font-weight: bold; padding: 6px 12px; border: none; border-radius: 4px;")
        self.btn_teach_del.clicked.connect(self._delete_teach_row)
        top_bar.addWidget(self.btn_teach_del)
        
        self.btn_teach_build = QPushButton("📦 批量打包")
        self.btn_teach_build.setToolTip("将根据左侧勾选的项目进行批量打包")
        self.btn_teach_build.setStyleSheet("background-color: #1769d6; color: white; font-weight: bold; padding: 6px 12px; border: none; border-radius: 4px;")
        self.btn_teach_build.clicked.connect(self.build_project)
        top_bar.addWidget(self.btn_teach_build)
        
        layout.addLayout(top_bar)
        
        # 表格控件
        self.teach_table = QTableWidget()
        self.teach_table.setColumnCount(8)
        self.teach_table.setHorizontalHeaderLabels(["学校", "版本\\模式", "配置模块", "IP地址", "录入日期", "图片", "备注信息", "Base分支/版本"])
        self.teach_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.teach_table.horizontalHeader().setStretchLastSection(True)
        self.teach_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.teach_table.setSelectionMode(QTableWidget.SingleSelection)
        
        # 设置列宽度，高亮配置模块列
        self.teach_table.setColumnWidth(0, 200) # 学校
        self.teach_table.setColumnWidth(1, 100) # 版本\模式
        self.teach_table.setColumnWidth(2, 180) # 配置模块
        self.teach_table.setColumnWidth(3, 140) # IP地址
        self.teach_table.setColumnWidth(4, 115) # 录入日期
        self.teach_table.setColumnWidth(5, 180) # 图片
        self.teach_table.setColumnWidth(6, 200) # 备注信息
        self.teach_table.setColumnWidth(7, 150) # Base分支/版本
        
        # 设置默认行高以防单元格自定义控件被裁剪
        self.teach_table.verticalHeader().setDefaultSectionSize(42)
        
        # 监听单元格变化与双击事件
        self.teach_table.itemChanged.connect(self._on_teach_cell_changed)
        self.teach_table.cellDoubleClicked.connect(self._on_teach_cell_double_clicked)
        layout.addWidget(self.teach_table, 1)
        
        # 底部分页栏
        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(10)
        
        self.teach_total_label = QLabel("共 0 条记录")
        bottom_bar.addWidget(self.teach_total_label)
        bottom_bar.addStretch()
        
        self.btn_teach_prev = QPushButton("◀ 上一页")
        self.btn_teach_prev.clicked.connect(self._teach_prev_page)
        bottom_bar.addWidget(self.btn_teach_prev)
        
        self.teach_page_label = QLabel("第 1 / 1 页")
        self.teach_page_label.setStyleSheet("font-weight: bold; margin: 0 10px;")
        bottom_bar.addWidget(self.teach_page_label)
        
        self.btn_teach_next = QPushButton("下一页 ▶")
        self.btn_teach_next.clicked.connect(self._teach_next_page)
        bottom_bar.addWidget(self.btn_teach_next)
        
        bottom_bar.addSpacing(15)
        
        self.teach_page_size_combo = QComboBox()
        self.teach_page_size_combo.addItems(["10条/页", "20条/页", "50条/页"])
        self.teach_page_size_combo.currentIndexChanged.connect(self._teach_page_size_changed)
        bottom_bar.addWidget(self.teach_page_size_combo)
        
        layout.addLayout(bottom_bar)
        
        # 初始化分页状态
        self.teach_current_page = 1
        self.teach_page_size = 10
        self.teach_data = []
        
        self._load_teach_data()
        self._apply_teach_table_style()
        self._render_teach_table()
        
        return w

    def _load_teach_data(self):
        # 检查配置中是否存在 teach_deployments，若无则初始化预置数据
        if "teach_deployments" not in self.cfg or not self.cfg["teach_deployments"]:
            self.cfg["teach_deployments"] = [
                {
                    "school": "郑州信息科技职业学院",
                    "version": "新版",
                    "ip": "10.1.13.55",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "",
                    "base": "(完整本地化)"
                },
                {
                    "school": "黄山学院",
                    "version": "旧版",
                    "ip": "192.168.65.235",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "按照本地化（无网络环境下）处理",
                    "base": ""
                },
                {
                    "school": "湖南女子",
                    "version": "旧版",
                    "ip": "172.19.110.102",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "海鳗云旅游大数据平台大屏",
                    "base": "黄山学院"
                },
                {
                    "school": "成都-信管学院",
                    "version": "",
                    "ip": "172.16.10.52",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "自定义（只有3个大屏）",
                    "base": ""
                },
                {
                    "school": "成都-旅游学院",
                    "version": "",
                    "ip": "172.16.10.51",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "",
                    "base": ""
                },
                {
                    "school": "吉林电子",
                    "version": "",
                    "ip": "https://jltc-edu.haimanyun.com",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "特殊处理的自定义大屏",
                    "base": ""
                },
                {
                    "school": "吉林外国语大学 +（新版舆情+新版满意度）",
                    "version": "新版",
                    "ip": "192.168.137.117 改成: 192.168.2.108",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "当前版本可以作为 完全本地化的 范例；切记改新版舆情的时候，一定要注意是否是本地化请求的哦。找分支: eduLocal-吉林外国语大学-20260311(base郑职-案例)",
                    "base": "郑州职业学院"
                },
                {
                    "school": "三峡旅游",
                    "version": "",
                    "ip": "10.6.62.2",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "",
                    "base": ""
                },
                {
                    "school": "吉林农大",
                    "version": "新版",
                    "ip": "10.51.0.205",
                    "date": "2026-06-11",
                    "image_path": "",
                    "remark": "过程很丝滑!",
                    "base": "吉林外国语大学"
                }
            ]
            cfg_mod.save_config(self.cfg)
        
        # 兼容性迁移：确保每个项都包含 date 和 image_paths
        changed = False
        for item in self.cfg.get("teach_deployments", []):
            if "date" not in item:
                item["date"] = "2026-06-11"
                changed = True
            if "image_path" not in item:
                item["image_path"] = ""
                changed = True
            if "image_paths" not in item:
                item["image_paths"] = [item["image_path"]] if item.get("image_path") else []
                changed = True
            elif isinstance(item.get("image_paths"), str):
                item["image_paths"] = [item["image_paths"]] if item["image_paths"] else []
                changed = True
            if item.get("image_paths") and not item.get("image_path"):
                item["image_path"] = item["image_paths"][0]
                changed = True
        seen_branches = set()
        deduped_deployments = []
        for item in self.cfg.get("teach_deployments", []):
            branch_name = item.get("branch_name", "")
            if branch_name:
                if branch_name in seen_branches:
                    changed = True
                    continue
                seen_branches.add(branch_name)
            deduped_deployments.append(item)
        if len(deduped_deployments) != len(self.cfg.get("teach_deployments", [])):
            self.cfg["teach_deployments"] = deduped_deployments
            changed = True
        if changed:
            cfg_mod.save_config(self.cfg)
        
        self.teach_data = list(self.cfg["teach_deployments"])

    def _apply_teach_table_style(self):
        theme = getattr(self, "current_theme", "dark")
        if theme == "light":
            self.teach_table.setStyleSheet("""
                QTableWidget {
                    background-color: #ffffff;
                    gridline-color: #e0e0e0;
                    color: #333333;
                    border: 1px solid #e0e0e0;
                }
                QTableWidget::item {
                    padding: 6px;
                }
                QHeaderView::section {
                    background-color: #f5f5f5;
                    color: #333333;
                    padding: 6px;
                    border: 1px solid #e0e0e0;
                    font-weight: bold;
                }
                QTableWidget::item:selected {
                    background-color: #1769d6;
                    color: white;
                }
                QComboBox, QDateEdit {
                    background-color: #ffffff;
                    color: #333333;
                    border: 1px solid #cccccc;
                    border-radius: 3px;
                    padding: 2px;
                }
                QComboBox::drop-down, QDateEdit::drop-down {
                    border: none;
                }
            """)
        else:
            self.teach_table.setStyleSheet("""
                QTableWidget {
                    background-color: #1e1e1e;
                    gridline-color: #3d3d3d;
                    color: #e0e0e0;
                    border: 1px solid #3d3d3d;
                }
                QTableWidget::item {
                    padding: 6px;
                }
                QHeaderView::section {
                    background-color: #2d2d2d;
                    color: #e0e0e0;
                    padding: 6px;
                    border: 1px solid #3d3d3d;
                    font-weight: bold;
                }
                QTableWidget::item:selected {
                    background-color: #1769d6;
                    color: white;
                }
                QComboBox, QDateEdit {
                    background-color: #2d2d2d;
                    color: #e0e0e0;
                    border: 1px solid #555555;
                    border-radius: 3px;
                    padding: 2px;
                }
            """)

    def _apply_nav_style(self):
        theme = getattr(self, "current_theme", "dark")
        if theme == "light":
            self.nav_frame.setStyleSheet("""
                QFrame#NavBarFrame {
                    border-bottom: 1px solid #e0e0e0;
                    background-color: #f5f5f5;
                }
            """)
            btn_style = """
                QPushButton {
                    padding: 6px 12px;
                    font-weight: bold;
                    border: none;
                    border-radius: 4px;
                    color: #333333;
                    background-color: transparent;
                }
                QPushButton:hover {
                    background-color: #e0e0e0;
                    color: black;
                }
                QPushButton:checked {
                    background-color: #1769d6;
                    color: white;
                }
            """
            self.btn_nav_git.setStyleSheet(btn_style)
            self.btn_nav_teach.setStyleSheet(btn_style)
            
            tool_btn_style = """
                QPushButton {
                    padding: 6px 10px;
                    border: 1px solid #cccccc;
                    border-radius: 4px;
                    color: #333333;
                    background-color: #ffffff;
                }
                QPushButton:hover {
                    background-color: #f0f0f0;
                    color: black;
                }
            """
            self.pwd_btn.setStyleSheet(tool_btn_style)
            self.theme_btn.setStyleSheet(tool_btn_style)
        else:
            self.nav_frame.setStyleSheet("""
                QFrame#NavBarFrame {
                    border-bottom: 1px solid #3d3d3d;
                    background-color: #252526;
                }
            """)
            btn_style = """
                QPushButton {
                    padding: 6px 12px;
                    font-weight: bold;
                    border: none;
                    border-radius: 4px;
                    color: #cccccc;
                    background-color: transparent;
                }
                QPushButton:hover {
                    background-color: #333333;
                    color: white;
                }
                QPushButton:checked {
                    background-color: #1769d6;
                    color: white;
                }
            """
            self.btn_nav_git.setStyleSheet(btn_style)
            self.btn_nav_teach.setStyleSheet(btn_style)
            
            tool_btn_style = """
                QPushButton {
                    padding: 6px 10px;
                    border: 1px solid #555555;
                    border-radius: 4px;
                    color: #cccccc;
                    background-color: #2d2d2d;
                }
                QPushButton:hover {
                    background-color: #3a3a3a;
                    color: white;
                }
            """
            self.pwd_btn.setStyleSheet(tool_btn_style)
            self.theme_btn.setStyleSheet(tool_btn_style)

    def _render_teach_table(self):
        self.teach_table.blockSignals(True)
        
        total_items = len(self.teach_data)
        self.teach_total_label.setText(f"共 {total_items} 条记录")
        
        total_pages = max(1, (total_items + self.teach_page_size - 1) // self.teach_page_size)
        if self.teach_current_page > total_pages:
            self.teach_current_page = total_pages
        if self.teach_current_page < 1:
            self.teach_current_page = 1
            
        self.teach_page_label.setText(f"第 {self.teach_current_page} / {total_pages} 页")
        self.btn_teach_prev.setEnabled(self.teach_current_page > 1)
        self.btn_teach_next.setEnabled(self.teach_current_page < total_pages)
        
        start_idx = (self.teach_current_page - 1) * self.teach_page_size
        end_idx = min(start_idx + self.teach_page_size, total_items)
        page_items = self.teach_data[start_idx:end_idx]
        
        self.teach_table.setRowCount(len(page_items))
        for row_idx, item in enumerate(page_items):
            global_idx = start_idx + row_idx
            
            # School (Col 0)
            school_item = QTableWidgetItem(item.get("school", ""))
            school_item.setData(Qt.UserRole, global_idx)
            self.teach_table.setItem(row_idx, 0, school_item)
            
            # Version (Col 1) - Editable QComboBox
            self.teach_table.setItem(row_idx, 1, QTableWidgetItem(""))
            combo = QComboBox()
            combo.addItems(["新版", "旧版"])
            combo.setEditable(True)
            combo.setCurrentText(item.get("version", ""))
            combo.currentTextChanged.connect(lambda text, g_idx=global_idx: self._on_teach_combo_changed(g_idx, text))
            self.teach_table.setCellWidget(row_idx, 1, combo)
            
            # Config modules (Col 2)
            self.teach_table.setItem(row_idx, 2, QTableWidgetItem(""))
            config_note = item.get("config_note", "")
            config_widget = ConfigTagsCellWidget(config_note, self)
            self.teach_table.setCellWidget(row_idx, 2, config_widget)
            
            # IP Address (Col 3)
            self.teach_table.setItem(row_idx, 3, QTableWidgetItem(""))
            ip_widget = IpCellWidget(item.get("ip", ""), self, global_idx)
            self.teach_table.setCellWidget(row_idx, 3, ip_widget)
            
            # Date (Col 4) - QDateEdit
            self.teach_table.setItem(row_idx, 4, QTableWidgetItem(""))
            date_edit = QDateEdit()
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            date_str = item.get("date", "")
            if date_str:
                qdate = QDate.fromString(date_str, "yyyy-MM-dd")
                if qdate.isValid():
                    date_edit.setDate(qdate)
                else:
                    date_edit.setDate(QDate.currentDate())
            else:
                date_edit.setDate(QDate.currentDate())
            date_edit.dateChanged.connect(lambda qd, g_idx=global_idx: self._on_teach_date_changed(g_idx, qd.toString("yyyy-MM-dd")))
            self.teach_table.setCellWidget(row_idx, 4, date_edit)
            
            # Image (Col 5) - ImageCellWidget
            self.teach_table.setItem(row_idx, 5, QTableWidgetItem(""))
            img_widget = ImageCellWidget(item.get("image_paths", item.get("image_path", "")), self, global_idx)
            self.teach_table.setCellWidget(row_idx, 5, img_widget)
            
            # Remark (Col 6)
            remark_item = QTableWidgetItem(item.get("remark", ""))
            remark_item.setToolTip(item.get("remark", ""))
            self.teach_table.setItem(row_idx, 6, remark_item)
            
            # Base branch/version (Col 7)
            base_item = QTableWidgetItem(item.get("base", ""))
            self.teach_table.setItem(row_idx, 7, base_item)
            
        self.teach_table.blockSignals(False)

    def _on_teach_combo_changed(self, global_idx, text):
        if global_idx is not None and global_idx < len(self.teach_data):
            self.teach_data[global_idx]["version"] = text.strip()
            cfg_mod.save_config(self.cfg)

    def _on_teach_date_changed(self, global_idx, date_str):
        if global_idx is not None and global_idx < len(self.teach_data):
            self.teach_data[global_idx]["date"] = date_str
            cfg_mod.save_config(self.cfg)

    def _filter_teach_table(self):
        query = self.teach_search_input.text().strip().lower()
        all_items = self.cfg.get("teach_deployments", [])
        if not query:
            self.teach_data = list(all_items)
        else:
            self.teach_data = [
                d for d in all_items
                if query in d.get("school", "").lower() 
                or query in d.get("version", "").lower() 
                or query in d.get("ip", "").lower() 
                or query in d.get("remark", "").lower() 
                or query in d.get("base", "").lower()
                or query in d.get("branch_name", "").lower()
                or query in d.get("config_note", "").lower()
            ]
        self.teach_current_page = 1
        self._render_teach_table()

    def _add_teach_row(self):
        new_item = {
            "school": "双击修改学校名称",
            "version": "新版",
            "config_note": "",
            "ip": "",
            "date": datetime.today().strftime("%Y-%m-%d"),
            "image_path": "",
            "image_paths": [],
            "remark": "",
            "base": ""
        }
        self.cfg.setdefault("teach_deployments", []).insert(0, new_item)
        cfg_mod.save_config(self.cfg)
        
        self._load_teach_data()
        self.teach_search_input.clear()
        self.teach_current_page = 1
        self._render_teach_table()
        
        self.teach_table.setCurrentCell(0, 0)
        self.teach_table.editItem(self.teach_table.item(0, 0))

    def _delete_teach_row(self):
        row = self.teach_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "提示", "请先在表格中点击选中一行进行删除")
            return
            
        school_item = self.teach_table.item(row, 0)
        if not school_item:
            return
        global_idx = school_item.data(Qt.UserRole)
        if global_idx is None or global_idx >= len(self.teach_data):
            return
            
        target_item = self.teach_data[global_idx]
        school_name = target_item.get("school", "未命名学校")
        
        if QMessageBox.question(
            self, "确认删除", f"确定要删除学校 [{school_name}] 的部署记录吗？"
        ) != QMessageBox.Yes:
            return
            
        if target_item in self.cfg.get("teach_deployments", []):
            self.cfg["teach_deployments"].remove(target_item)
            cfg_mod.save_config(self.cfg)
            
        self.log(f"已删除学校部署记录：{school_name}", self.LOG_COLORS["hint"])
        self._load_teach_data()
        self._filter_teach_table()

    def _on_teach_cell_changed(self, item: QTableWidgetItem):
        row = item.row()
        col = item.column()
        school_item = self.teach_table.item(row, 0)
        if not school_item:
            return
        global_idx = school_item.data(Qt.UserRole)
        if global_idx is None or global_idx >= len(self.teach_data):
            return
            
        target_item = self.teach_data[global_idx]
        val = item.text().strip()
        
        if col == 0:
            target_item["school"] = val
        elif col == 3:
            target_item["ip"] = val
        elif col == 6:
            target_item["remark"] = val
            item.setToolTip(val)
        elif col == 7:
            target_item["base"] = val
            
        cfg_mod.save_config(self.cfg)
        self.teach_current_page = 1
        self._render_teach_table()

    def _on_teach_cell_double_clicked(self, row, col):
        if col == 2:
            school_item = self.teach_table.item(row, 0)
            if not school_item:
                return
            global_idx = school_item.data(Qt.UserRole)
            if global_idx is None or global_idx >= len(self.teach_data):
                return
            target_item = self.teach_data[global_idx]
            current_note = target_item.get("config_note", "")
            
            dlg = BranchConfigDialog(self, initial_note=current_note)
            if dlg.exec_() == QDialog.Accepted:
                new_note = dlg.get_note()
                target_item["config_note"] = new_note
                cfg_mod.save_config(self.cfg)
                self._render_teach_table()
                self.log(f"已更新学校 [{target_item.get('school')}] 的配置模块为: {new_note}", self.LOG_COLORS["ok"])

    def _teach_prev_page(self):
        if self.teach_current_page > 1:
            self.teach_current_page -= 1
            self._render_teach_table()

    def _teach_next_page(self):
        total_items = len(self.teach_data)
        total_pages = max(1, (total_items + self.teach_page_size - 1) // self.teach_page_size)
        if self.teach_current_page < total_pages:
            self.teach_current_page += 1
            self._render_teach_table()

    def _teach_page_size_changed(self, idx: int):
        sizes = [10, 20, 50]
        if idx >= 0 and idx < len(sizes):
            self.teach_page_size = sizes[idx]
            self.teach_current_page = 1
            self._render_teach_table()

    def _build_left(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel("工作目录："))
        dir_row = QHBoxLayout()
        self.dir_combo = QComboBox()
        self.dir_combo.setEditable(True)
        self.dir_combo.view().setContextMenuPolicy(Qt.CustomContextMenu)
        self.dir_combo.view().customContextMenuRequested.connect(self._dir_history_menu)
        dir_row.addWidget(self.dir_combo, 1)
        del_dir_btn = QPushButton("✕")
        del_dir_btn.setFixedWidth(32)
        del_dir_btn.setToolTip("从历史中删除当前下拉框选中的目录")
        del_dir_btn.clicked.connect(self._delete_current_dir)
        dir_row.addWidget(del_dir_btn)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(32)
        browse_btn.setToolTip("浏览选择目录")
        browse_btn.clicked.connect(self._browse_work_dir)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        btn_row = QHBoxLayout()
        self.scan_btn = QPushButton("读取目录")
        self.scan_btn.clicked.connect(self.scan_projects)
        btn_row.addWidget(self.scan_btn)
        self.clone_btn = QPushButton("克隆仓库")
        self.clone_btn.clicked.connect(self.clone_repo)
        btn_row.addWidget(self.clone_btn)
        layout.addLayout(btn_row)

        self.project_count_label = QLabel("项目列表 (0)（右键可快捷全选/设置备注/管理分组）")
        layout.addWidget(self.project_count_label)

        # 创建树控件
        self.project_list = ProjectTreeWidget()
        self.project_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(self._project_menu)
        self.project_list.itemSelectionChanged.connect(self._on_project_selected)
        self.project_list.model().rowsMoved.connect(self._save_group_order)
        layout.addWidget(self.project_list, 1)

        # 底部工具栏：展开/折叠 + 全选/取消全选
        expand_row = QHBoxLayout()
        expand_row.setContentsMargins(0, 2, 0, 0)
        btn_style = (
            "QPushButton{font-size:11px;padding:0 6px;border:1px solid #555;border-radius:3px;}"
            "QPushButton:hover{background:#1769d6;color:white;border-color:#1769d6;}"
        )
        btn_expand_all = QPushButton("▶ 展开")
        btn_expand_all.setFixedHeight(22)
        btn_expand_all.setStyleSheet(btn_style)
        btn_expand_all.clicked.connect(self.project_list.expandAll)
        btn_collapse_all = QPushButton("▼ 折叠")
        btn_collapse_all.setFixedHeight(22)
        btn_collapse_all.setStyleSheet(btn_style)
        btn_collapse_all.clicked.connect(self.project_list.collapseAll)
        btn_check_all = QPushButton("☑ 全选")
        btn_check_all.setFixedHeight(22)
        btn_check_all.setStyleSheet(btn_style)
        btn_check_all.clicked.connect(
            lambda: [it.setCheckState(0, Qt.Checked)
                     for it in self.project_list.all_project_items()])
        btn_uncheck_all = QPushButton("☐ 取消")
        btn_uncheck_all.setFixedHeight(22)
        btn_uncheck_all.setStyleSheet(btn_style)
        btn_uncheck_all.clicked.connect(
            lambda: [it.setCheckState(0, Qt.Unchecked)
                     for it in self.project_list.all_project_items()])
        expand_row.addWidget(btn_expand_all)
        expand_row.addWidget(btn_collapse_all)
        expand_row.addSpacing(8)
        expand_row.addWidget(btn_check_all)
        expand_row.addWidget(btn_uncheck_all)
        expand_row.addStretch(1)
        layout.addLayout(expand_row)
        return w



    def _build_right(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # 顶部：当前项目/分支
        top = QHBoxLayout()
        self.cur_project_label = QLabel("当前项目：-")
        f = QFont()
        f.setBold(True)
        self.cur_project_label.setFont(f)
        top.addWidget(self.cur_project_label)
        top.addStretch(1)
        self.cur_branch_label = QLabel("当前分支：-")
        self.cur_branch_label.setFont(f)
        top.addWidget(self.cur_branch_label)
        layout.addLayout(top)

        # 分支操作
        g1 = QGroupBox("分支操作")
        g1v = QVBoxLayout(g1)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("分支（本地）："))
        self.branch_combo = QComboBox()
        self.branch_combo.setMinimumWidth(300)
        # 同步更新"新建分支"区的基准分支标签，并刷新当前分支配置标签
        self.branch_combo.currentTextChanged.connect(self._on_cur_branch_changed)
        # 用户手动选择分支时自动切换（activated 不响应程序刷新）
        self.branch_combo.activated.connect(lambda _: self.checkout_branch())
        r1.addWidget(self.branch_combo, 1)
        # 当前分支配置标签（有已保存配置时显示）
        self.cur_cfg_tag_chanye = QPushButton("产业")
        self.cur_cfg_tag_chanye.setFixedSize(46, 22)
        self.cur_cfg_tag_chanye.setStyleSheet(
            "QPushButton{background:#27ae60;color:#fff;border-radius:10px;"
            "font-size:11px;padding:0 4px;border:none;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        self.cur_cfg_tag_chanye.setToolTip("当前分支已配置产业（教学）或教育产品线，点击修改配置")
        self.cur_cfg_tag_chanye.clicked.connect(self._edit_cur_branch_config)
        self.cur_cfg_tag_chanye.setVisible(False)
        r1.addWidget(self.cur_cfg_tag_chanye)
        self.cur_cfg_tag_shixun = QPushButton("实训")
        self.cur_cfg_tag_shixun.setFixedSize(46, 22)
        self.cur_cfg_tag_shixun.setStyleSheet(
            "QPushButton{background:#2980b9;color:#fff;border-radius:10px;"
            "font-size:11px;padding:0 4px;border:none;}"
            "QPushButton:hover{background:#3498db;}"
        )
        self.cur_cfg_tag_shixun.setToolTip("当前分支已配置实训（大屏）模块，点击修改配置")
        self.cur_cfg_tag_shixun.clicked.connect(self._edit_cur_branch_config)
        self.cur_cfg_tag_shixun.setVisible(False)
        r1.addWidget(self.cur_cfg_tag_shixun)
        self.edit_cfg_btn = QPushButton("⚙ 打标签")
        self.edit_cfg_btn.setFixedSize(60, 22)
        self.edit_cfg_btn.setStyleSheet(
            "QPushButton{background:#8e44ad;color:#fff;border-radius:3px;"
            "font-size:11px;font-weight:bold;border:none;}"
            "QPushButton:hover{background:#9b59b6;}"
        )
        self.edit_cfg_btn.setToolTip("修改或新增当前选中分支的产业/实训配置")
        self.edit_cfg_btn.clicked.connect(self._edit_cur_branch_config)
        r1.addWidget(self.edit_cfg_btn)
        self.fetch_branch_btn = QPushButton("重新获取分支")
        self.fetch_branch_btn.clicked.connect(self.fetch_branches)
        r1.addWidget(self.fetch_branch_btn)
        self.del_branch_btn = QPushButton("🗑 删除分支")
        self.del_branch_btn.setToolTip("删除当前下拉框选中的分支（本地/远端可选）")
        self.del_branch_btn.clicked.connect(self.delete_branch)
        r1.addWidget(self.del_branch_btn)
        g1v.addLayout(r1)


        # 远端分支（可折叠）
        self.toggle_remote_btn = QToolButton()
        self.toggle_remote_btn.setText("远端分支")
        self.toggle_remote_btn.setCheckable(True)
        self.toggle_remote_btn.setChecked(False)
        self.toggle_remote_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_remote_btn.setArrowType(Qt.RightArrow)
        self.toggle_remote_btn.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.toggle_remote_btn.toggled.connect(self._toggle_remote_panel)
        g1v.addWidget(self.toggle_remote_btn)

        self.remote_list = QListWidget()
        self.remote_list.setMaximumHeight(120)
        self.remote_list.setVisible(False)
        self.remote_list.itemDoubleClicked.connect(lambda _: self.checkout_remote_branch())
        self.remote_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.remote_list.customContextMenuRequested.connect(self._remote_branch_menu)
        g1v.addWidget(self.remote_list)
        layout.addWidget(g1)

        # 常用操作
        g2 = QGroupBox("常用操作")
        r2 = QHBoxLayout(g2)
        
        self.pull_btn = QPushButton("pull")
        self.pull_btn.clicked.connect(lambda: self.simple_git("pull"))
        self.status_btn = QPushButton("status")
        self.status_btn.clicked.connect(lambda: self.simple_git("status"))
        self.add_btn = QPushButton("add")
        self.add_btn.clicked.connect(self.do_add)
        self.commit_btn = QPushButton("commit")
        self.commit_btn.clicked.connect(self.do_commit)
        self.push_btn = QPushButton("push")
        self.push_btn.clicked.connect(lambda: self.simple_git("push"))
        for b in (self.pull_btn, self.status_btn, self.add_btn, self.commit_btn, self.push_btn):
            r2.addWidget(b)

        # 添加分割线以区分单个操作和一键操作
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        line.setStyleSheet("color: #ccc;")
        r2.addWidget(line)

        self.quick_publish_btn = QPushButton("⚡ 一键操作 (Publish)")
        self.quick_publish_btn.clicked.connect(self.do_quick_publish)
        self.quick_publish_btn.setStyleSheet(
            "QPushButton{background:#d35400;color:#fff;border-radius:3px;font-weight:bold;padding:3px 12px;}"
            "QPushButton:hover{background:#e67e22;}"
        )
        self.quick_publish_btn.setToolTip("一键执行 status、add、commit 并 push")
        r2.addWidget(self.quick_publish_btn)

        r2.addStretch(1)
        layout.addWidget(g2)

        # 新建分支
        g3 = QGroupBox("新建分支")
        r3 = QGridLayout(g3)
        r3.setVerticalSpacing(8)

        # 行0：基准分支（只读标签，与上方"分支（本地）"①同步，无需重复选择）
        r3.addWidget(QLabel("基准分支："), 0, 0)
        self.base_branch_label = QLabel("—")
        self.base_branch_label.setStyleSheet("QLabel{padding:2px 6px;border:1px solid #888;border-radius:3px;}")
        r3.addWidget(self.base_branch_label, 0, 1, 1, 6)

        # 行1：新分支名 | 产业tag | 实训tag | ✕ | ⚙选择配置
        r3.addWidget(QLabel("新分支名："), 1, 0)
        
        row1_lay = QHBoxLayout()
        row1_lay.setContentsMargins(0, 0, 0, 0)
        row1_lay.setSpacing(6)
        
        self.new_branch_edit = QComboBox()
        self.new_branch_edit.setEditable(True)
        self.new_branch_edit.lineEdit().setPlaceholderText("例如：eduLocal-吉林农大-t1-20260611(base吉林外国语)")
        self.new_branch_edit.currentIndexChanged.connect(self._on_branch_name_selected)
        row1_lay.addWidget(self.new_branch_edit, 1)

        # 产业/实训配置标签（选配置后出现，点击查看详情）
        self.tag_chanye = QPushButton("产业")
        self.tag_chanye.setFixedSize(46, 22)
        self.tag_chanye.setToolTip("已选择产业（教学）或教育产品线— 点击查看选中详情")
        self.tag_chanye.setStyleSheet(
            "QPushButton{background:#27ae60;color:#fff;border-radius:10px;"
            "font-size:11px;padding:0 4px;border:none;}"
            "QPushButton:hover{background:#2ecc71;}"
        )
        self.tag_chanye.clicked.connect(lambda: self._show_config_detail("chanye"))
        self.tag_chanye.setVisible(False)
        row1_lay.addWidget(self.tag_chanye)

        self.tag_shixun = QPushButton("实训")
        self.tag_shixun.setFixedSize(46, 22)
        self.tag_shixun.setToolTip("已选择实训（大屏）— 点击查看选中详情")
        self.tag_shixun.setStyleSheet(
            "QPushButton{background:#2980b9;color:#fff;border-radius:10px;"
            "font-size:11px;padding:0 4px;border:none;}"
            "QPushButton:hover{background:#3498db;}"
        )
        self.tag_shixun.clicked.connect(lambda: self._show_config_detail("shixun"))
        self.tag_shixun.setVisible(False)
        row1_lay.addWidget(self.tag_shixun)

        del_branch_btn = QPushButton("✕")
        del_branch_btn.setFixedWidth(30)
        del_branch_btn.setToolTip("从下拉历史中删除当前条目（不删除实际 git 分支）")
        del_branch_btn.clicked.connect(self._delete_branch_name)
        row1_lay.addWidget(del_branch_btn)

        cfg_btn = QPushButton("⚙ 选择配置…")
        cfg_btn.setToolTip("选择产业/实训/教育产品线模块，自动生成配置标签")
        cfg_btn.clicked.connect(self._open_branch_config)
        row1_lay.addWidget(cfg_btn)

        r3.addLayout(row1_lay, 1, 1, 1, 6)

        # 行2：创建并切换（单独占一行，靠左）
        row2_lay = QHBoxLayout()
        row2_lay.setContentsMargins(0, 0, 0, 0)
        self.create_branch_btn = QPushButton("✔ 创建并切换")
        self.create_branch_btn.clicked.connect(self.create_branch)
        row2_lay.addWidget(self.create_branch_btn)
        row2_lay.addStretch(1)
        r3.addLayout(row2_lay, 2, 1, 1, 6)

        # 内部备注存储（不可见，仅作数据存储）
        self.branch_note_edit = QLineEdit()
        self.branch_note_edit.setVisible(False)

        r3.setColumnStretch(1, 1)
        layout.addWidget(g3)


        # IP + Logo 替换
        g4 = QGroupBox("打包配置（IP + Logo 替换）")
        g4v = QVBoxLayout(g4)
        ip_row = QHBoxLayout()
        logo_row = QHBoxLayout()
        ip_row.addWidget(QLabel("旧IP："))
        self.old_ip_edit = QComboBox()
        self.old_ip_edit.setEditable(True)
        self.old_ip_edit.setMinimumWidth(150)
        self.old_ip_edit.lineEdit().setPlaceholderText("可下拉选历史")
        ip_row.addWidget(self.old_ip_edit, 1)
        del_old_ip_btn = QPushButton("✕")
        del_old_ip_btn.setFixedWidth(32)
        del_old_ip_btn.setToolTip("删除当前旧IP历史条目")
        del_old_ip_btn.clicked.connect(self._delete_old_ip)
        ip_row.addWidget(del_old_ip_btn)
        ip_row.addWidget(QLabel("新IP："))
        self.new_ip_edit = QLineEdit()
        self.new_ip_edit.setPlaceholderText("请输入替换IP")
        ip_row.addWidget(self.new_ip_edit, 1)
        self.preview_ip_btn = QPushButton("预览匹配")
        self.preview_ip_btn.clicked.connect(self.preview_ip)
        ip_row.addWidget(self.preview_ip_btn)
        self.ip_history_btn = QPushButton("历史")
        self.ip_history_btn.clicked.connect(self.show_ip_history)
        ip_row.addWidget(self.ip_history_btn)
        self.replace_ip_btn = QPushButton("一键替换")
        self.replace_ip_btn.setToolTip("执行 IP 替换；如已上传 Logo，则同时替换登录 Logo")
        self.replace_ip_btn.clicked.connect(self.replace_ip)
        ip_row.addWidget(self.replace_ip_btn)
        g4v.addLayout(ip_row)

        self.logo_file_path = ""
        self.logo_label = QLabel("Logo：")
        logo_row.addWidget(self.logo_label)
        self.upload_logo_btn = QPushButton("上传Logo")
        self.upload_logo_btn.setToolTip("仅教育旧项目可用：选择学校 Logo，未上传则一键替换时不处理 Logo")
        self.upload_logo_btn.clicked.connect(self.select_logo_file)
        self.upload_logo_btn.setVisible(False)
        logo_row.addWidget(self.upload_logo_btn)
        self.logo_file_label = QLabel("未上传")
        self.logo_file_label.setMinimumWidth(56)
        self.logo_file_label.setToolTip("未上传 Logo 时不会替换 Logo")
        self.logo_file_label.setVisible(False)
        logo_row.addWidget(self.logo_file_label)
        self.preview_logo_btn = QPushButton("预览Logo")
        self.preview_logo_btn.setToolTip("预览已上传的 Logo")
        self.preview_logo_btn.clicked.connect(self.preview_logo_file)
        self.preview_logo_btn.setVisible(False)
        self.preview_logo_btn.setEnabled(False)
        logo_row.addWidget(self.preview_logo_btn)
        self.clear_logo_btn = QPushButton("✕")
        self.clear_logo_btn.setFixedWidth(28)
        self.clear_logo_btn.setToolTip("清除已选择的 Logo")
        self.clear_logo_btn.clicked.connect(self.clear_logo_file)
        self.clear_logo_btn.setVisible(False)
        logo_row.addWidget(self.clear_logo_btn)
        logo_row.addStretch(1)
        g4v.addLayout(logo_row)
        layout.addWidget(g4)

        # 日志
        g5 = QGroupBox("操作日志")
        v5 = QVBoxLayout(g5)
        log_top = QHBoxLayout()
        log_top.addStretch(1)
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(lambda: self.log_view.clear())
        log_top.addWidget(clear_btn)
        v5.addLayout(log_top)
        self.log_view = QTextBrowser()
        self.log_view.setReadOnly(True)
        self.log_view.setOpenLinks(False)
        self.log_view.anchorClicked.connect(self._on_log_link_clicked)
        self.log_view.setFont(QFont("Consolas", 9))
        v5.addWidget(self.log_view, 1)
        layout.addWidget(g5, 1)

        # 最右下角：打包按钮
        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        self.build_btn = QPushButton("📦 打包")
        self.build_btn.setToolTip("根据 package.json 中的 Vue 版本自动切换 Node 并执行打包")
        self.build_btn.clicked.connect(self.build_project)
        self.build_btn.setStyleSheet(
            "QPushButton{background:#34495e;color:#fff;border-radius:3px;font-weight:bold;padding:5px 15px;}"
            "QPushButton:hover{background:#2c3e50;}"
            "QPushButton:disabled{background:#7f8c8d;color:#bdc3c7;}"
        )
        bottom_row.addWidget(self.build_btn)
        layout.addLayout(bottom_row)

        self._busy_buttons = [
            self.scan_btn, self.clone_btn, self.fetch_branch_btn,
            self.status_btn, self.pull_btn, self.add_btn, self.commit_btn, self.push_btn,
            self.create_branch_btn, self.preview_ip_btn, self.replace_ip_btn,
            self.upload_logo_btn, self.preview_logo_btn, self.clear_logo_btn, self.build_btn,
            self.project_list, self.dir_combo, self.edit_cfg_btn, self.quick_publish_btn,
        ]
        return w

    # ---------------- 状态/工具 ----------------

    def _restore_state(self):
        dirs = self.cfg.get("work_dirs", [])
        self.dir_combo.addItems(dirs)
        self.old_ip_edit.setCurrentText(self.cfg.get("last_old_ip", ""))
        self.new_ip_edit.setText(self.cfg.get("last_new_ip", ""))
        theme = self.cfg.get("theme", "dark")
        self.current_theme = theme
        self.theme_btn.setText("☀ 浅色" if theme == "light" else "☾ 深色")
        self._apply_log_colors()
        if hasattr(self, "nav_frame"):
            self._apply_nav_style()
        if hasattr(self, "teach_table"):
            self._apply_teach_table_style()
        # 加载分支名历史
        self._refresh_branch_name_history()
        if dirs:
            self.scan_projects()

    def closeEvent(self, event):
        self.cfg["last_old_ip"] = self.old_ip_edit.currentText().strip()
        self.cfg["last_new_ip"] = self.new_ip_edit.text().strip()
        cfg_mod.save_config(self.cfg)
        if hasattr(self, "build_process") and self.build_process.state() == QProcess.Running:
            self.build_process.terminate()
            self.build_process.waitForFinished(1000)
        super().closeEvent(event)

    def change_unlock_password(self):
        from .lock import change_unlock_password
        change_unlock_password(self)

    def work_dir(self) -> str:
        return self.dir_combo.currentText().strip()

    def selected_project(self) -> str:
        items = self.project_list.selectedItems()
        if not items:
            return ""
        item = items[0]
        # 分组头节点不算选中项目
        if item.data(0, _GROUP_ROLE):
            return ""
        return item.data(0, Qt.UserRole) or item.text(0)

    def repo_path(self) -> str:
        proj = self.selected_project()
        return os.path.join(self.work_dir(), proj) if proj else ""

    def _is_old_education_project(self, project: str = "") -> bool:
        project = project or self.selected_project()
        if not project:
            return False
        note = self.cfg.get("project_notes", {}).get(project, "")
        project_key = project.strip().lower()
        note_key = note.replace(" ", "")
        return (
            project_key == "teaching-practice"
            or note_key in ("教育（旧）", "教育(旧)", "教育旧")
            or "教育（旧）" in note_key
            or "教育(旧)" in note_key
            or "教育旧" in note_key
        )

    def _refresh_logo_button_visibility(self):
        if hasattr(self, "upload_logo_btn"):
            is_visible = self._is_old_education_project()
            for widget in (self.logo_label, self.upload_logo_btn, self.logo_file_label, self.preview_logo_btn, self.clear_logo_btn):
                widget.setVisible(is_visible)
                widget.setEnabled(is_visible)
            if is_visible:
                has_logo = bool(getattr(self, "logo_file_path", ""))
                self.preview_logo_btn.setEnabled(has_logo)
                self.clear_logo_btn.setEnabled(has_logo)
            if not is_visible:
                self.clear_logo_file()

    def current_branch_text(self) -> str:
        """从顶部标签取当前分支名（已去掉"当前分支："前缀）。"""
        return self.cur_branch_label.text().replace("当前分支：", "").strip()

    def require_project(self) -> str:
        """返回选中项目的路径；未选中时提示并返回空串。"""
        repo = self.repo_path()
        if not repo:
            QMessageBox.warning(self, "提示", "请先在左侧选中一个项目")
            return ""
        return repo

    # 日志配色：默认值对应深色主题，浅色主题在 _apply_log_colors 里覆盖
    LOG_COLORS = {
        "normal": "#d4d4d4",   # 普通输出
        "cmd": "#4fc1ff",      # 命令行 $ git ...
        "ok": "#4ec9b0",       # 成功/绿色
        "err": "#f48771",      # 错误/红色
        "hint": "#808080",     # 提示行
        "divider": "#c586c0",  # 分隔线
        "green": "#4ec9b0",    # status 已暂存
        "red": "#f48771",      # status 未暂存
    }

    def _apply_log_colors(self):
        """根据当前主题选择日志配色方案，并设置日志区背景以保证对比度。"""
        if getattr(self, "current_theme", "dark") == "light":
            self.LOG_COLORS = {
                "normal": "#1a1a1a", "cmd": "#0050b3", "ok": "#157f3b",
                "err": "#c1121f", "hint": "#5a5a5a", "divider": "#7b1fa2",
                "green": "#157f3b", "red": "#c1121f",
            }
            log_bg = "#ffffff"
        else:
            self.LOG_COLORS = {
                "normal": "#ffffff", "cmd": "#6bc5ff", "ok": "#5fdbaa",
                "err": "#ff8a80", "hint": "#aaaaaa", "divider": "#e0a0f0",
                "green": "#5fdbaa", "red": "#ff8a80",
            }
            log_bg = "#1e1e1e"
        if hasattr(self, "log_view"):
            # 只设背景；文字颜色由 QTextCharFormat 在字符格式层控制，不受 QSS 干扰
            self.log_view.setStyleSheet(f"QTextBrowser, QTextEdit {{ background-color: {log_bg}; }}")

    def _make_fmt(self, color: str) -> QTextCharFormat:
        """构造带前景色的字符格式，直接写入文档层，不受任何 QSS 覆盖。"""
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        return fmt

    def log(self, text: str, color: str = None):
        """用 QTextCharFormat+insertText 写入日志，完全绕过 QSS/调色板干扰。"""
        ts = datetime.now().strftime("%H:%M:%S")
        col = color or self.LOG_COLORS["normal"]
        fmt = self._make_fmt(col)
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        for line in text.rstrip().splitlines() or [""]:
            cursor.insertText(f"{ts}  {line}", fmt)
            cursor.insertBlock()
        self.log_view.setTextCursor(cursor)
        self.log_view.moveCursor(QTextCursor.End)

    def log_html(self, html_text: str):
        """写入 HTML 内容的日志，常用于超链接。"""
        ts = datetime.now().strftime("%H:%M:%S")
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(f"{ts}  ", self._make_fmt(self.LOG_COLORS["hint"]))
        cursor.insertHtml(html_text)
        cursor.insertBlock()
        self.log_view.setTextCursor(cursor)
        self.log_view.moveCursor(QTextCursor.End)

    def log_raw(self, text: str, color: str = None):
        """直接追加原始文字，不带时间戳前缀，用于实时流式输出。"""
        col = color or self.LOG_COLORS["normal"]
        fmt = self._make_fmt(col)
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text, fmt)
        self.log_view.setTextCursor(cursor)
        self.log_view.moveCursor(QTextCursor.End)

    def log_divider(self, title: str):
        """在日志中插入醒目的分隔线，标明当前项目/分支。"""
        fmt = self._make_fmt(self.LOG_COLORS["divider"])
        sep = "─" * 24
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertBlock()
        cursor.insertText(f"{sep}  【 {title} 】  {sep}", fmt)
        cursor.insertBlock()
        self.log_view.setTextCursor(cursor)
        self.log_view.moveCursor(QTextCursor.End)

    def log_result(self, r):
        self.log(f"$ {r.cmd}", self.LOG_COLORS["cmd"])
        if r.output:
            self.log(r.output, self.LOG_COLORS["normal"] if r.ok else self.LOG_COLORS["err"])
        if not r.ok and not r.output:
            self.log("(命令失败，无输出)", self.LOG_COLORS["err"])

    def set_busy(self, busy: bool, message: str = ""):
        for b in self._busy_buttons:
            b.setEnabled(not busy)
        self.statusBar().showMessage(message or ("执行中…" if busy else "就绪"))

    def run_async(self, fn, on_done, *args, busy_msg: str = "执行中…", **kwargs):
        """在后台线程执行 fn，完成后在主线程回调 on_done(result)。"""
        self.set_busy(True, busy_msg)
        worker = Worker(fn, *args, **kwargs)
        self.workers.append(worker)

        def cleanup():
            self.set_busy(False)
            if worker in self.workers:
                self.workers.remove(worker)

        def ok(result):
            cleanup()
            on_done(result)

        def fail(err):
            cleanup()
            self.log(err, self.LOG_COLORS["err"])
            QMessageBox.critical(self, "出错了", err)

        worker.finished_ok.connect(ok)
        worker.failed.connect(fail)
        worker.start()

    # ---------------- 左侧功能 ----------------

    def _browse_work_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.work_dir() or os.path.expanduser("~"))
        if d:
            self.dir_combo.setCurrentText(d)
            self.scan_projects()

    def scan_projects(self):
        wd = self.work_dir()
        if not wd:
            QMessageBox.warning(self, "提示", "请先填写或选择工作目录")
            return
        if not os.path.isdir(wd):
            QMessageBox.warning(self, "提示", f"目录不存在：\n{wd}")
            return
        cfg_mod.remember_work_dir(self.cfg, wd)
        cfg_mod.save_config(self.cfg)
        # 刷新下拉历史（保留当前文本）
        self.dir_combo.blockSignals(True)
        cur = self.dir_combo.currentText()
        self.dir_combo.clear()
        self.dir_combo.addItems(self.cfg["work_dirs"])
        self.dir_combo.setCurrentText(cur)
        self.dir_combo.blockSignals(False)

        self.log(f"扫描目录：{wd}", self.LOG_COLORS["cmd"])
        self.run_async(git_ops.scan_projects, self._on_projects_scanned, wd, busy_msg="正在扫描目录…")

    def _on_projects_scanned(self, projects: list):
        self.project_list.blockSignals(True)
        self.project_list.clear()
        notes  = self.cfg.get("project_notes", {})
        groups = self.cfg.get("project_groups", {})   # {group: [proj, ...]}

        # 按保存的分组顺序分配项目；未在任何分组中的归入「未分组」
        assigned = set()
        ordered_groups = list(groups.keys())
        # 确保「未分组」在末尾（如果存在）
        if DEFAULT_GROUP in ordered_groups:
            ordered_groups.remove(DEFAULT_GROUP)
            ordered_groups.append(DEFAULT_GROUP)

        def make_proj_item(grp_node, name):
            item = QTreeWidgetItem(grp_node)
            item.setData(0, Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable)
            item.setCheckState(0, Qt.Unchecked)
            note = notes.get(name, "")
            item.setText(0, f"{name}    [{note}]" if note else name)
            if note:
                item.setToolTip(0, note)
            return item

        proj_set = set(projects)
        for grp_name in ordered_groups:
            members = [p for p in groups[grp_name] if p in proj_set]
            if not members:
                continue
            grp_node = self.project_list.find_or_create_group(grp_name)
            for name in members:
                make_proj_item(grp_node, name)
                assigned.add(name)

        # 剩余未分组项目
        unassigned = [p for p in projects if p not in assigned]
        if unassigned:
            grp_node = self.project_list.find_or_create_group(DEFAULT_GROUP)
            for name in unassigned:
                make_proj_item(grp_node, name)

        self.project_list.blockSignals(False)
        self.project_count_label.setText(f"项目列表 ({len(projects)})（右键可快捷全选/设置备注/管理分组）")
        self.log(f"共发现 {len(projects)} 个 git 项目")
        last = self.cfg.get("last_project", "")
        if last in proj_set:
            for it in self.project_list.all_project_items():
                if it.data(0, Qt.UserRole) == last:
                    self.project_list.setCurrentItem(it)
                    break

    def _project_menu(self, pos):
        item = self.project_list.itemAt(pos)
        # 分组头节点不弹项目菜单
        is_group = item and item.data(0, _GROUP_ROLE)
        is_proj  = item and not is_group

        menu = QMenu(self)
        act_new_group = menu.addAction("📁 新建分组…")

        act_move   = None
        act_open   = None
        act_note   = None
        act_clear  = None
        act_rename_grp = None
        act_del_grp    = None
        act_to_top     = None
        act_move_up    = None

        if is_proj:
            name  = item.data(0, Qt.UserRole)
            notes = self.cfg.setdefault("project_notes", {})
            menu.addSeparator()
            act_open  = menu.addAction("📂 打开目录")
            act_note  = menu.addAction("📝 设置/修改备注…")
            act_clear = menu.addAction("❌ 清除备注")
            act_clear.setEnabled(bool(notes.get(name)))
            # 移至分组子菜单
            move_menu = menu.addMenu("➡ 移至分组")
            act_move = {}
            for gn in self.project_list.group_names():
                act_move[move_menu.addAction(gn)] = gn
            act_move[move_menu.addAction("+ 新建分组…")] = None

        if is_group:
            grp_name = item.text(0)
            root = self.project_list.invisibleRootItem()
            grp_idx = root.indexOfChild(item)
            is_ungrouped = (grp_name == DEFAULT_GROUP)
            menu.addSeparator()
            act_to_top = menu.addAction("⬆ 置顶")
            act_move_up = menu.addAction("↑ 上移一位")
            # 未分组 或 已在最顶 则禁用
            act_to_top.setEnabled(not is_ungrouped and grp_idx > 0)
            act_move_up.setEnabled(not is_ungrouped and grp_idx > 0)
            menu.addSeparator()
            act_rename_grp = menu.addAction("✏ 重命名分组…")
            act_del_grp    = menu.addAction("🗑 删除分组（项目移至未分组）")

        action = menu.exec_(self.project_list.mapToGlobal(pos))
        if not action:
            return

        # ── 新建分组 ─────────────────────────────────────────────────────────
        if action is act_new_group:
            gname, ok = QInputDialog.getText(self, "新建分组", "分组名称：")
            if ok and gname.strip():
                self.project_list.find_or_create_group(gname.strip())
                self._save_group_order()
            return

        # ── 分组头操作 ───────────────────────────────────────────────────────
        if is_group:
            grp_name = item.text(0)
            root = self.project_list.invisibleRootItem()
            grp_idx = root.indexOfChild(item)
            if action is act_to_top:
                root.takeChild(grp_idx)
                root.insertChild(0, item)
                self._save_group_order()
            elif action is act_move_up:
                if grp_idx > 0:
                    root.takeChild(grp_idx)
                    root.insertChild(grp_idx - 1, item)
                    self._save_group_order()
            elif action is act_rename_grp:
                new_name, ok = QInputDialog.getText(
                    self, "重命名分组", "新分组名：", text=grp_name)
                if ok and new_name.strip() and new_name.strip() != grp_name:
                    item.setText(0, new_name.strip())
                    self._save_group_order()
            elif action is act_del_grp:
                # 把该组下所有项目移到「未分组」
                ungrp = self.project_list.find_or_create_group(DEFAULT_GROUP)
                while item.childCount():
                    child = item.takeChild(0)
                    ungrp.addChild(child)
                root.takeChild(root.indexOfChild(item))
                self._save_group_order()
            return


        # ── 项目操作 ─────────────────────────────────────────────────────────
        if is_proj:
            name  = item.data(0, Qt.UserRole)
            notes = self.cfg.setdefault("project_notes", {})

            if action is act_open:
                proj_path = os.path.join(self.work_dir(), name)
                if os.path.exists(proj_path):
                    try:
                        os.startfile(proj_path)
                    except Exception as e:
                        QMessageBox.warning(self, "错误", f"无法打开目录:\n{e}")
                else:
                    QMessageBox.warning(self, "错误", f"该目录不存在:\n{proj_path}")

            elif action is act_note:
                text, ok = QInputDialog.getText(
                    self, "项目备注", f"为 [{name}] 设置备注：", text=notes.get(name, ""))
                if ok:
                    text = text.strip()
                    if text:
                        notes[name] = text
                    else:
                        notes.pop(name, None)
                    self._apply_note_to_item(item, name)
                    cfg_mod.save_config(self.cfg)

            elif action is act_clear:
                notes.pop(name, None)
                self._apply_note_to_item(item, name)
                cfg_mod.save_config(self.cfg)

            elif act_move and action in act_move:
                dest_grp_name = act_move[action]
                if dest_grp_name is None:
                    # 新建分组再移入
                    dest_grp_name, ok = QInputDialog.getText(self, "新建分组", "分组名称：")
                    if not ok or not dest_grp_name.strip():
                        return
                    dest_grp_name = dest_grp_name.strip()
                src_root = item.parent() or self.project_list.invisibleRootItem()
                src_root.removeChild(item)
                dest_root = self.project_list.find_or_create_group(dest_grp_name)
                dest_root.addChild(item)
                dest_root.setExpanded(True)
                self.project_list.setCurrentItem(item)
                self._save_group_order()

    def _apply_note_to_item(self, item, name: str):
        note = self.cfg.get("project_notes", {}).get(name, "")
        item.setText(0, f"{name}    [{note}]" if note else name)
        item.setToolTip(0, note)
        if name == self.selected_project():
            self._refresh_logo_button_visibility()

    def _save_group_order(self, *_):
        """将当前树的分组结构持久化到 cfg["project_groups"]。"""
        self.cfg["project_groups"] = self.project_list.dump_order()
        cfg_mod.save_config(self.cfg)

    def _dir_history_menu(self, pos):
        view = self.dir_combo.view()
        idx = view.indexAt(pos)
        if not idx.isValid():
            return
        path = self.dir_combo.itemText(idx.row())
        menu = QMenu(self)
        act_del = menu.addAction(f"删除该历史记录")
        if menu.exec_(view.mapToGlobal(pos)) is act_del:
            self._remove_dir_history(idx.row(), path)

    def _delete_current_dir(self):
        """删除下拉框当前显示的目录历史（配合 ✕ 按钮）。"""
        path = self.dir_combo.currentText().strip()
        if not path:
            return
        idx = self.dir_combo.findText(path)
        if idx < 0:
            QMessageBox.information(self, "提示", "该目录不在历史列表中，无需删除")
            return
        if QMessageBox.question(
            self, "确认删除", f"从历史中删除该目录？\n\n{path}",
        ) != QMessageBox.Yes:
            return
        self._remove_dir_history(idx, path)

    def _remove_dir_history(self, row: int, path: str):
        self.dir_combo.removeItem(row)
        self.cfg["work_dirs"] = [
            d for d in self.cfg.get("work_dirs", [])
            if os.path.normpath(d) != os.path.normpath(path)
        ]
        cfg_mod.save_config(self.cfg)
        self.log(f"已删除目录历史：{path}")

    def toggle_theme(self):
        """在深色 / 浅色之间切换主题并记忆。"""
        from .theme import apply_theme
        from PyQt5.QtWidgets import QApplication
        new_theme = "light" if getattr(self, "current_theme", "dark") == "dark" else "dark"
        ok = apply_theme(QApplication.instance(), new_theme)
        if not ok:
            QMessageBox.warning(self, "提示", "未安装 PyQtDarkTheme，已回退到系统样式")
        self.current_theme = new_theme
        self.cfg["theme"] = new_theme
        cfg_mod.save_config(self.cfg)
        self.theme_btn.setText("☀ 浅色" if new_theme == "light" else "☾ 深色")
        self._apply_log_colors()
        if hasattr(self, "teach_table"):
            self._apply_teach_table_style()
        if hasattr(self, "nav_frame"):
            self._apply_nav_style()
        # 旧日志用旧主题颜色写入，切换背景后颜色对比度失效，清空重来
        self.log_view.clear()
        label = "浅色" if new_theme == "light" else "深色"
        self.log(f"已切换到{label}主题", self.LOG_COLORS["hint"])
        # 若当前有选中项目，重新打印分隔线并获取分支，恢复上下文
        proj = self.selected_project()
        if proj:
            self.log_divider(f"项目：{proj}")
            self.fetch_branches()

    def clone_repo(self):
        wd = self.work_dir()
        dlg = CloneDialog(wd if os.path.isdir(wd) else os.path.expanduser("~"), self)
        if dlg.exec_() != QDialog.Accepted:
            return
        url, target = dlg.values()
        self.log(f"开始克隆：{url} → {target}", self.LOG_COLORS["cmd"])

        def do_clone():
            return git_ops.run_git(target, "clone", url, timeout=600)

        def done(r):
            self.log_result(r)
            if r.ok:
                QMessageBox.information(self, "完成", "克隆成功")
                if os.path.normpath(target) == os.path.normpath(self.work_dir()):
                    self.scan_projects()
            else:
                QMessageBox.critical(self, "克隆失败", r.output or "未知错误")

        self.run_async(do_clone, done, busy_msg="正在克隆仓库…（视仓库大小可能较久）")

    def _on_project_selected(self):
        proj = self.selected_project()
        if not proj:
            self._refresh_logo_button_visibility()
            return
        self.cfg["last_project"] = proj
        self._refresh_logo_button_visibility()
        
        self.branch_combo.clear()
        self.base_branch_label.setText("—")
        self._refresh_old_ip_options(proj)
        self.new_ip_edit.clear()  # 项目不共享新IP，切换时清空
        
        self.log_divider(f"项目：{proj}")
        
        repo_path = self.repo_path()
        vue_ver, node_ver = self._detect_project_vue_and_node(repo_path)
        if vue_ver:
            self.cur_project_label.setText(f"当前项目：{proj} (Vue {vue_ver})")
            self.log(f"检测到项目 {proj} 为 Vue {vue_ver}，打包时将自动使用 Node {node_ver}", self.LOG_COLORS["red"])
        else:
            self.cur_project_label.setText(f"当前项目：{proj}")
            
        self.fetch_branches()

    def _refresh_old_ip_options(self, project: str):
        """用该项目执行过替换的 new_ip 填充旧IP候选，项目不共享旧IP输入。"""
        ips = cfg_mod.old_ips_for_project(self.cfg, project)
        self.old_ip_edit.blockSignals(True)
        self.old_ip_edit.clear()
        self.old_ip_edit.addItems(ips)
        # 默认显示该项目最近一次的旧IP（即历史第一条），没有则空
        self.old_ip_edit.setCurrentIndex(0 if ips else -1)
        self.old_ip_edit.blockSignals(False)

    def _refresh_branch_name_history(self):
        """用全局分支名历史刷新下拉，保留当前编辑内容；note 存为 userData。"""
        cur_name = self.new_branch_edit.currentText()
        history = cfg_mod.branch_name_history(self.cfg)
        self.new_branch_edit.blockSignals(True)
        self.new_branch_edit.clear()
        for entry in history:
            self.new_branch_edit.addItem(entry["name"], entry["note"])
        self.new_branch_edit.setCurrentText(cur_name)
        self.new_branch_edit.blockSignals(False)

    def _on_branch_name_selected(self, index: int):
        """从下拉选中历史条目时，自动填入备注并更新标签。"""
        if index < 0:
            return
        note = self.new_branch_edit.itemData(index) or ""
        self.branch_note_edit.setText(note)
        self._update_tags_from_note(note)

    def _delete_branch_name(self):
        """从历史中删除当前条目（不影响实际 git 分支）。"""
        name = self.new_branch_edit.currentText().strip()
        if not name:
            return
        names = [e["name"] for e in cfg_mod.branch_name_history(self.cfg)]
        if name not in names:
            QMessageBox.information(self, "提示", "该名称不在历史记录中，无需删除")
            return
        cfg_mod.remove_branch_name(self.cfg, name)
        cfg_mod.save_config(self.cfg)
        self._refresh_branch_name_history()
        self.new_branch_edit.setCurrentText("")
        self.branch_note_edit.clear()
        self._update_tags_from_note("")   # 隐藏标签
        self.log(f"已从历史中移除分支名：{name}", self.LOG_COLORS["hint"])

    def _open_branch_config(self):
        """弹出配置模块选择对话框，结果写入内部备注并更新标签。"""
        dlg = BranchConfigDialog(self, initial_note=self.branch_note_edit.text())
        if dlg.exec_() == QDialog.Accepted:
            note = dlg.get_note()
            self.branch_note_edit.setText(note)
            self._update_tags_from_note(note)

    def _update_tags_from_note(self, note: str):
        """根据备注内容控制产业/实训标签的显隐。"""
        has_chanye = "产业（教学）" in note or "教育产品线-" in note
        self.tag_chanye.setVisible(has_chanye)
        self.tag_shixun.setVisible("实训" in note)

    def _show_config_detail(self, kind: str):
        """点击标签后弹出详情对话框。"""
        note = self.branch_note_edit.text()
        if kind == "chanye":
            QMessageBox.information(self, "产业（教学）配置",
                                    self._format_chanye_detail(note))
        elif kind == "shixun":
            QMessageBox.information(self, "实训（大屏）配置",
                                    self._format_shixun_detail(note))


    def _on_cur_branch_changed(self, branch_name: str):
        """branch_combo 切换时：同步基准分支标签 + 刷新当前分支配置标签。"""
        if hasattr(self, "base_branch_label"):
            self.base_branch_label.setText(branch_name or "—")
        self._refresh_cur_branch_cfg(branch_name)

    def _refresh_cur_branch_cfg(self, branch_name: str):
        """根据分支名查找已保存的配置备注，更新 combo 旁的配置标签与打标签按钮。"""
        note = ""
        for entry in cfg_mod.branch_name_history(self.cfg):
            if entry.get("name") == branch_name:
                note = entry.get("note", "")
                break
        has_chanye = "产业（教学）" in note or "教育产品线-" in note
        has_shixun = "实训" in note
        self.cur_cfg_tag_chanye.setVisible(has_chanye)
        self.cur_cfg_tag_shixun.setVisible(has_shixun)
        
        # 如果已经配置了“产业”或“实训”之一，则隐藏“⚙ 打标签”按钮；若均未配置，则显示。
        self.edit_cfg_btn.setVisible(not (has_chanye or has_shixun))
        
        # 保存当前分支的备注供编辑使用
        self._cur_branch_note = note

    def _edit_cur_branch_config(self):
        """弹出配置对话框，修改或新增当前选中分支的配置。"""
        branch_name = self.branch_combo.currentText().strip()
        if not branch_name:
            QMessageBox.warning(self, "提示", "当前没有选中的分支！")
            return
            
        # 获取当前已保存的备注（如果有）
        note = getattr(self, "_cur_branch_note", "")
        
        dlg = BranchConfigDialog(self, initial_note=note)
        if dlg.exec_() == QDialog.Accepted:
            new_note = dlg.get_note()
            
            # 保存到配置中！
            cfg_mod.add_branch_name(self.cfg, branch_name, new_note)
            cfg_mod.save_config(self.cfg)
            
            # 同步更新教学部署列表中对应学校的配置模块
            if branch_name.startswith("eduLocal-"):
                school_name = self._extract_school_name(branch_name)
                target_item = self._find_teach_deployment(branch_name, school_name)
                if target_item:
                    target_item["branch_name"] = branch_name
                    target_item["config_note"] = new_note
                    cfg_mod.save_config(self.cfg)
                    self.teach_data = list(self.cfg["teach_deployments"])
                    self._render_teach_table()
            
            # 重新刷新当前分支的配置显示
            self._refresh_cur_branch_cfg(branch_name)
            
            # 同时刷新新建分支区域的配置下拉历史，以便保持最新的数据一致性
            self._refresh_branch_name_history()
            
            self.log(f"已更新分支 [{branch_name}] 的配置并同步至部署列表", self.LOG_COLORS["ok"])

    @staticmethod
    def _format_chanye_detail(note: str) -> str:
        parts = []
        if "产业（教学）" in note:
            parts.append("✔ 产业（教学）")
        products = [name for name in BranchConfigDialog.EDU_PRODUCT_LINES if name in note]
        if products:
            parts.append("✔ 教育产品线：")
            parts.extend([f"    • {name}" for name in products])
        return "已选择模块：\n\n" + "\n".join(parts) if parts else "未保存产业/教育产品线配置"

    @staticmethod
    def _format_shixun_detail(note: str) -> str:
        """将备注字符串按分组格式化展示。
        新格式：  实训：[实训-舆情]··· | [实训-客流]···
        旧格式：  实训：舆情监测，客流趋势分析，…（平面逗号列表）
        """
        if not note:
            return "未保存实训配置"

        shixun_part = ""
        if "实训：" in note:
            shixun_part = note[note.find("实训：") + 3:].strip()
        else:
            # 如果有 " | " 隔离，取不是 "产业（教学）" 的那一半
            parts = note.split(" | ")
            for p in parts:
                p = p.strip()
                if p and p != "产业（教学）":
                    shixun_part = p
                    break
            if not shixun_part and "产业（教学）" not in note:
                shixun_part = note.strip()

        if not shixun_part:
            return "实训（大屏）未选择子模块"

        lines = []

        # — 新格式：包含 [实训-XXX] 分组标头
        if "[实训-" in shixun_part:
            for segment in shixun_part.split(" | "):
                seg = segment.strip()
                if seg.startswith("[实训-"):
                    end = seg.find("]")
                    cat = seg[5:end]   # e.g. "舆情"
                    mods = seg[end+1:]
                    lines.append(f"▪ {cat}")
                    # 支持中文和英文逗号
                    mod_list = [m.strip() for m in mods.replace(",", "，").split("，") if m.strip()]
                    for m in mod_list:
                        lines.append(f"    • {m}")
                elif seg:
                    lines.append(seg)

        # — 旧格式：逗号平面列表，用 SHIXUN_GROUPS 归类
        else:
            # 支持中文和英文逗号
            all_mods = [m.strip() for m in shixun_part.replace(",", "，").split("，") if m.strip()]
            categorized: dict[str, list] = {}
            uncategorized = []
            cat_map = {}
            for cat_name, _, modules in BranchConfigDialog.SHIXUN_GROUPS:
                for mod_name, _ in modules:
                    cat_map[mod_name] = cat_name
            for m in all_mods:
                cat = cat_map.get(m)
                if cat:
                    categorized.setdefault(cat, []).append(m)
                else:
                    uncategorized.append(m)
            for cat_name, _, _ in BranchConfigDialog.SHIXUN_GROUPS:
                if cat_name in categorized:
                    lines.append(f"▪ {cat_name}")
                    for m in categorized[cat_name]:
                        lines.append(f"    • {m}")
            if uncategorized:
                lines.append(f"▪ 其他")
                for m in uncategorized:
                    lines.append(f"    • {m}")

        return "已保存模块：\n\n" + "\n".join(lines) if lines else "实训（大屏）未选择子模块"

    def _delete_old_ip(self):
        """删除当前旧IP下拉中选中的 IP（从 ip_history 中移除该项目的相关记录）。"""
        ip = self.old_ip_edit.currentText().strip()
        proj = self.selected_project()
        if not ip:
            return
        if not proj:
            QMessageBox.warning(self, "提示", "请先选中一个项目")
            return
        if QMessageBox.question(
            self, "确认删除", f"从当前项目的旧IP候选中删除：\n\n{ip}\n\n确定吗？"
        ) != QMessageBox.Yes:
            return
        cfg_mod.remove_old_ip_for_project(self.cfg, proj, ip)
        cfg_mod.save_config(self.cfg)
        self._refresh_old_ip_options(proj)
        self.log(f"已删除旧IP候选：{ip}", self.LOG_COLORS["hint"])

    # ---------------- 右侧功能 ----------------

    def fetch_branches(self):
        repo = self.require_project()
        if not repo:
            return

        def fetch():
            fetch_result = git_ops.run_git(repo, "fetch", "--all", "--prune", timeout=300)
            local = git_ops.local_branches(repo)
            remote = git_ops.remote_branches(repo)
            return fetch_result, local, remote

        def done(result):
            fetch_result, (r, branches, current), (rr, remotes) = result
            self.log_result(fetch_result)
            self.log_result(r)
            if not r.ok:
                return
            self.branch_combo.clear()
            self.branch_combo.addItems(branches)
            if current:
                self.branch_combo.setCurrentText(current)
                self.cur_branch_label.setText(f"当前分支：{current}")
            self.log(f"共 {len(branches)} 个本地分支，当前分支：{current or '(未知)'}")
            # 远端分支
            self.remote_list.clear()
            local_branch_set = set(branches)
            for remote_branch in remotes:
                item = QListWidgetItem(remote_branch)
                item.setData(Qt.UserRole, remote_branch)
                local_name = self._local_name_from_remote(remote_branch)
                if local_name in local_branch_set:
                    item.setText(f"{remote_branch}    [本地已存在]")
                    item.setToolTip(f"{remote_branch}\n本地已存在同名分支：{local_name}")
                    item.setForeground(QColor("#8bc34a"))
                self.remote_list.addItem(item)
            self.toggle_remote_btn.setText(f"远端分支（{len(remotes)}）")
            self.log(f"共 {len(remotes)} 个远端分支")

        self.run_async(fetch, done, busy_msg="正在读取分支…")

    def _toggle_remote_panel(self, checked: bool):
        self.remote_list.setVisible(checked)
        self.toggle_remote_btn.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _remote_branch_menu(self, pos):
        item = self.remote_list.itemAt(pos)
        if not item:
            return
        self.remote_list.setCurrentItem(item)
        remote_branch = item.data(Qt.UserRole) or item.text().strip()
        menu = QMenu(self)
        act_pull = menu.addAction(f"拉取此分支到本地：{remote_branch}")
        action = menu.exec_(self.remote_list.mapToGlobal(pos))
        if action == act_pull:
            self.checkout_remote_branch()

    @staticmethod
    def _local_name_from_remote(remote_branch: str) -> str:
        remote_branch = (remote_branch or "").strip()
        if "/" not in remote_branch:
            return remote_branch
        return remote_branch.split("/", 1)[1]

    def checkout_remote_branch(self):
        repo = self.require_project()
        if not repo:
            return

        item = self.remote_list.currentItem()
        remote_branch = (item.data(Qt.UserRole) or item.text()).strip() if item else ""
        if not remote_branch:
            QMessageBox.warning(self, "提示", "请先在远端分支列表中选择一个分支")
            return
        if "->" in remote_branch:
            QMessageBox.warning(self, "提示", "请选择具体远端分支，不要选择 HEAD 指针")
            return

        local_branch = self._local_name_from_remote(remote_branch)
        if not local_branch:
            QMessageBox.warning(self, "提示", f"无法识别远端分支名称：{remote_branch}")
            return
        if "/" not in remote_branch:
            QMessageBox.warning(self, "提示", f"远端分支格式不正确：{remote_branch}")
            return

        def check_and_checkout_remote():
            r, files = git_ops.dirty_files(repo)
            if not r.ok:
                return ("error", r, None)
            if files:
                return ("dirty", r, files)

            local_r, local_branches, _ = git_ops.local_branches(repo)
            if not local_r.ok:
                return ("error", local_r, None)

            if local_branch in local_branches:
                cr = git_ops.run_git(repo, "checkout", local_branch)
                return ("done", cr, local_branch)

            remote_name, _ = remote_branch.split("/", 1)
            fr = git_ops.run_git(repo, "fetch", remote_name, "--prune", timeout=300)
            if not fr.ok:
                return ("error", fr, None)

            cr = git_ops.run_git(repo, "checkout", "--track", "-b", local_branch, remote_branch)
            return ("done", cr, local_branch)

        def done(result):
            kind, r, checked_out_branch = result
            if kind == "error":
                self.log_result(r)
            elif kind == "dirty":
                files = checked_out_branch
                self.log(f"拉取远端分支被阻止：存在 {len(files)} 个未提交修改", self.LOG_COLORS["err"])
                QMessageBox.warning(
                    self, "有未提交代码",
                    "当前分支存在未提交的修改，请先 commit 或处理后再拉取远端分支：\n\n"
                    + "\n".join(files[:30]) + ("\n…" if len(files) > 30 else ""),
                )
            else:
                self.log_result(r)
                if r.ok:
                    self.cur_branch_label.setText(f"当前分支：{checked_out_branch}")
                    if self.branch_combo.findText(checked_out_branch) < 0:
                        self.branch_combo.addItem(checked_out_branch)
                    self.branch_combo.setCurrentText(checked_out_branch)
                    self.log_divider(f"{self.selected_project()} @ {checked_out_branch}")
                    self.log(f"已从远端分支 {remote_branch} 拉取并切换到本地分支：{checked_out_branch}", self.LOG_COLORS["ok"])
                    self.fetch_branches()

        self.run_async(check_and_checkout_remote, done, busy_msg="正在拉取远端分支…")

    def checkout_branch(self):
        repo = self.require_project()
        if not repo:
            return
        target = self.branch_combo.currentText().strip()
        if not target:
            QMessageBox.warning(self, "提示", "请先获取并选择分支")
            return
        # 选中的就是当前分支，无需切换
        if target == self.current_branch_text():
            return

        def check_and_checkout():
            r, files = git_ops.dirty_files(repo)
            if not r.ok:
                return ("error", r, None)
            if files:
                return ("dirty", r, files)
            cr = git_ops.run_git(repo, "checkout", target)
            return ("done", cr, None)

        def done(result):
            kind, r, files = result
            if kind == "error":
                self.log_result(r)
                # 恢复原分支选择展示
                orig = self.current_branch_text()
                if orig:
                    self.branch_combo.setCurrentText(orig)
            elif kind == "dirty":
                self.log(f"切换被阻止：存在 {len(files)} 个未提交修改", self.LOG_COLORS["err"])
                # 恢复原分支选择展示
                orig = self.current_branch_text()
                if orig:
                    self.branch_combo.setCurrentText(orig)
                QMessageBox.warning(
                    self, "有未提交代码",
                    "当前分支存在未提交的修改，请先 commit 或处理后再切换：\n\n"
                    + "\n".join(files[:30]) + ("\n…" if len(files) > 30 else ""),
                )
            else:
                self.log_result(r)
                if r.ok:
                    self.cur_branch_label.setText(f"当前分支：{target}")
                    self.log_divider(f"{self.selected_project()} @ {target}")
                    self.log(f"已切换到分支：{target}", self.LOG_COLORS["ok"])
                else:
                    # 恢复原分支选择展示
                    orig = self.current_branch_text()
                    if orig:
                        self.branch_combo.setCurrentText(orig)

        self.run_async(check_and_checkout, done, busy_msg="正在切换分支…")

    def delete_branch(self):
        """删除分支：弹窗选择目标分支（不依赖 branch_combo，可独立选择）。"""
        repo = self.require_project()
        if not repo:
            return
        current = self.current_branch_text()

        # 获取本地分支列表（排除当前分支）
        all_branches = [self.branch_combo.itemText(i)
                        for i in range(self.branch_combo.count())
                        if self.branch_combo.itemText(i) != current]
        if not all_branches:
            QMessageBox.information(self, "提示", "没有其他可删除的分支（当前分支无法删除）")
            return

        # 获取远端分支列表以用于存在性判断
        remotes = [self.remote_list.item(i).text().strip() for i in range(self.remote_list.count())]

        # 弹出选择对话框
        dlg = QDialog(self)
        dlg.setWindowTitle("删除分支")
        dlg_lay = QVBoxLayout(dlg)
        dlg_lay.addWidget(QLabel("选择要删除的分支："))
        branch_picker = QComboBox()
        branch_picker.addItems(all_branches)
        dlg_lay.addWidget(branch_picker)

        # 远端分支存在性状态提示
        status_label = QLabel()
        dlg_lay.addWidget(status_label)

        dlg_lay.addWidget(QLabel("删除范围："))
        r_local  = QRadioButton("仅本地")
        r_remote = QRadioButton("仅远端（origin）")
        r_both   = QRadioButton("本地 + 远端（origin）")
        r_both.setChecked(True)
        grp = QButtonGroup(dlg)
        for rb in (r_local, r_remote, r_both):
            grp.addButton(rb)
            dlg_lay.addWidget(rb)

        def update_remote_status(target_branch):
            target_branch = target_branch.strip()
            # 只要远端分支列表中任意一项以 /target_branch 结尾，就说明存在远端分支
            has_remote = any(r.endswith(f"/{target_branch}") for r in remotes)
            if has_remote:
                status_label.setText("提示：<span style='color:green; font-weight:bold;'>✔ 存在对应的远端分支</span>")
                r_remote.setEnabled(True)
                r_both.setEnabled(True)
                r_both.setChecked(True)
            else:
                status_label.setText("提示：<span style='color:#e67e22; font-weight:bold;'>⚠ 远端不存在该分支</span>")
                r_remote.setEnabled(False)
                r_both.setEnabled(False)
                r_local.setChecked(True)

        branch_picker.currentTextChanged.connect(update_remote_status)
        update_remote_status(branch_picker.currentText())

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("确认删除")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        dlg_lay.addWidget(btns)
        if dlg.exec_() != QDialog.Accepted:
            return

        target     = branch_picker.currentText().strip()
        del_local  = r_local.isChecked() or r_both.isChecked()
        del_remote = r_remote.isChecked() or r_both.isChecked()

        if del_remote:
            reply = QMessageBox.warning(
                self,
                "安全二次确认",
                f"🚨 警告：您正在请求删除远端分支！\n\n项目：{self.selected_project()}\n分支：origin/{target}\n\n该操作将永久从远程仓库移除该分支，可能影响团队内其他开发人员，且无法直接撤销。是否确认继续删除？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return

        def do_delete():
            results = []
            if del_local:
                r = git_ops.run_git(repo, "branch", "-D", target)
                results.append(("本地", r))
            if del_remote:
                r = git_ops.run_git(repo, "push", "origin", "--delete", target, timeout=120)
                results.append(("远端", r))
            return results

        def done(results):
            all_ok = True
            for label, r in results:
                self.log_result(r)
                if r.ok:
                    self.log(f"{label}分支「{target}」已删除", self.LOG_COLORS["ok"])
                else:
                    all_ok = False
            if all_ok:
                self.fetch_branches()   # 刷新列表

        self.run_async(do_delete, done, busy_msg=f"正在删除分支 {target}…")


    def simple_git(self, action: str):
        repo = self.require_project()
        if not repo:
            return

        def run():
            r = git_ops.run_git(repo, action, timeout=300)
            # push 失败且提示没有 upstream 时，自动加 --set-upstream origin <branch> 重试
            if not r.ok and action == "push" and "no upstream branch" in r.output.lower():
                br = git_ops.run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
                branch = br.stdout.strip()
                if branch:
                    r = git_ops.run_git(repo, "push", "--set-upstream", "origin", branch, timeout=300)
            return r

        def done(r):
            if action == "status" and r.ok:
                self.log(f"$ {r.cmd}", self.LOG_COLORS["cmd"])
                self._log_status_colored(r.stdout)
            else:
                self.log_result(r)
            if r.ok and action in ("pull", "push"):
                self.log(f"{action} 完成", self.LOG_COLORS["ok"])
                self.fetch_branches()   # 自动刷新分支列表

        self.run_async(run, done, busy_msg=f"正在执行 git {action}…")


    def _log_status_colored(self, text: str):
        """按 git 原生配色展示 status：已暂存绿色、未暂存/未跟踪红色。"""
        GREEN = self.LOG_COLORS["green"]
        RED = self.LOG_COLORS["red"]
        NORMAL = self.LOG_COLORS["normal"]
        HINT = self.LOG_COLORS["hint"]
        mode = NORMAL
        for line in text.splitlines():
            stripped = line.strip()
            lower = stripped.lower()
            if lower.startswith("changes to be committed"):
                mode = GREEN
                self.log(line, NORMAL)
                continue
            if (lower.startswith("changes not staged")
                    or lower.startswith("untracked files")
                    or lower.startswith("unmerged paths")):
                mode = RED
                self.log(line, NORMAL)
                continue
            if stripped and not line[0].isspace():
                mode = NORMAL
                self.log(line, NORMAL)
                continue
            if stripped.startswith("(use "):
                self.log(line, HINT)
                continue
            self.log(line, mode if stripped else NORMAL)

    def do_add(self):
        repo = self.require_project()
        if not repo:
            return

        def gather():
            return git_ops.run_git(repo, "status", "--short")

        def done(status):
            self.log_result(status)
            if not status.ok:
                return
            if not status.stdout.strip():
                QMessageBox.information(self, "提示", "工作区干净，没有需要暂存的修改")
                return
            if QMessageBox.question(
                self, "确认 add",
                "将以下修改加入暂存区（git add .）：\n\n"
                + status.stdout.strip() + "\n\n确定吗？",
            ) != QMessageBox.Yes:
                return

            def add():
                return git_ops.run_git(repo, "add", ".")

            def add_done(r):
                self.log_result(r)
                if r.ok:
                    self.log("add 完成，可点击 commit 提交", self.LOG_COLORS["ok"])

            self.run_async(add, add_done, busy_msg="正在暂存…")

        self.run_async(gather, done, busy_msg="正在读取修改状态…")

    def do_commit(self):
        repo = self.require_project()
        if not repo:
            return

        def gather():
            staged = git_ops.run_git(repo, "diff", "--cached", "--name-status")
            unstaged = git_ops.run_git(repo, "status", "--short")
            log = git_ops.run_git(repo, "log", "-3", "--oneline")
            return staged, unstaged, log

        def done(result):
            staged, unstaged, log = result
            if not staged.ok:
                self.log_result(staged)
                return
            if not staged.stdout.strip():
                if unstaged.stdout.strip():
                    QMessageBox.information(
                        self, "提示",
                        "暂存区为空，但工作区有未暂存的修改。\n请先点击 add 暂存后再 commit。")
                else:
                    QMessageBox.information(self, "提示", "暂存区为空，没有可提交的内容")
                return
            dlg = CommitDialog(staged.stdout, log.stdout, self)
            if dlg.exec_() != QDialog.Accepted:
                return
            msg = dlg.message()

            def commit():
                return git_ops.run_git(repo, "commit", "-m", msg)

            def commit_done(r):
                self.log_result(r)
                if r.ok:
                    self.log("提交成功", self.LOG_COLORS["ok"])

            self.run_async(commit, commit_done, busy_msg="正在提交…")

        self.run_async(gather, done, busy_msg="正在读取暂存状态…")

    def do_quick_publish(self):
        repo = self.require_project()
        if not repo:
            return

        def gather():
            status = git_ops.run_git(repo, "status", "--short")
            log = git_ops.run_git(repo, "log", "-3", "--oneline")
            return status, log

        def done(result):
            status, log = result
            if not status.ok:
                self.log_result(status)
                return

            changes = status.stdout.strip()
            # 如果工作区完全干净
            if not changes:
                if QMessageBox.question(
                    self, "一键操作 (Publish)",
                    "当前工作区干净，无任何待提交修改。是否直接执行 git push 推送已提交的代码？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
                ) == QMessageBox.Yes:
                    self.simple_git("push")
                return

            # 如果有修改，弹出输入 commit message 的对话框
            dlg = QuickPublishDialog(changes, log.stdout, self)
            if dlg.exec_() != QDialog.Accepted:
                return
            msg = dlg.message()

            # 执行一键流水线：add -> commit -> push
            def pipeline():
                # 1. git add .
                r = git_ops.run_git(repo, "add", ".")
                if not r.ok:
                    return "add", r

                # 2. git commit -m msg
                r = git_ops.run_git(repo, "commit", "-m", msg)
                if not r.ok:
                    return "commit", r

                # 3. git push
                r = git_ops.run_git(repo, "push", timeout=300)
                # push 失败且提示没有 upstream 时，自动加 --set-upstream origin <branch> 重试
                if not r.ok and "no upstream branch" in r.output.lower():
                    br = git_ops.run_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
                    branch = br.stdout.strip()
                    if branch:
                        r = git_ops.run_git(repo, "push", "--set-upstream", "origin", branch, timeout=300)
                return "push", r

            def pipeline_done(pipeline_result):
                step, r = pipeline_result
                self.log_result(r)
                if r.ok:
                    self.log("⚡ 一键操作 (Add -> Commit -> Push) 已成功完成！", self.LOG_COLORS["ok"])
                    self.fetch_branches()
                else:
                    self.log(f"一键操作在 [{step}] 步骤失败，操作中止。", self.LOG_COLORS["err"])

            self.run_async(pipeline, pipeline_done, busy_msg="正在执行一键操作…")

        self.run_async(gather, done, busy_msg="正在读取修改状态…")

    def _extract_school_name(self, branch_name: str) -> str:
        if not branch_name:
            return ""
        if branch_name.startswith("eduLocal-"):
            rest = branch_name[len("eduLocal-"):]
            parts = rest.split("-")
            if parts:
                return parts[0]
        return branch_name

    def _find_teach_deployment(self, branch_name: str = "", school_name: str = ""):
        deployments = self.cfg.get("teach_deployments", [])
        if branch_name:
            for item in deployments:
                if item.get("branch_name") == branch_name:
                    return item
        if school_name:
            for item in deployments:
                if item.get("school") == school_name:
                    return item
        return None

    def _upsert_teach_deployment_for_branch(self, branch_name: str, new_item: dict) -> str:
        deployments = self.cfg.setdefault("teach_deployments", [])
        new_item["branch_name"] = branch_name
        existing = None
        kept = []
        for item in deployments:
            if item.get("branch_name") == branch_name:
                if existing is None:
                    existing = item
                continue
            kept.append(item)

        if existing is not None:
            old_image_paths = existing.get("image_paths", [])
            old_image_path = existing.get("image_path", "")
            existing.update(new_item)
            if old_image_paths:
                existing["image_paths"] = old_image_paths
                existing["image_path"] = old_image_paths[0]
            elif old_image_path:
                existing["image_path"] = old_image_path
                existing["image_paths"] = [old_image_path]
            kept.insert(0, existing)
            self.cfg["teach_deployments"] = kept
            return "updated"

        kept.insert(0, new_item)
        self.cfg["teach_deployments"] = kept
        return "created"

    def create_branch(self):
        repo = self.require_project()
        if not repo:
            return
        base = self.branch_combo.currentText().strip()
        name = self.new_branch_edit.currentText().strip()
        note = self.branch_note_edit.text().strip()
        if not base:
            QMessageBox.warning(self, "提示", "请先点击『获取分支』并在上方下拉框选择基准分支")
            return
        if not name:
            QMessageBox.warning(self, "提示", "请填写新分支名")
            return
        if not note or note.startswith("（未选"):
            if QMessageBox.question(
                self, "未选择配置",
                "该分支没有选择产业/实训配置，是否继续创建？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            ) != QMessageBox.Yes:
                return
            note = ""   # 无备注
        note_line = f"\n备注：{note}" if note else ""
        if QMessageBox.question(
            self, "确认",
            f"基于 [{base}] 创建新分支：\n\n{name}{note_line}\n\n并自动切换，确定吗？",
        ) != QMessageBox.Yes:
            return

        def create():
            r, files = git_ops.dirty_files(repo)
            if not r.ok:
                return ("error", r, None)
            if files:
                return ("dirty", r, files)
            cr = git_ops.run_git(repo, "checkout", "-b", name, base)
            return ("done", cr, None)

        def done(result):
            kind, r, files = result
            if kind == "error":
                self.log_result(r)
            elif kind == "dirty":
                self.log(f"创建被阻止：存在 {len(files)} 个未提交修改", self.LOG_COLORS["err"])
                QMessageBox.warning(
                    self, "有未提交代码",
                    "当前有未提交的修改，请先提交后再新建分支：\n\n"
                    + "\n".join(files[:30]) + ("\n…" if len(files) > 30 else ""),
                )
            else:
                self.log_result(r)
                if r.ok:
                    self.cur_branch_label.setText(f"当前分支：{name}")
                    # 备注和分支名一起入库，供其他项目复用
                    cfg_mod.add_branch_name(self.cfg, name, note)
                    cfg_mod.save_config(self.cfg)
                    self._refresh_branch_name_history()
                    self.new_branch_edit.setCurrentText("")
                    self.branch_note_edit.clear()
                    self._update_tags_from_note("")   # 新分支编辑区标签清空
                    self.log_divider(f"{self.selected_project()} @ {name}")
                    self.log(f"已创建并切换到新分支：{name}", self.LOG_COLORS["ok"])
                    if note:
                        self.log(f"备注：{note}", self.LOG_COLORS["hint"])
                    
                    # 自动创建部署信息记录
                    school_name = self._extract_school_name(name)
                    base_school = self._extract_school_name(base)
                    from PyQt5.QtCore import QDate
                    current_date = QDate.currentDate().toString("yyyy-MM-dd")
                    
                    if "teach_deployments" not in self.cfg:
                        self.cfg["teach_deployments"] = []
                        
                    new_item = {
                        "school": school_name,
                        "branch_name": name,
                        "version": "新版",
                        "config_note": note,
                        "ip": "",
                        "date": current_date,
                        "image_path": "",
                        "image_paths": [],
                        "remark": "暂无",
                        "base": base_school
                    }
                    pre_ip = self.new_ip_edit.text().strip()
                    if pre_ip:
                        new_item["ip"] = pre_ip
                    
                    upsert_result = self._upsert_teach_deployment_for_branch(name, new_item)
                    cfg_mod.save_config(self.cfg)
                    self.teach_data = list(self.cfg["teach_deployments"])
                    self._render_teach_table()
                    if upsert_result == "updated":
                        self.log(f"已更新教学部署列表中同名分支记录：[{name}]", self.LOG_COLORS["ok"])
                    else:
                        self.log(f"已自动在教学部署列表中新建记录：[{school_name}]", self.LOG_COLORS["ok"])
                        
                    self.fetch_branches()

        self.run_async(create, done, busy_msg="正在创建分支…")

    # ---------------- IP 替换 ----------------

    def _ip_inputs(self, need_new: bool = False):
        repo = self.require_project()
        if not repo:
            return None
        old_ip = self.old_ip_edit.currentText().strip()
        new_ip = self.new_ip_edit.text().strip()
        if not old_ip:
            QMessageBox.warning(self, "提示", "请填写旧IP")
            return None
        if need_new and not new_ip:
            QMessageBox.warning(self, "提示", "请填写新IP")
            return None
        return repo, old_ip, new_ip

    def select_logo_file(self):
        if not self._is_old_education_project():
            QMessageBox.warning(self, "不可用", "上传Logo仅适用于教育旧项目。")
            return
        logo_path, _ = QFileDialog.getOpenFileName(
            self, "选择学校 Logo 图片", "", "图片文件 (*.png *.jpg *.jpeg)"
        )
        if not logo_path:
            return
        self.logo_file_path = logo_path
        name = os.path.basename(logo_path)
        self.logo_file_label.setText(name if len(name) <= 16 else name[:13] + "...")
        self.logo_file_label.setToolTip(logo_path)
        self.preview_logo_btn.setEnabled(True)
        self.clear_logo_btn.setEnabled(True)
        self.log(f"已选择 Logo：{logo_path}", self.LOG_COLORS["hint"])

    def preview_logo_file(self):
        logo_path = getattr(self, "logo_file_path", "")
        if not logo_path:
            QMessageBox.information(self, "提示", "请先上传 Logo")
            return
        if not os.path.exists(logo_path):
            QMessageBox.warning(self, "Logo 不存在", f"已选择的 Logo 文件不存在：\n{logo_path}")
            self.clear_logo_file()
            return
        dlg = ImagePreviewDialog(logo_path, self)
        dlg.exec_()

    def clear_logo_file(self):
        self.logo_file_path = ""
        if hasattr(self, "logo_file_label"):
            self.logo_file_label.setText("未上传")
            self.logo_file_label.setToolTip("未上传 Logo 时不会替换 Logo")
        if hasattr(self, "preview_logo_btn"):
            self.preview_logo_btn.setEnabled(False)
        if hasattr(self, "clear_logo_btn"):
            self.clear_logo_btn.setEnabled(False)

    def preview_ip(self):
        params = self._ip_inputs()
        if not params:
            return
        repo, old_ip, _ = params
        branch = self.current_branch_text() or "(未知)"
        self.log(f"开始检索 [{old_ip}] … （项目：{self.selected_project()} | 分支：{branch}）", self.LOG_COLORS["cmd"])

        def done(hits):
            if not hits:
                self.log("未找到匹配内容")
                return
            for rel, lineno, content in hits[:500]:
                self.log(f"{rel}:{lineno}: {content}")
            if len(hits) > 500:
                self.log(f"…（共 {len(hits)} 处，仅显示前 500 条）", self.LOG_COLORS["hint"])
            self.log(f"共命中 {len(hits)} 处，涉及 {len({h[0] for h in hits})} 个文件", self.LOG_COLORS["ok"])

        self.run_async(ip_replace.preview_ip, done, repo, old_ip, busy_msg="正在检索…")

    def replace_ip(self):
        params = self._ip_inputs(need_new=True)
        if not params:
            return
        repo, old_ip, new_ip = params
        branch = self.current_branch_text() or "(未知)"
        project = self.selected_project()
        logo_payload = None
        logo_path = self.logo_file_path.strip()
        if logo_path:
            if not os.path.exists(logo_path):
                QMessageBox.warning(self, "Logo 不存在", f"已选择的 Logo 文件不存在：\n{logo_path}")
                return
            if not self._is_old_education_project(project):
                QMessageBox.warning(self, "不可用", "Logo 替换仅适用于教育旧项目。")
                return
            logo_payload, logo_err = self._prepare_logo_replacement(repo, logo_path)
            if logo_err:
                QMessageBox.warning(self, "Logo 准备失败", logo_err)
                return

        logo_line = ""
        if logo_payload:
            logo_line = (
                f"\n\n同时替换 Logo：\n"
                f"  {os.path.basename(logo_path)} → {logo_payload['rel_target']}\n"
                f"  Home.vue 引用 → {logo_payload['new_ref_path']}"
            )
        if QMessageBox.question(
            self, "确认替换",
            f"项目：{project}\n分支：{branch}\n\n将该项目中所有\n\n  {old_ip}  →  {new_ip}\n\n"
            f"建议先点「预览匹配」确认范围。{logo_line}\n\n确定执行吗？",
        ) != QMessageBox.Yes:
            return
        self.log(f"开始替换 [{old_ip}] → [{new_ip}] …", self.LOG_COLORS["cmd"])
        if logo_payload:
            self.log(f"已加入 Logo 替换：{logo_payload['rel_target']}", self.LOG_COLORS["cmd"])

        def replace_all():
            ip_results = ip_replace.replace_ip(repo, old_ip, new_ip)
            logo_result = self._apply_logo_replacement(logo_payload) if logo_payload else None
            return ip_results, logo_result

        def done(result):
            results, logo_result = result
            file_count = 0
            total = 0
            if not results:
                self.log("没有文件需要替换")
            else:
                for rel, count in results:
                    if isinstance(count, int):
                        total += count
                        self.log(f"{rel}: 替换 {count} 处")
                    else:
                        self.log(f"{rel}: {count}", self.LOG_COLORS["err"])
                file_count = len(results)
                self.log(f"IP 替换完成：{file_count} 个文件，共 {total} 处", self.LOG_COLORS["ok"])
                cfg_mod.add_ip_history(self.cfg, project, branch, old_ip, new_ip, file_count)
                cfg_mod.save_config(self.cfg)
                self._refresh_old_ip_options(project)

                # 检测是否是在新分支（以 eduLocal- 开头）下进行的 IP 替换，若是则自动同步更新教学部署列表中对应学校的 IP 地址
                if branch.startswith("eduLocal-"):
                    school_name = self._extract_school_name(branch)
                    target_item = self._find_teach_deployment(branch, school_name)
                    if target_item:
                        target_item["branch_name"] = branch
                        target_item["ip"] = new_ip
                        cfg_mod.save_config(self.cfg)
                        self.teach_data = list(self.cfg["teach_deployments"])
                        self._render_teach_table()
                        self.log(f"检测到在新分支下执行IP替换，已自动将教学部署列表中 [{school_name}] 的 IP 地址更新为：{new_ip}", self.LOG_COLORS["ok"])

            logo_msg = ""
            if logo_result:
                rel, home_rel, err = logo_result
                if err:
                    self.log(f"Logo 替换失败：{rel} - {err}", self.LOG_COLORS["err"])
                    logo_msg = f"\nLogo 替换失败：{err}"
                else:
                    self.log(f"Logo 已替换：{rel}", self.LOG_COLORS["ok"])
                    self.log(f"Home.vue 已同步更新：{home_rel}", self.LOG_COLORS["ok"])
                    logo_msg = f"\nLogo 已替换：{rel}"
                    self.clear_logo_file()

            QMessageBox.information(
                self,
                "完成",
                f"IP 替换：{file_count} 个文件、{total} 处。{logo_msg}\n可用 status/commit 查看并提交。"
            )

        self.run_async(replace_all, done, busy_msg="正在一键替换…")

    def _resolve_login_logo_target(self, repo: str):
        import re

        home_vue = os.path.join(repo, "src", "views", "Home.vue")
        if not os.path.exists(home_vue):
            return None, "未找到 src/views/Home.vue"

        try:
            with open(home_vue, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            return None, f"读取 src/views/Home.vue 失败：{e}"

        match = re.search(
            r"(?P<path>(?:@/|/|\.{1,2}/)?(?:src/)?images/loginLogo\.(?P<ext>png|jpe?g))",
            content,
            re.IGNORECASE,
        )
        if match:
            ref_path = match.group("path").replace("\\", "/")
            if ref_path.startswith("@/"):
                target = os.path.join(repo, "src", ref_path[2:])
            elif ref_path.startswith("/"):
                target = os.path.join(repo, ref_path.lstrip("/"))
            elif ref_path.startswith("./") or ref_path.startswith("../"):
                target = os.path.normpath(os.path.join(os.path.dirname(home_vue), ref_path))
            elif ref_path.startswith("src/"):
                target = os.path.join(repo, ref_path)
            else:
                target = os.path.join(repo, "src", ref_path)
            return {
                "home_vue": home_vue,
                "content": content,
                "target": target,
                "ref_path": match.group("path"),
                "ref_span": match.span("path"),
            }, ""

        return None, "Home.vue 中未找到 loginLogo.png / loginLogo.jpg / loginLogo.jpeg 引用"

    def _prepare_logo_replacement(self, repo: str, logo_path: str):
        source_ext = os.path.splitext(logo_path)[1].lower()
        if source_ext not in (".png", ".jpg", ".jpeg"):
            return None, "Logo 仅支持 .png / .jpg / .jpeg"

        logo_info, err = self._resolve_login_logo_target(repo)
        if err:
            return None, err

        old_target = logo_info["target"]
        target_dir = os.path.dirname(old_target)
        target = os.path.join(target_dir, f"loginLogo{source_ext}")
        ref_path = logo_info["ref_path"]
        ref_root, _ = os.path.splitext(ref_path)
        new_ref_path = f"{ref_root}{source_ext}"
        start, end = logo_info["ref_span"]
        new_home_content = logo_info["content"][:start] + new_ref_path + logo_info["content"][end:]

        return {
            "logo_path": logo_path,
            "target": target,
            "target_dir": target_dir,
            "home_vue": logo_info["home_vue"],
            "new_home_content": new_home_content,
            "new_ref_path": new_ref_path,
            "rel_target": os.path.relpath(target, repo),
            "rel_home": os.path.relpath(logo_info["home_vue"], repo),
        }, ""

    def _apply_logo_replacement(self, payload: dict):
        import shutil
        try:
            os.makedirs(payload["target_dir"], exist_ok=True)
            if os.path.abspath(payload["logo_path"]) != os.path.abspath(payload["target"]):
                shutil.copyfile(payload["logo_path"], payload["target"])
            with open(payload["home_vue"], "w", encoding="utf-8") as f:
                f.write(payload["new_home_content"])
            return payload["rel_target"], payload["rel_home"], ""
        except Exception as e:
            return payload.get("rel_target", ""), payload.get("rel_home", ""), str(e)

    def replace_school_logo(self):
        repo = self.require_project()
        if not repo:
            return
        if not self._is_old_education_project():
            QMessageBox.warning(self, "不可用", "替换Logo仅适用于教育旧项目。")
            return

        logo_path, _ = QFileDialog.getOpenFileName(
            self, "选择学校 Logo 图片", "", "图片文件 (*.png *.jpg *.jpeg)"
        )
        if not logo_path:
            return

        payload, err = self._prepare_logo_replacement(repo, logo_path)
        if err:
            QMessageBox.warning(self, "Logo 准备失败", err)
            return

        if QMessageBox.question(
            self, "确认替换学校 Logo",
            f"将使用：\n{logo_path}\n\n"
            f"复制为：\n{payload['rel_target']}\n\n"
            f"并同步修改 Home.vue 引用为：\n{payload['new_ref_path']}\n\n确定执行吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return

        def replace():
            return self._apply_logo_replacement(payload)

        def done(result):
            rel, home_rel, err = result
            if err:
                self.log(f"Logo 替换失败：{rel} - {err}", self.LOG_COLORS["err"])
                QMessageBox.warning(self, "替换失败", f"{rel}\n\n{err}")
            else:
                self.log(f"Logo 已替换：{rel}", self.LOG_COLORS["ok"])
                self.log(f"Home.vue 已同步更新：{home_rel}", self.LOG_COLORS["ok"])
                QMessageBox.information(self, "完成", f"已替换学校 Logo：\n{rel}\n\n已同步更新：\n{home_rel}")

        self.run_async(replace, done, busy_msg="正在替换学校 Logo…")

    def show_ip_history(self):
        """弹窗以表格展示 IP 替换历史。"""
        history = self.cfg.get("ip_history", [])
        dlg = QDialog(self)
        dlg.setWindowTitle("IP 替换历史")
        dlg.resize(900, 460)
        v = QVBoxLayout(dlg)

        cur_proj = self.selected_project()
        filter_row = QHBoxLayout()
        only_cur = QPushButton(f"仅看当前项目（{cur_proj or '无'}）")
        only_cur.setCheckable(True)
        filter_row.addWidget(only_cur)
        filter_row.addStretch(1)
        v.addLayout(filter_row)

        from PyQt5.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        table = QTableWidget()
        headers = ["时间", "项目", "分支", "旧IP", "新IP", "文件数"]
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        table.verticalHeader().setDefaultSectionSize(36)
        v.addWidget(table, 1)

        def fill():
            rows = [r for r in history
                    if not only_cur.isChecked() or r.get("project") == cur_proj]
            table.setRowCount(len(rows))
            for i, rec in enumerate(rows):
                vals = [rec.get("time", ""), rec.get("project", ""),
                        rec.get("branch", ""), rec.get("old_ip", ""),
                        rec.get("new_ip", ""), str(rec.get("files", ""))]
                for j, val in enumerate(vals):
                    table.setItem(i, j, QTableWidgetItem(val))
            table.resizeColumnsToContents()
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

        only_cur.toggled.connect(fill)
        fill()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        use_btn = QPushButton("用选中行填入输入框")
        close_btn = QPushButton("关闭")
        btn_row.addWidget(use_btn)
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

        def use_selected():
            row = table.currentRow()
            if row < 0:
                return
            self.old_ip_edit.setCurrentText(table.item(row, 3).text())
            self.new_ip_edit.setText(table.item(row, 4).text())
            dlg.accept()

        use_btn.clicked.connect(use_selected)
        close_btn.clicked.connect(dlg.reject)
        dlg.exec_()

    def _detect_project_vue_and_node(self, repo_path: str):
        """
        读取 package.json, 检测 vue 版本:
        - Vue 2: 返回 (2, "8.12.0")
        - Vue 3: 返回 (3, "20.18.0")
        - 其他/未检测到: 返回 (None, None)
        """
        import json
        if not repo_path or not os.path.exists(repo_path):
            return None, None
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
        except Exception as e:
            self.log(f"读取 package.json 出错: {e}", self.LOG_COLORS["err"])
        return None, None

    def build_project(self):
        """打包项目功能，支持勾选多个项目进行批量并发打包"""
        checked_paths = []
        for item in self.project_list.all_project_items():
            if item.checkState(0) == Qt.Checked:
                name = item.data(0, Qt.UserRole)
                proj_path = os.path.join(self.work_dir(), name)
                checked_paths.append(proj_path)
                
        if not checked_paths:
            QMessageBox.warning(self, "提示", "请先在左侧项目列表中勾选需要打包的项目才可以启动打包。")
            return
            
        # 如果已经存在运行中的打包窗口
        has_dialog = hasattr(self, "batch_build_dialog") and self.batch_build_dialog is not None
        if has_dialog:
            try:
                # 检查是否有任何任务在运行或排队中
                any_building = any(t["status"] in ("building", "pending") for t in self.batch_build_dialog.tasks.values())
                if any_building:
                    # 如果有任务在运行，则追加新勾选的任务，并唤醒显示
                    self.batch_build_dialog.append_projects(checked_paths)
                    self.batch_build_dialog.show()
                    self.batch_build_dialog.raise_()
                    self.batch_build_dialog.activateWindow()
                    return
                else:
                    # 如果以前的都完成了/终止了，直接安全关闭旧窗口，以便开启新的批量打包
                    self.batch_build_dialog.close_and_terminate()
                    self.batch_build_dialog = None
            except Exception:
                self.batch_build_dialog = None
                
        from .batch_build import BatchBuildDialog
        # 创建非阻塞窗口并记录引用，避免生命周期提前结束或重复创建
        self.batch_build_dialog = BatchBuildDialog(checked_paths, self)
        self.batch_build_dialog.show()

    def _on_build_output(self):
        if hasattr(self, "build_process"):
            data = self.build_process.readAllStandardOutput().data()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("gbk", errors="ignore")
            self.log_raw(text)

    def _on_build_finished(self, exit_code, exit_status):
        self.set_busy(False)
        self.build_btn.setText("📦 打包")
        self.setWindowTitle("Git 多项目分支管理工具 - builderTool")
        
        repo_path = getattr(self, "_build_project_path", "")
        if exit_code == 0:
            self.log("打包成功！", self.LOG_COLORS["ok"])
            
            # 定位打包生成路径
            target_dir = repo_path
            if repo_path:
                dist_path = os.path.join(repo_path, "dist")
                disk_path = os.path.join(repo_path, "disk")
                dist_disk_path = os.path.join(dist_path, "disk")
                if os.path.exists(dist_disk_path):
                    target_dir = dist_disk_path
                elif os.path.exists(dist_path):
                    target_dir = dist_path
                elif os.path.exists(disk_path):
                    target_dir = disk_path
            
            # 在日志中输出点击一键直达超链接
            path_url = QUrl.fromLocalFile(target_dir).toString()
            link_html = f'打包完成！输出路径: <a href="{path_url}" style="color: {self.LOG_COLORS["ok"]}; font-weight: bold;">{target_dir}</a> (点击一键直达)'
            self.log_html(link_html)
            
            # 闪烁任务栏与置顶弹窗提示
            from PyQt5.QtWidgets import QApplication
            QApplication.alert(self)
            msg = QMessageBox(self)
            msg.setWindowTitle("打包成功")
            msg.setText(f"项目打包成功！\n\n路径: {target_dir}")
            msg.setIcon(QMessageBox.Information)
            msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
            msg.exec_()
        else:
            self.log(f"打包失败，退出码: {exit_code}", self.LOG_COLORS["err"])
            from PyQt5.QtWidgets import QApplication
            QApplication.alert(self)
            msg = QMessageBox(self)
            msg.setWindowTitle("打包失败")
            msg.setText(f"打包执行失败，退出码: {exit_code}\n详情见操作日志。")
            msg.setIcon(QMessageBox.Critical)
            msg.setWindowFlags(msg.windowFlags() | Qt.WindowStaysOnTopHint)
            msg.exec_()

    def _on_log_link_clicked(self, url: QUrl):
        """处理日志内超链接点击事件，直接在资源管理器中打开对应目录，避免 QTextBrowser 内部导航导致内容清空"""
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
            import os
            try:
                os.startfile(local_path)
            except Exception as e:
                self.log(f"无法打开路径: {e}", self.LOG_COLORS["err"])
        else:
            from PyQt5.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)

    def closeEvent(self, event):
        """主窗口关闭时，强制释放/关闭后台的打包弹窗并终止其子进程"""
        # 保存一些主要配置
        self.cfg["last_old_ip"] = self.old_ip_edit.currentText().strip()
        self.cfg["last_new_ip"] = self.new_ip_edit.text().strip()
        cfg_mod.save_config(self.cfg)
        
        # 终止单体项目的打包进程（如果是旧式单体打包）
        if hasattr(self, "build_process") and self.build_process.state() == QProcess.Running:
            self.build_process.terminate()
            self.build_process.waitForFinished(1000)
            
        # 关闭后台批量打包弹窗并强制终止其所有任务
        if hasattr(self, "batch_build_dialog") and self.batch_build_dialog is not None:
            try:
                self.batch_build_dialog.close_and_terminate()
            except Exception:
                pass
                
        super().closeEvent(event)
