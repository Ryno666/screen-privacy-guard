#!/usr/bin/env python3
"""Build/install only the control UI; never starts the camera service."""
from pathlib import Path
import plistlib
import subprocess

ROOT = Path(__file__).resolve().parent
APP = Path.home()/'Applications/屏幕旁观保护.app'
EXECUTABLE = APP/'Contents/MacOS/ScreenPrivacyControl'
EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
(APP/'Contents/Info.plist').write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'com.local.screen-privacy-guard.control',
    'CFBundleName': '屏幕旁观保护', 'CFBundleDisplayName': '屏幕旁观保护',
    'CFBundleExecutable': 'ScreenPrivacyControl', 'CFBundlePackageType': 'APPL',
    'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0',
    'LSMinimumSystemVersion': '13.0', 'NSHighResolutionCapable': True,
}))
subprocess.run(['/usr/bin/xcrun','swiftc','-g','-swift-version','5',str(ROOT/'ControlApp.swift'),'-o',str(EXECUTABLE)],check=True)
subprocess.run([str(EXECUTABLE),'--self-test'],check=True)
subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(APP)],check=True)
register='/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
subprocess.run([register,'-f',str(APP)],check=True)
entry=ROOT/'旁观保护控制面板.app'
if not entry.exists() and not entry.is_symlink():
    entry.symlink_to(APP,target_is_directory=True)
print(APP)
