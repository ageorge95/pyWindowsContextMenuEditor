# pyWindowsContextMenuEditor

PySide6 GUI to view and edit the Windows Explorer right-click (context) menu.
Built from the [ag95_repo_base](https://github.com/ageorge95/ag95_repo_base) templates.

All changes are written to `HKEY_CURRENT_USER`, so administrator rights are
never needed.

## Features

### Classic context menu tab

- Choose what is right-clicked: all files, files and folders, folders, all
  folders (incl. special), folder / library / desktop background, drives, or a
  specific file extension (e.g. `.txt`). `All locations` (default) shows
  everything at once, with a `Location` column.
- Search box to filter by menu text, command or key name.
- Lists commands, submenus (with their items) and COM shell extensions
  (7-Zip, TortoiseGit, ...) from both HKCU and HKLM.
- **Add** commands and submenus (also items inside your own submenus).
- **Edit** your own entries: menu text, key name, command, icon, position
  (top/bottom), Shift-only (`Extended`) and disabled state.
- **Disable / Enable** any entry, including system ones, for the current user:
  - commands: `LegacyDisable` value in a HKCU override key
  - shell extensions: the HKCU `Shell Extensions\Blocked` list
- **Delete** your own (HKCU) entries. A `.reg` backup is always exported to the
  `backups` folder first; use **Restore backup...** to import it again.

Use `%1` in commands for the clicked file/folder and `%V` for the folder of a
background click.

### Windows 11 menu tab

- Toggle the full (classic) menu so "Show more options" is skipped.
- Lists the entries packaged (MSIX / sparse) apps add to the new Windows 11
  menu (Notepad++, Terminal, Photos, ...) and lets you hide / show them for the
  current user. These entries cannot be created or deleted, they come from the
  apps' manifests.

Use **Restart Explorer** (status bar) if a change is not visible immediately.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | PySide6 GUI |
| `context_menu_registry.py` | Registry backend (read / write / backup) |
| `icon.ico` | Application/taskbar icon |
| `version.txt` | Version shown in the window title |
| `requirements.txt` | Python dependencies |
| `main.spec` | PyInstaller one-folder build definition |
| `BUILD_release.bat` | Builds the executable with PyInstaller |
| `START.bat` | Runs the app from the local virtual environment |
| `_install_templates/` | Generic install/update scripts (PowerShell and Bash) |

## Usage

1. Install the environment:
   `powershell -ExecutionPolicy Bypass -File _install_templates\install.ps1`
2. Start the app with `START.bat` (or `python main.py`).
3. Update the environment later with `_install_templates\update.ps1`.
4. Build a release with `BUILD_release.bat`; the output goes to `dist\main\`.
