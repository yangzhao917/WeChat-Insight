"""
一键打包 WeChat Insight 为单个 exe
用法: python build.py
"""
import subprocess
import sys
import shutil
import os

def main():
    # 查找 wechat-cli.exe 的路径
    wechat_cli_path = shutil.which("wechat-cli")
    if not wechat_cli_path:
        print("错误: 找不到 wechat-cli，请先安装: pip install wechat-cli")
        sys.exit(1)
    print(f"找到 wechat-cli: {wechat_cli_path}")

    # 确保 data 目录存在
    os.makedirs("data", exist_ok=True)

    # 使用 spec 文件方式打包，避免路径转义问题
    spec_content = f'''# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[(r'{wechat_cli_path}', '.')],
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
'''
    with open('WeChat_Insight.spec', 'w', encoding='utf-8') as f:
        f.write(spec_content)

    print("开始打包...")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "WeChat_Insight.spec"], check=True)
    print("\n打包完成! 输出文件: dist/WeChat_Insight.exe")


if __name__ == "__main__":
    main()
