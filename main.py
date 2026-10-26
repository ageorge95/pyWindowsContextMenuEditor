# VERSION=1
# https://github.com/ageorge95/ag95_repo_base

import ctypes
import os
import sys

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (QAbstractItemView,
                               QApplication,
                               QCheckBox,
                               QComboBox,
                               QDialog,
                               QDialogButtonBox,
                               QFileDialog,
                               QFormLayout,
                               QHBoxLayout,
                               QHeaderView,
                               QLabel,
                               QLineEdit,
                               QMainWindow,
                               QMessageBox,
                               QPushButton,
                               QTabWidget,
                               QTreeWidget,
                               QTreeWidgetItem,
                               QVBoxLayout,
                               QWidget)

import context_menu_registry as registry

APP_NAME = 'Windows Context Menu Editor'
APP_ID = 'AG95.pyWindowsContextMenuEditor'  # Windows taskbar identity; rename for a new project
ICON_FILE = 'icon.ico'
VERSION_FILE = 'version.txt'
BACKUP_FOLDER = 'backups'


def get_running_path(relative_path):
    """Return a bundled file's path in both source and PyInstaller builds."""
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def get_data_path(relative_path):
    """Return a writable path next to the script / executable (not inside the bundle)."""
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def set_windows_app_id():
    """Give the app its own Windows taskbar identity.

    When run as python.exe, Windows groups the window under Python's taskbar
    entry and uses Python's icon. An explicit AppUserModelID makes the taskbar
    use the window icon instead, matching the compiled executable.
    """
    if sys.platform != 'win32':
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass


def read_version():
    """Return the version from version.txt, or 'dev' if it is missing."""
    try:
        with open(get_running_path(VERSION_FILE), 'r', encoding='utf-8') as file:
            return file.read().strip()
    except OSError:
        return 'dev'


def show_error(parent, error):
    QMessageBox.critical(parent, APP_NAME, str(error))


class EntryDialog(QDialog):
    """Add / edit dialog for a HKCU shell verb or submenu."""

    def __init__(self, parent, placeholder, entry=None, is_submenu=False):
        super().__init__(parent)
        self.entry = entry
        self.is_submenu = entry.kind == registry.KIND_SUBMENU if entry else is_submenu
        self.placeholder = placeholder
        kind = 'submenu' if self.is_submenu else 'command'
        self.setWindowTitle(f'Edit {kind}' if entry else f'Add {kind}')
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText('Text shown in the menu, e.g. Open with My App')
        form.addRow('Menu text:', self.label_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText('Registry key name (auto-filled from the menu text)')
        form.addRow('Key name:', self.name_edit)

        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText(f'"C:\\Path\\app.exe" "{placeholder}"')
        command_row = QHBoxLayout()
        command_row.addWidget(self.command_edit)
        browse_command = QPushButton('Browse...')
        browse_command.clicked.connect(self.on_browse_command)
        command_row.addWidget(browse_command)
        self.command_label = QLabel('Command:')
        form.addRow(self.command_label, command_row)
        self.command_hint = QLabel(f'Use {placeholder} for the clicked item. '
                                   'Environment variables like %SystemRoot% are allowed.')
        self.command_hint.setStyleSheet('color: gray;')
        form.addRow('', self.command_hint)

        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText('Optional, e.g. C:\\Path\\app.exe,0 or C:\\icons\\my.ico')
        icon_row = QHBoxLayout()
        icon_row.addWidget(self.icon_edit)
        browse_icon = QPushButton('Browse...')
        browse_icon.clicked.connect(self.on_browse_icon)
        icon_row.addWidget(browse_icon)
        form.addRow('Icon:', icon_row)

        self.position_combo = QComboBox()
        self.position_combo.addItems(['Default', 'Top', 'Bottom'])
        form.addRow('Position:', self.position_combo)

        self.extended_check = QCheckBox('Only show when Shift is held (Extended)')
        form.addRow('', self.extended_check)
        self.disabled_check = QCheckBox('Disabled (hidden from the menu)')
        form.addRow('', self.disabled_check)

        if self.is_submenu:
            for widget in (self.command_label, self.command_edit, browse_command, self.command_hint):
                widget.setVisible(False)
            note = QLabel('After creating the submenu, select it and use "Add command" to add its items.')
            note.setStyleSheet('color: gray;')
            layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.name_was_edited = bool(entry)
        self.label_edit.textEdited.connect(self.on_label_edited)
        self.name_edit.textEdited.connect(self.on_name_edited)

        if entry:
            self.label_edit.setText(entry.raw_label or entry.label)
            self.name_edit.setText(entry.name)
            self.command_edit.setText(entry.command)
            self.icon_edit.setText(entry.icon)
            position = entry.position.capitalize()
            self.position_combo.setCurrentText(position if position in registry.POSITIONS[1:] else 'Default')
            self.extended_check.setChecked(entry.extended)
            self.disabled_check.setChecked(entry.disabled)

    def on_label_edited(self, text):
        if not self.name_was_edited:
            name = ''.join(char for char in text.replace('&', '') if char not in '\\/')
            self.name_edit.setText(' '.join(name.split()))

    def on_name_edited(self, _):
        self.name_was_edited = True

    def on_browse_command(self):
        file_path, _ = QFileDialog.getOpenFileName(self, 'Select program', '',
                                                   'Programs (*.exe *.bat *.cmd *.com);;All files (*)')
        if not file_path:
            return
        file_path = os.path.normpath(file_path)
        self.command_edit.setText(f'"{file_path}" "{self.placeholder}"')
        if not self.icon_edit.text() and file_path.lower().endswith('.exe'):
            self.icon_edit.setText(f'{file_path},0')

    def on_browse_icon(self):
        file_path, _ = QFileDialog.getOpenFileName(self, 'Select icon', '',
                                                   'Icons (*.ico *.exe *.dll);;All files (*)')
        if file_path:
            file_path = os.path.normpath(file_path)
            self.icon_edit.setText(file_path if file_path.lower().endswith('.ico') else f'{file_path},0')

    def on_accept(self):
        if not self.label_edit.text().strip():
            show_error(self, 'The menu text must not be empty.')
            return
        if not self.name_edit.text().strip():
            show_error(self, 'The key name must not be empty.')
            return
        if not self.is_submenu and not self.command_edit.text().strip():
            show_error(self, 'The command must not be empty.')
            return
        self.accept()

    def values(self):
        position = self.position_combo.currentText()
        return {'name': self.name_edit.text().strip(),
                'label': self.label_edit.text().strip(),
                'command': self.command_edit.text().strip(),
                'icon': self.icon_edit.text().strip(),
                'position': '' if position == 'Default' else position,
                'extended': self.extended_check.isChecked(),
                'disabled': self.disabled_check.isChecked(),
                'is_submenu': self.is_submenu}


class ClassicMenuTab(QWidget):
    """View and edit the classic (registry based) context menu entries."""

    COLUMNS = ('Menu text', 'Location', 'Type', 'Status', 'Source', 'Command / handler', 'Key name')

    def __init__(self, status_callback):
        super().__init__()
        self.set_status = status_callback
        self.backup_dir = get_data_path(BACKUP_FOLDER)

        layout = QVBoxLayout(self)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel('Right-click on:'))
        self.target_combo = QComboBox()
        self.target_combo.addItems(list(registry.TARGETS))
        self.target_combo.currentTextChanged.connect(self.on_target_changed)
        target_row.addWidget(self.target_combo)
        self.extension_edit = QLineEdit()
        self.extension_edit.setPlaceholderText('.txt')
        self.extension_edit.setFixedWidth(120)
        self.extension_edit.returnPressed.connect(self.refresh)
        self.extension_edit.setVisible(False)
        target_row.addWidget(self.extension_edit)
        refresh_button = QPushButton('Refresh')
        refresh_button.clicked.connect(self.refresh)
        target_row.addWidget(refresh_button)
        target_row.addStretch()
        target_row.addWidget(QLabel('Search:'))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Filter by text, command or key name')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(280)
        self.search_edit.textChanged.connect(self.apply_filter)
        target_row.addWidget(self.search_edit)
        layout.addLayout(target_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        self.tree.itemDoubleClicked.connect(lambda *_: self.on_edit())
        layout.addWidget(self.tree)

        buttons_row = QHBoxLayout()
        self.add_button = QPushButton('Add command')
        self.add_button.clicked.connect(lambda: self.on_add(is_submenu=False))
        self.add_submenu_button = QPushButton('Add submenu')
        self.add_submenu_button.clicked.connect(lambda: self.on_add(is_submenu=True))
        self.edit_button = QPushButton('Edit')
        self.edit_button.clicked.connect(self.on_edit)
        self.toggle_button = QPushButton('Disable')
        self.toggle_button.clicked.connect(self.on_toggle)
        self.delete_button = QPushButton('Delete')
        self.delete_button.clicked.connect(self.on_delete)
        restore_button = QPushButton('Restore backup...')
        restore_button.clicked.connect(self.on_restore)
        backups_button = QPushButton('Open backups folder')
        backups_button.clicked.connect(self.on_open_backups)
        for button in (self.add_button, self.add_submenu_button, self.edit_button,
                       self.toggle_button, self.delete_button):
            buttons_row.addWidget(button)
        buttons_row.addStretch()
        buttons_row.addWidget(restore_button)
        buttons_row.addWidget(backups_button)
        layout.addLayout(buttons_row)

        note = QLabel('Changes are written to HKEY_CURRENT_USER only (no admin rights needed). '
                      'System entries cannot be deleted, but can be disabled for the current user. '
                      'On Windows 11 these entries appear under "Show more options".')
        note.setWordWrap(True)
        note.setStyleSheet('color: gray;')
        layout.addWidget(note)

        self.refresh()

    # ------------------------------------------------------------ helpers

    def current_target(self):
        return self.target_combo.currentText()

    def selected_entry(self):
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def add_class_path(self):
        """Return the class path where new entries are created (inside a selected user submenu if any)."""
        entry = self.selected_entry()
        if entry and entry.kind == registry.KIND_SUBMENU and entry.user_owned:
            return rf'{entry.class_path}\shell\{entry.name}'
        return registry.get_class_paths(self.current_target(), self.extension_edit.text())[0]

    def build_item(self, entry, parent):
        item = QTreeWidgetItem(parent, [entry.label,
                                        entry.location,
                                        entry.kind,
                                        entry.status,
                                        entry.source,
                                        entry.command,
                                        entry.name])
        item.setData(0, Qt.UserRole, entry)
        item.setToolTip(5, entry.command)
        item.setToolTip(6, rf'{entry.class_path}\{"shellex" if entry.kind == registry.KIND_SHELLEX else "shell"}'
                           rf'\{entry.name}')
        if entry.disabled or entry.status != 'Enabled':
            for column in range(len(self.COLUMNS)):
                item.setForeground(column, Qt.gray)
        if entry.kind == registry.KIND_SUBMENU:
            for child in registry.list_entries([rf'{entry.class_path}\shell\{entry.name}']):
                self.build_item(child, item)
        return item

    # ------------------------------------------------------------ actions

    def on_target_changed(self, target):
        is_extension = target == registry.FILE_EXTENSION_TARGET
        self.extension_edit.setVisible(is_extension)
        if is_extension:
            self.extension_edit.setFocus()
            if not self.extension_edit.text():
                self.tree.clear()
                self.update_buttons()
                return
        self.refresh()

    def refresh(self, select_name=None):
        self.tree.clear()
        try:
            class_paths = registry.get_class_paths(self.current_target(), self.extension_edit.text())
            entries = registry.list_entries(class_paths)
        except (registry.ContextMenuError, OSError) as error:
            self.update_buttons()
            show_error(self, error)
            return
        self.tree.setSortingEnabled(False)
        for entry in entries:
            item = self.build_item(entry, self.tree)
            if select_name and entry.name.lower() == select_name.lower():
                self.tree.setCurrentItem(item)
        self.tree.setSortingEnabled(True)
        self.tree.expandAll()
        for column in range(len(self.COLUMNS) - 1):
            self.tree.resizeColumnToContents(column)
            self.tree.setColumnWidth(column, min(self.tree.columnWidth(column), 380))
        self.set_status(f'{len(entries)} entries for "{" + ".join(class_paths)}"')
        self.apply_filter()
        self.update_buttons()

    def apply_filter(self):
        """Hide rows not matching the search text (a parent stays visible if a child matches)."""
        text = self.search_edit.text().strip().lower()

        def filter_item(item):
            child_visible = False
            for index in range(item.childCount()):
                child_visible = filter_item(item.child(index)) or child_visible
            matches = not text or any(text in item.text(column).lower() for column in range(len(self.COLUMNS)))
            item.setHidden(not (matches or child_visible))
            return matches or child_visible

        for index in range(self.tree.topLevelItemCount()):
            filter_item(self.tree.topLevelItem(index))

    def update_buttons(self):
        entry = self.selected_entry()
        in_submenu = bool(entry and entry.kind == registry.KIND_SUBMENU and entry.user_owned)
        target = self.current_target()
        has_target = in_submenu or (target != registry.ALL_LOCATIONS_TARGET
                                    and (target != registry.FILE_EXTENSION_TARGET or bool(self.extension_edit.text())))
        self.add_button.setEnabled(has_target)
        self.add_submenu_button.setEnabled(has_target)
        tooltip = '' if has_target else 'Select a specific "Right-click on" location to add entries'
        self.add_button.setToolTip(tooltip)
        self.add_submenu_button.setToolTip(tooltip)
        self.add_button.setText('Add command to submenu' if in_submenu else 'Add command')
        self.add_submenu_button.setText('Add nested submenu' if in_submenu else 'Add submenu')
        self.edit_button.setEnabled(bool(entry and entry.editable))
        self.delete_button.setEnabled(bool(entry and entry.user_owned))
        self.toggle_button.setEnabled(entry is not None)
        self.toggle_button.setText('Enable' if entry and entry.disabled else 'Disable')

    def on_add(self, is_submenu):
        try:
            class_path = self.add_class_path()
        except registry.ContextMenuError as error:
            show_error(self, error)
            return
        entry = self.selected_entry()
        if entry and entry.kind == registry.KIND_SUBMENU and entry.user_owned:
            placeholder = registry.command_placeholder(entry.location)
        else:
            placeholder = registry.command_placeholder(self.current_target())
        dialog = EntryDialog(self, placeholder, is_submenu=is_submenu)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        try:
            registry.save_verb(class_path, None, **values)
        except (registry.ContextMenuError, OSError) as error:
            show_error(self, error)
            return
        self.refresh(select_name=values['name'])
        self.set_status(f'Added "{values["label"]}"')

    def on_edit(self):
        entry = self.selected_entry()
        if not entry:
            return
        if not entry.editable:
            QMessageBox.information(self, APP_NAME, 'Only user (HKCU) commands and submenus can be edited. '
                                                    'System entries and shell extensions can be disabled instead.')
            return
        dialog = EntryDialog(self, registry.command_placeholder(entry.location), entry=entry)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        try:
            registry.save_verb(entry.class_path, entry.name, **values)
        except (registry.ContextMenuError, OSError) as error:
            show_error(self, error)
            return
        self.refresh(select_name=values['name'])
        self.set_status(f'Saved "{values["label"]}"')

    def on_toggle(self):
        entry = self.selected_entry()
        if not entry:
            return
        try:
            registry.set_entry_disabled(entry, not entry.disabled)
        except (registry.ContextMenuError, OSError) as error:
            show_error(self, error)
            return
        self.refresh(select_name=entry.name)
        self.set_status(f'{"Enabled" if entry.disabled else "Disabled"} "{entry.label}"')

    def on_delete(self):
        entry = self.selected_entry()
        if not entry:
            return
        answer = QMessageBox.question(self, APP_NAME,
                                      f'Delete "{entry.label}"?\n\n{entry.registry_path()}\n\n'
                                      f'A .reg backup will be saved in:\n{self.backup_dir}')
        if answer != QMessageBox.Yes:
            return
        try:
            backup_file = registry.delete_entry(entry, self.backup_dir)
        except (registry.ContextMenuError, OSError) as error:
            show_error(self, error)
            return
        self.refresh()
        self.set_status(f'Deleted "{entry.label}", backup: {backup_file}')

    def on_restore(self):
        file_path, _ = QFileDialog.getOpenFileName(self, 'Restore backup', self.backup_dir,
                                                   'Registry files (*.reg)')
        if not file_path:
            return
        try:
            registry.restore_backup(file_path)
        except (registry.ContextMenuError, OSError, UnicodeError) as error:
            show_error(self, error)
            return
        self.refresh()
        self.set_status(f'Restored {os.path.basename(file_path)}')

    def on_open_backups(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        os.startfile(self.backup_dir)


class PackagedScanWorker(QObject):
    finished = Signal(list)
    failed = Signal(str)

    def run(self):
        try:
            self.finished.emit(registry.scan_packaged_entries())
        except (registry.ContextMenuError, OSError) as error:
            self.failed.emit(str(error))


class ModernMenuTab(QWidget):
    """Windows 11 modern context menu: classic menu toggle and packaged app entries."""

    COLUMNS = ('App', 'Verb', 'Shown for', 'Status', 'CLSID', 'Package')

    def __init__(self, status_callback):
        super().__init__()
        self.set_status = status_callback
        self.thread = None
        self.worker = None

        layout = QVBoxLayout(self)

        self.classic_check = QCheckBox('Always show the full (classic) context menu - '
                                       'skip "Show more options" (Windows 11, current user)')
        self.classic_check.setChecked(registry.is_classic_menu_forced())
        self.classic_check.toggled.connect(self.on_classic_toggled)
        self.classic_check.setEnabled(registry.is_windows_11())
        layout.addWidget(self.classic_check)

        title = QLabel('Entries added to the Windows 11 menu by installed (packaged) apps:')
        layout.addWidget(title)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(self.COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.tree)

        buttons_row = QHBoxLayout()
        self.scan_button = QPushButton('Scan apps')
        self.scan_button.clicked.connect(self.scan)
        self.toggle_button = QPushButton('Hide')
        self.toggle_button.clicked.connect(self.on_toggle)
        buttons_row.addWidget(self.scan_button)
        buttons_row.addWidget(self.toggle_button)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        note = QLabel('Packaged app entries cannot be created or deleted (they come from the app\'s manifest), '
                      'but they can be hidden for the current user via the shell extension block list. '
                      'Restart Explorer for changes in this tab to take effect.')
        note.setWordWrap(True)
        note.setStyleSheet('color: gray;')
        layout.addWidget(note)

        if not registry.is_windows_11():
            self.tree.setEnabled(False)
            self.scan_button.setEnabled(False)
            note.setText('The modern context menu only exists on Windows 11.')
        self.update_buttons()

    def selected_entry(self):
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def update_buttons(self):
        entry = self.selected_entry()
        self.toggle_button.setEnabled(bool(entry and entry.clsid and (entry.blocked_by_user or not entry.blocked)))
        self.toggle_button.setText('Show' if entry and entry.blocked else 'Hide')

    def scan(self, select_clsid=None):
        if self.thread is not None:
            return
        self.scan_button.setEnabled(False)
        self.set_status('Scanning installed apps...')
        self.select_clsid = select_clsid
        self.thread = QThread(self)
        self.worker = PackagedScanWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.failed.connect(self.on_scan_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.on_thread_finished)
        self.thread.start()

    def on_thread_finished(self):
        self.thread.deleteLater()
        self.worker.deleteLater()
        self.thread = None
        self.worker = None
        self.scan_button.setEnabled(True)

    def on_scan_finished(self, entries):
        self.tree.clear()
        self.tree.setSortingEnabled(False)
        for entry in entries:
            item = QTreeWidgetItem(self.tree, [entry.display_name,
                                               entry.verb_id,
                                               ', '.join(entry.item_types),
                                               entry.status,
                                               entry.clsid,
                                               entry.package_full_name])
            item.setData(0, Qt.UserRole, entry)
            item.setToolTip(2, ', '.join(entry.item_types))
            if entry.blocked:
                for column in range(len(self.COLUMNS)):
                    item.setForeground(column, Qt.gray)
            if self.select_clsid and entry.clsid == self.select_clsid:
                self.tree.setCurrentItem(item)
        self.tree.setSortingEnabled(True)
        for column in range(len(self.COLUMNS)):
            self.tree.resizeColumnToContents(column)
            self.tree.setColumnWidth(column, min(self.tree.columnWidth(column), 380))
        self.set_status(f'{len(entries)} packaged app context menu entries found')
        self.update_buttons()

    def on_scan_failed(self, message):
        self.set_status('Scan failed')
        show_error(self, message)

    def on_toggle(self):
        entry = self.selected_entry()
        if not entry:
            return
        try:
            registry.set_clsid_blocked(entry.clsid, not entry.blocked)
        except (registry.ContextMenuError, OSError) as error:
            show_error(self, error)
            return
        self.scan(select_clsid=entry.clsid)

    def on_classic_toggled(self, checked):
        try:
            registry.set_classic_menu_forced(checked)
        except OSError as error:
            show_error(self, error)
            return
        self.set_status('Classic menu ' + ('enabled' if checked else 'disabled') +
                        ' - restart Explorer to apply')


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'{APP_NAME} v{read_version()}')
        self.resize(1100, 650)
        self.setWindowIcon(QIcon(get_running_path(ICON_FILE)))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(10, 10, 10, 10)

        self.tabs = QTabWidget()
        self.classic_tab = ClassicMenuTab(self.set_status)
        self.modern_tab = ModernMenuTab(self.set_status)
        self.tabs.addTab(self.classic_tab, 'Classic context menu')
        self.tabs.addTab(self.modern_tab, 'Windows 11 menu')
        self.tabs.currentChanged.connect(self.on_tab_changed)
        layout.addWidget(self.tabs)

        restart_button = QPushButton('Restart Explorer')
        restart_button.setToolTip('Restart explorer.exe so all context menu changes are applied')
        restart_button.clicked.connect(self.on_restart_explorer)
        self.statusBar().addPermanentWidget(restart_button)

    def set_status(self, message):
        self.statusBar().showMessage(message)

    def on_tab_changed(self, index):
        if self.tabs.widget(index) is self.modern_tab and registry.is_windows_11() \
                and self.modern_tab.tree.topLevelItemCount() == 0:
            self.modern_tab.scan()

    def on_restart_explorer(self):
        answer = QMessageBox.question(self, APP_NAME, 'Restart Windows Explorer now?\n'
                                                      'Open Explorer windows will be closed.')
        if answer == QMessageBox.Yes:
            registry.restart_explorer()
            self.set_status('Explorer restarted')


def main():
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(get_running_path(ICON_FILE)))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
