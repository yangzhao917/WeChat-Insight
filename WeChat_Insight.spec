# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[(r'C:\Users\17404\AppData\Local\Programs\Python\Python313\Scripts\wechat-cli.EXE', '.')],
    datas=[('templates', 'templates'), ('data', 'data')],
    hiddenimports=['flask', 'openai', 'click', 'zstandard', 'wechat_cli',
                   'Crypto', 'Crypto.Cipher', 'Crypto.Cipher.AES'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name='WeChat_Insight',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
