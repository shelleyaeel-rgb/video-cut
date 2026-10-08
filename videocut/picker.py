"""Run native dialogs in a separate main-thread process for Tk safety."""
import json
import sys
import os
import argparse
from pathlib import Path


def windows_picker(audio):
    import ctypes
    from ctypes import wintypes as w
    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [('lStructSize',w.DWORD),('hwndOwner',w.HWND),('hInstance',w.HINSTANCE),('lpstrFilter',w.LPCWSTR),('lpstrCustomFilter',w.LPWSTR),('nMaxCustFilter',w.DWORD),('nFilterIndex',w.DWORD),('lpstrFile',w.LPWSTR),('nMaxFile',w.DWORD),('lpstrFileTitle',w.LPWSTR),('nMaxFileTitle',w.DWORD),('lpstrInitialDir',w.LPCWSTR),('lpstrTitle',w.LPCWSTR),('Flags',w.DWORD),('nFileOffset',w.WORD),('nFileExtension',w.WORD),('lpstrDefExt',w.LPCWSTR),('lCustData',ctypes.c_ssize_t),('lpfnHook',ctypes.c_void_p),('lpTemplateName',w.LPCWSTR),('pvReserved',ctypes.c_void_p),('dwReserved',w.DWORD),('FlagsEx',w.DWORD)]
    buffer=ctypes.create_unicode_buffer(32768)
    config=OPENFILENAMEW()
    config.lStructSize=ctypes.sizeof(config)
    config.lpstrFile=ctypes.cast(buffer,w.LPWSTR)
    config.nMaxFile=len(buffer)
    config.lpstrTitle='选择 BGM' if audio else '选择 OBS 原始录像'
    config.lpstrFilter='媒体文件\0'+('*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;*.mp4;*.mkv' if audio else '*.mp4;*.mkv;*.mov;*.avi;*.webm;*.ts')+'\0所有文件\0*.*\0\0'
    config.Flags=0x80000|0x1000|0x800|0x8
    dialog=ctypes.WinDLL('comdlg32',use_last_error=True)
    dialog.GetOpenFileNameW.argtypes=[ctypes.POINTER(OPENFILENAMEW)]
    dialog.GetOpenFileNameW.restype=w.BOOL
    ok=dialog.GetOpenFileNameW(ctypes.byref(config))
    if not ok and dialog.CommDlgExtendedError():
        raise RuntimeError('Windows file dialog failed')
    return buffer.value if ok else ''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('kind', choices=('music', 'video'), nargs='?', default='video')
    parser.add_argument('--result', type=Path)
    args = parser.parse_args()
    def emit(value):
        payload = json.dumps(value, ensure_ascii=True)
        if args.result:
            args.result.write_text(payload, encoding='utf-8')
        else:
            print(payload)
    try:
        audio = args.kind == 'music'
        if os.name == 'nt':
            emit({'path': windows_picker(audio)})
            return
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected = filedialog.askopenfilename(title='选择 BGM' if audio else '选择 OBS 原始录像', filetypes=[('媒体文件', '*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.mp4 *.mkv' if audio else '*.mp4 *.mkv *.mov *.avi *.webm *.ts'), ('所有文件', '*.*')])
        root.destroy()
        emit({'path': selected})
    except Exception:
        emit({'error': '文件选择窗口不可用，请复制完整路径到输入框。'})


if __name__ == '__main__':
    main()
