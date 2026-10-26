"""Registry backend for viewing and editing the Windows Explorer context menu.

All write operations target HKEY_CURRENT_USER only, so administrator rights
are never needed. System-wide (HKLM) entries are read so they can be shown,
and they can be hidden for the current user through HKCU overrides:

- classic verbs:      a HKCU key with the same name and a LegacyDisable value
- shell extensions:   the HKCU 'Shell Extensions\\Blocked' list
- Win11 packaged app: the same HKCU 'Shell Extensions\\Blocked' list
"""

import ctypes
import json
import os
import re
import subprocess
import sys
import winreg
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import datetime

CLASSES_PATH = r'Software\Classes'
BLOCKED_PATH = r'Software\Microsoft\Windows\CurrentVersion\Shell Extensions\Blocked'
CLASSIC_MENU_CLSID = '{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}'
CLASSIC_MENU_PATH = rf'{CLASSES_PATH}\CLSID\{CLASSIC_MENU_CLSID}'

ALL_LOCATIONS_TARGET = 'All locations'
FILE_EXTENSION_TARGET = 'Specific file extension'
TARGETS = {ALL_LOCATIONS_TARGET: None,
           'All files': '*',
           'Files and folders': 'AllFilesystemObjects',
           'Folders': 'Directory',
           'All folders (incl. special)': 'Folder',
           'Folder background': r'Directory\Background',
           'Library background': r'LibraryFolder\background',
           'Desktop background': 'DesktopBackground',
           'Drives': 'Drive',
           FILE_EXTENSION_TARGET: None}
BACKGROUND_TARGETS = ('Folder background', 'Library background', 'Desktop background')
LOCATION_NAMES = {class_path: target for target, class_path in TARGETS.items() if class_path}

POSITIONS = ('', 'Top', 'Bottom')

KIND_VERB = 'Command'
KIND_SUBMENU = 'Submenu'
KIND_SHELLEX = 'Shell extension'

HIVE_NAMES = {winreg.HKEY_CURRENT_USER: 'HKCU',
              winreg.HKEY_LOCAL_MACHINE: 'HKLM'}

GUID_PATTERN = re.compile(r'^\{?[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}?$')

SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000
CREATE_NO_WINDOW = 0x08000000


class ContextMenuError(Exception):
    """Raised when a context menu operation cannot be performed."""


@dataclass
class MenuEntry:
    kind: str
    class_path: str
    name: str
    label: str = ''
    command: str = ''
    icon: str = ''
    extended: bool = False
    position: str = ''
    disabled: bool = False
    status: str = 'Enabled'
    user_owned: bool = False
    in_system: bool = False
    clsid: str = ''
    blocked_by_user: bool = False
    raw_label: str = ''
    has_default_label: bool = False

    @property
    def source(self):
        if self.user_owned:
            return 'User (HKCU)'
        return 'System (HKLM)'

    @property
    def location(self):
        """Human readable name of where this entry appears (top level class path)."""
        if self.class_path.startswith('SystemFileAssociations\\'):
            return self.class_path.split('\\', 1)[1].split('\\', 1)[0]
        top_level = self.class_path.split(r'\shell' + '\\', 1)[0]
        return LOCATION_NAMES.get(top_level, top_level)

    @property
    def editable(self):
        return self.user_owned and self.kind in (KIND_VERB, KIND_SUBMENU)

    def registry_path(self):
        """Return this entry's HKCU registry path (relative to the hive)."""
        if self.kind == KIND_SHELLEX:
            return rf'{CLASSES_PATH}\{self.class_path}\shellex\ContextMenuHandlers\{self.name}'
        return rf'{CLASSES_PATH}\{self.class_path}\shell\{self.name}'


@dataclass
class PackagedEntry:
    package_name: str
    package_full_name: str
    display_name: str
    verb_id: str
    clsid: str
    item_types: list = field(default_factory=list)
    blocked: bool = False
    blocked_by_user: bool = False

    @property
    def status(self):
        if self.blocked and not self.blocked_by_user:
            return 'Hidden (system)'
        return 'Hidden' if self.blocked else 'Visible'


# ---------------------------------------------------------------- helpers


def normalize_clsid(value):
    """Return a CLSID as '{UPPER-CASE}' or '' if the value is not a GUID."""
    value = (value or '').strip()
    if not GUID_PATTERN.match(value):
        return ''
    return '{' + value.strip('{}').upper() + '}'


def load_indirect_string(value):
    """Resolve '@dll,-id' / '@{package?ms-resource:...}' strings; return '' on failure."""
    buffer = ctypes.create_unicode_buffer(1024)
    try:
        result = ctypes.windll.shlwapi.SHLoadIndirectString(value, buffer, len(buffer), None)
    except (AttributeError, OSError):
        return ''
    return buffer.value if result == 0 else ''


def display_label(value):
    """Turn a registry menu label into the text the user sees."""
    if not value:
        return ''
    if value.startswith('@'):
        value = load_indirect_string(value) or value
    # '&' marks the keyboard accelerator; '&&' is a literal ampersand
    return value.replace('&&', '\0').replace('&', '').replace('\0', '&')


def _read_values(key):
    values = {}
    index = 0
    while True:
        try:
            name, data, _ = winreg.EnumValue(key, index)
        except OSError:
            break
        values[name] = data
        index += 1
    return values


def _subkey_names(key):
    names = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
    return names


def _key_exists(hive, path):
    try:
        winreg.CloseKey(winreg.OpenKey(hive, path))
        return True
    except OSError:
        return False


def _read_default(hive, path):
    try:
        with winreg.OpenKey(hive, path) as key:
            return str(winreg.QueryValueEx(key, '')[0] or '')
    except OSError:
        return ''


def _delete_tree(hive, path):
    """Delete a registry key and everything below it."""
    try:
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
            subkeys = _subkey_names(key)
    except FileNotFoundError:
        return
    for subkey in subkeys:
        _delete_tree(hive, rf'{path}\{subkey}')
    winreg.DeleteKey(hive, path)


def _copy_tree(hive, source_path, destination_path):
    """Copy a registry key (values and subkeys) to a new location."""
    with winreg.OpenKey(hive, source_path) as source, winreg.CreateKey(hive, destination_path) as destination:
        index = 0
        while True:
            try:
                name, data, value_type = winreg.EnumValue(source, index)
            except OSError:
                break
            winreg.SetValueEx(destination, name, 0, value_type, data)
            index += 1
        for subkey in _subkey_names(source):
            _copy_tree(hive, rf'{source_path}\{subkey}', rf'{destination_path}\{subkey}')


def _hidden_startupinfo():
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


# ---------------------------------------------------------------- targets


def get_class_paths(target, extension=''):
    """Return the registry class paths that make up a target's menu.

    The first path is the one new entries are written to.
    """
    if target == ALL_LOCATIONS_TARGET:
        return [class_path for class_path in TARGETS.values() if class_path]
    if target != FILE_EXTENSION_TARGET:
        return [TARGETS[target]]

    extension = normalize_extension(extension)
    if not extension:
        raise ContextMenuError('Please enter a file extension, e.g. .txt')
    class_paths = [rf'SystemFileAssociations\{extension}']
    prog_id = _read_default(winreg.HKEY_CLASSES_ROOT, extension)
    if prog_id and prog_id not in class_paths:
        class_paths.append(prog_id)
    return class_paths


def normalize_extension(extension):
    extension = (extension or '').strip().lower()
    if not extension:
        return ''
    if not extension.startswith('.'):
        extension = '.' + extension
    if not re.match(r'^\.[^\\/:*?"<>|\s]+$', extension):
        raise ContextMenuError(f'"{extension}" is not a valid file extension.')
    return extension


def command_placeholder(target):
    """Placeholder for the clicked item: %V for backgrounds, %1 otherwise."""
    return '%V' if target in BACKGROUND_TARGETS else '%1'


# ---------------------------------------------------------------- reading


def get_blocked_clsids():
    """Return (blocked_by_user, blocked_by_system) as sets of normalized CLSIDs."""
    result = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, BLOCKED_PATH) as key:
                result.append({normalize_clsid(name) for name in _read_values(key)} - {''})
        except OSError:
            result.append(set())
    return result[0], result[1]


def _read_verb_keys(hive, class_path):
    verbs = {}
    shell_path = rf'{CLASSES_PATH}\{class_path}\shell'
    try:
        with winreg.OpenKey(hive, shell_path) as shell_key:
            names = _subkey_names(shell_key)
    except OSError:
        return verbs

    for name in names:
        try:
            with winreg.OpenKey(hive, rf'{shell_path}\{name}') as verb_key:
                values = _read_values(verb_key)
                subkeys = {subkey.lower() for subkey in _subkey_names(verb_key)}
        except OSError:
            continue
        command = ''
        if 'command' in subkeys:
            try:
                with winreg.OpenKey(hive, rf'{shell_path}\{name}\command') as command_key:
                    command_values = _read_values(command_key)
                command = str(command_values.get('') or '')
                if not command and 'DelegateExecute' in command_values:
                    command = f'[COM] {command_values["DelegateExecute"]}'
            except OSError:
                pass
        verbs[name.lower()] = {'name': name,
                               'values': values,
                               'command': command,
                               'has_command': 'command' in subkeys,
                               'has_submenu': 'shell' in subkeys
                                              or 'SubCommands' in values
                                              or 'ExtendedSubCommandsKey' in values}
    return verbs


def _list_verbs(class_path):
    user_verbs = _read_verb_keys(winreg.HKEY_CURRENT_USER, class_path)
    system_verbs = _read_verb_keys(winreg.HKEY_LOCAL_MACHINE, class_path)
    entries = []

    for key in sorted(set(user_verbs) | set(system_verbs)):
        user = user_verbs.get(key)
        system = system_verbs.get(key)
        # a HKCU key without its own command is only an override of a system entry
        user_owned = user is not None and (system is None or user['has_command'] or user['has_submenu'])
        primary = user if user_owned else system
        effective = user or system
        values = dict(system['values']) if system else {}
        if user:
            values.update(user['values'])

        raw_label = str(values.get('MUIVerb') or values.get('') or '')
        has_submenu = primary['has_submenu'] or (system or {}).get('has_submenu', False)
        disabled = 'LegacyDisable' in effective['values']
        if disabled:
            status = 'Disabled'
        elif 'ProgrammaticAccessOnly' in effective['values']:
            status = 'Hidden (programmatic)'
        else:
            status = 'Enabled'

        entries.append(MenuEntry(kind=KIND_SUBMENU if has_submenu else KIND_VERB,
                                 class_path=class_path,
                                 name=primary['name'],
                                 label=display_label(raw_label) or primary['name'],
                                 raw_label=raw_label,
                                 has_default_label='' in primary['values'],
                                 command=primary['command'] or (system or {}).get('command', ''),
                                 icon=str(values.get('Icon') or ''),
                                 extended='Extended' in values,
                                 position=str(values.get('Position') or ''),
                                 disabled=disabled,
                                 status=status,
                                 user_owned=user_owned,
                                 in_system=system is not None))
    return entries


def _read_handler_keys(hive, class_path):
    handlers = {}
    handlers_path = rf'{CLASSES_PATH}\{class_path}\shellex\ContextMenuHandlers'
    try:
        with winreg.OpenKey(hive, handlers_path) as handlers_key:
            names = _subkey_names(handlers_key)
    except OSError:
        return handlers
    for name in names:
        clsid = normalize_clsid(_read_default(hive, rf'{handlers_path}\{name}')) or normalize_clsid(name)
        handlers[name.lower()] = {'name': name, 'clsid': clsid}
    return handlers


def _list_shell_extensions(class_path, blocked_by_user, blocked_by_system):
    user_handlers = _read_handler_keys(winreg.HKEY_CURRENT_USER, class_path)
    system_handlers = _read_handler_keys(winreg.HKEY_LOCAL_MACHINE, class_path)
    entries = []

    for key in sorted(set(user_handlers) | set(system_handlers)):
        handler = user_handlers.get(key) or system_handlers[key]
        clsid = handler['clsid']
        clsid_name = _read_default(winreg.HKEY_CLASSES_ROOT, rf'CLSID\{clsid}') if clsid else ''
        dll = _read_default(winreg.HKEY_CLASSES_ROOT, rf'CLSID\{clsid}\InprocServer32') if clsid else ''

        if handler['name'].startswith('-'):
            status = 'Disabled (renamed)'
        elif clsid in blocked_by_user:
            status = 'Blocked'
        elif clsid in blocked_by_system:
            status = 'Blocked (system)'
        else:
            status = 'Enabled'

        entries.append(MenuEntry(kind=KIND_SHELLEX,
                                 class_path=class_path,
                                 name=handler['name'],
                                 label=clsid_name or handler['name'],
                                 command=dll or clsid,
                                 disabled=status != 'Enabled',
                                 status=status,
                                 user_owned=key in user_handlers,
                                 in_system=key in system_handlers,
                                 clsid=clsid,
                                 blocked_by_user=clsid in blocked_by_user))
    return entries


def list_entries(class_paths):
    """Return all classic context menu entries for the given class paths."""
    blocked_by_user, blocked_by_system = get_blocked_clsids()
    entries = []
    for class_path in class_paths:
        entries.extend(_list_verbs(class_path))
        entries.extend(_list_shell_extensions(class_path, blocked_by_user, blocked_by_system))
    return entries


# ---------------------------------------------------------------- writing


def save_verb(class_path, old_name, name, label, command, icon='', extended=False, position='',
              disabled=False, is_submenu=False):
    """Create or update a HKCU shell verb (renaming it if old_name differs)."""
    name = (name or '').strip()
    if not name or '\\' in name:
        raise ContextMenuError('The key name must not be empty or contain "\\".')
    if position not in POSITIONS:
        raise ContextMenuError(f'Invalid position "{position}".')
    if not is_submenu and not (command or '').strip():
        raise ContextMenuError('The command must not be empty.')

    shell_path = rf'{CLASSES_PATH}\{class_path}\shell'
    path = rf'{shell_path}\{name}'

    if old_name and old_name.lower() != name.lower():
        if _key_exists(winreg.HKEY_CURRENT_USER, path):
            raise ContextMenuError(f'An entry named "{name}" already exists.')
        _copy_tree(winreg.HKEY_CURRENT_USER, rf'{shell_path}\{old_name}', path)
        _delete_tree(winreg.HKEY_CURRENT_USER, rf'{shell_path}\{old_name}')
    elif old_name and old_name != name:
        # change of letter case only: the registry is case-insensitive, go through a temp key
        temporary_path = rf'{shell_path}\{name}.renaming'
        _copy_tree(winreg.HKEY_CURRENT_USER, rf'{shell_path}\{old_name}', temporary_path)
        _delete_tree(winreg.HKEY_CURRENT_USER, rf'{shell_path}\{old_name}')
        _copy_tree(winreg.HKEY_CURRENT_USER, temporary_path, path)
        _delete_tree(winreg.HKEY_CURRENT_USER, temporary_path)
    elif not old_name and _key_exists(winreg.HKEY_CURRENT_USER, path):
        raise ContextMenuError(f'An entry named "{name}" already exists.')

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        values = _read_values(key)
        winreg.SetValueEx(key, 'MUIVerb', 0, winreg.REG_SZ, label or name)
        if '' in values:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, label or name)
        _set_or_delete(key, 'Icon', icon.strip() if icon else '')
        _set_or_delete(key, 'Position', position)
        _set_flag(key, 'Extended', extended)
        _set_flag(key, 'LegacyDisable', disabled)

        # a cascading menu reads its children from the 'shell' subkey when SubCommands is empty
        if is_submenu:
            winreg.SetValueEx(key, 'SubCommands', 0, winreg.REG_SZ, '')
        else:
            _set_or_delete(key, 'SubCommands', None)

    if is_submenu:
        _delete_tree(winreg.HKEY_CURRENT_USER, rf'{path}\command')
    else:
        command = command.strip()
        # REG_EXPAND_SZ lets commands use environment variables such as %SystemRoot%
        value_type = winreg.REG_EXPAND_SZ if re.search(r'%[A-Za-z_][\w()]*%', command) else winreg.REG_SZ
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf'{path}\command') as command_key:
            winreg.SetValueEx(command_key, '', 0, value_type, command)
    notify_shell()


def _set_or_delete(key, name, value):
    if value:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        return
    try:
        winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def _set_flag(key, name, enabled):
    _set_or_delete(key, name, None)
    if enabled:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, '')


def set_entry_disabled(entry, disabled):
    """Enable or disable (hide) an entry for the current user."""
    if entry.kind == KIND_SHELLEX:
        if not entry.clsid:
            raise ContextMenuError('This shell extension has no valid CLSID, it cannot be blocked.')
        if not disabled and entry.status == 'Blocked (system)':
            raise ContextMenuError('This extension is blocked system-wide (HKLM); administrator rights are needed.')
        if not disabled and entry.status == 'Disabled (renamed)':
            raise ContextMenuError('This extension was disabled by renaming its key; remove the leading "-" '
                                   'in the registry (or delete and re-install it) to enable it.')
        set_clsid_blocked(entry.clsid, disabled)
        return

    path = entry.registry_path()
    if disabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, 'LegacyDisable', 0, winreg.REG_SZ, '')
        notify_shell()
        return

    if not _key_exists(winreg.HKEY_CURRENT_USER, path):
        raise ContextMenuError('This entry is disabled system-wide (HKLM); administrator rights are needed.')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
        _set_flag(key, 'LegacyDisable', False)
        is_empty = not _read_values(key) and not _subkey_names(key)
    if not entry.user_owned and is_empty:
        # remove the HKCU override so the system entry is used unchanged again
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    notify_shell()


def set_clsid_blocked(clsid, blocked):
    """Add/remove a CLSID to/from the current user's shell extension block list."""
    clsid = normalize_clsid(clsid)
    if not clsid:
        raise ContextMenuError('Invalid CLSID.')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, BLOCKED_PATH) as key:
        if blocked:
            winreg.SetValueEx(key, clsid, 0, winreg.REG_SZ, '')
        else:
            for name in _read_values(key):
                if normalize_clsid(name) == clsid:
                    winreg.DeleteValue(key, name)
    notify_shell()


def backup_key(hive, path, backup_dir, label=''):
    """Export a registry key to a .reg file and return the file path."""
    os.makedirs(backup_dir, exist_ok=True)
    safe_label = re.sub(r'[^\w.-]+', '_', label or path.rsplit('\\', 1)[-1]).strip('_') or 'entry'
    file_path = os.path.join(backup_dir, f'{datetime.now():%Y%m%d_%H%M%S}_{safe_label}.reg')
    result = subprocess.run(['reg', 'export', rf'{HIVE_NAMES[hive]}\{path}', file_path, '/y'],
                            capture_output=True,
                            text=True,
                            startupinfo=_hidden_startupinfo(),
                            creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise ContextMenuError(f'Backup failed, nothing was deleted:\n{result.stderr.strip() or result.stdout.strip()}')
    return file_path


def restore_backup(file_path):
    """Import a .reg backup file created by backup_key."""
    with open(file_path, 'r', encoding='utf-16') as file:
        content = file.read()
    if 'HKEY_LOCAL_MACHINE' in content or 'HKEY_CLASSES_ROOT' in content:
        raise ContextMenuError('Only HKEY_CURRENT_USER backups can be restored without administrator rights.')
    result = subprocess.run(['reg', 'import', file_path],
                            capture_output=True,
                            text=True,
                            startupinfo=_hidden_startupinfo(),
                            creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise ContextMenuError(f'Restore failed:\n{result.stderr.strip() or result.stdout.strip()}')
    notify_shell()


def delete_entry(entry, backup_dir):
    """Back up and delete a HKCU entry; return the backup file path."""
    if not entry.user_owned:
        raise ContextMenuError('System (HKLM) entries cannot be deleted without administrator rights. '
                               'Use "Disable" to hide them for the current user instead.')
    path = entry.registry_path()
    backup_file = backup_key(winreg.HKEY_CURRENT_USER, path, backup_dir, f'{entry.class_path}_{entry.name}')
    _delete_tree(winreg.HKEY_CURRENT_USER, path)
    notify_shell()
    return backup_file


def notify_shell():
    """Tell Explorer that associations changed so menus refresh."""
    try:
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except (AttributeError, OSError):
        pass


def restart_explorer():
    subprocess.run(['taskkill', '/f', '/im', 'explorer.exe'],
                   capture_output=True,
                   startupinfo=_hidden_startupinfo(),
                   creationflags=CREATE_NO_WINDOW)
    subprocess.Popen(['explorer.exe'])


# ---------------------------------------------------------------- Windows 11


def is_windows_11():
    return sys.platform == 'win32' and sys.getwindowsversion().build >= 22000


def is_classic_menu_forced():
    return _key_exists(winreg.HKEY_CURRENT_USER, rf'{CLASSIC_MENU_PATH}\InprocServer32')


def set_classic_menu_forced(forced):
    """Show the full (classic) context menu directly on Windows 11 for the current user."""
    if forced:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf'{CLASSIC_MENU_PATH}\InprocServer32') as key:
            winreg.SetValueEx(key, '', 0, winreg.REG_SZ, '')
    else:
        _delete_tree(winreg.HKEY_CURRENT_USER, CLASSIC_MENU_PATH)


def _local_name(tag):
    return tag.rsplit('}', 1)[-1]


def _resolve_package_string(value, package_name, package_full_name):
    if not value.startswith('ms-resource:'):
        return value
    resource = value[len('ms-resource:'):]
    if resource.startswith('//'):
        uri = 'ms-resource:' + resource
    elif resource.startswith('/'):
        uri = f'ms-resource://{package_name}{resource}'
    else:
        uri = f'ms-resource://{package_name}/resources/{resource}'
    return load_indirect_string(f'@{{{package_full_name}?{uri}}}')


def _parse_manifest(package):
    manifest_path = os.path.join(package.get('InstallLocation') or '', 'AppxManifest.xml')
    try:
        root = ElementTree.parse(manifest_path).getroot()
    except (OSError, ElementTree.ParseError):
        return []

    package_name = package.get('Name') or ''
    package_full_name = package.get('PackageFullName') or ''
    display_name = ''
    for element in root.iter():
        if _local_name(element.tag) == 'Properties':
            for child in element:
                if _local_name(child.tag) == 'DisplayName' and child.text:
                    display_name = _resolve_package_string(child.text.strip(), package_name, package_full_name)
            break

    verbs = {}
    for element in root.iter():
        if _local_name(element.tag) != 'Extension' or element.get('Category') != 'windows.fileExplorerContextMenus':
            continue
        for item_type in element.iter():
            if _local_name(item_type.tag) != 'ItemType':
                continue
            for verb in item_type:
                if _local_name(verb.tag) != 'Verb':
                    continue
                clsid = normalize_clsid(verb.get('Clsid'))
                entry = verbs.setdefault((verb.get('Id') or '', clsid),
                                         PackagedEntry(package_name=package_name,
                                                       package_full_name=package_full_name,
                                                       display_name=display_name or package_name,
                                                       verb_id=verb.get('Id') or '',
                                                       clsid=clsid))
                item = item_type.get('Type') or ''
                if item not in entry.item_types:
                    entry.item_types.append(item)
    return list(verbs.values())


def scan_packaged_entries():
    """Return the Windows 11 context menu entries registered by packaged (MSIX/sparse) apps."""
    command = ('Get-AppxPackage | Select-Object Name,PackageFullName,InstallLocation '
               '| ConvertTo-Json -Compress')
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True,
                            text=True,
                            encoding='utf-8',
                            errors='replace',
                            startupinfo=_hidden_startupinfo(),
                            creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise ContextMenuError(f'Could not list the installed apps:\n{result.stderr.strip()}')
    try:
        packages = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as error:
        raise ContextMenuError(f'Could not parse the installed apps list: {error}') from error
    if isinstance(packages, dict):
        packages = [packages]

    blocked_by_user, blocked_by_system = get_blocked_clsids()
    entries = []
    for package in packages:
        for entry in _parse_manifest(package):
            entry.blocked_by_user = entry.clsid in blocked_by_user
            entry.blocked = entry.blocked_by_user or entry.clsid in blocked_by_system
            entries.append(entry)
    entries.sort(key=lambda item: (item.display_name.lower(), item.verb_id.lower()))
    return entries
