# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 설정.

윈도우에서 아래를 실행하면 dist/사진찾기.exe 가 만들어진다.

    pip install pyinstaller
    pyinstaller 사진찾기.spec

exe 는 만든 운영체제에서만 돌아간다. 윈도우용 exe 는 윈도우에서 만들어야 한다.
"""

block_cipher = None

a = Analysis(
    ['packaging/entry_gui.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'findpic',
        'findpic.gui',
        'findpic.pipeline',
        'findpic.hwp.hwpx',
        'olefile',
        'PIL.Image',
        'PIL.ImageOps',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 쓰지 않는 큰 꾸러미를 빼서 exe 크기를 줄인다
    excludes=[
        'matplotlib', 'scipy', 'pandas', 'pytest', 'IPython', 'notebook',
        'PyQt5', 'PySide2', 'PySide6', 'wx', 'sqlalchemy', 'setuptools',
        'PIL.ImageQt', 'PIL.ImageTk',
    ],
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
    name='사진찾기',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 검은 명령창을 띄우지 않는다
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
