# build.spec  —  PyInstaller spec for Lemokey X4 Profile Switcher
#
# Build with:
#   pyinstaller build.spec
#
# Output: dist/LemokeyProfileSwitcher.exe

block_cipher = None

a = Analysis(
    ['lemokey_switcher.py'],
    pathex=[],
    binaries=[],
    datas=[],          # add ('icon.ico', '.') here if you have a custom icon
    hiddenimports=[
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'pystray._win32',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LemokeyProfileSwitcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='icon.ico',      # uncomment if you have a custom .ico
)
