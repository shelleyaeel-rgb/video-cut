"""Run native dialogs in a separate main-thread process for Tk safety."""
import json
import sys
import os


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
    try:
        audio = len(sys.argv) > 1 and sys.argv[1] == 'music'
        if os.name == 'nt':
            print(json.dumps({'path': windows_picker(audio)}, ensure_ascii=True))
            return
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        selected = filedialog.askopenfilename(title='选择 BGM' if audio else '选择 OBS 原始录像', filetypes=[('媒体文件', '*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.mp4 *.mkv' if audio else '*.mp4 *.mkv *.mov *.avi *.webm *.ts'), ('所有文件', '*.*')])
        root.destroy()
        print(json.dumps({'path': selected}, ensure_ascii=True))
    except Exception:
        print(json.dumps({'error': '文件选择窗口不可用，请复制完整路径到输入框。'}, ensure_ascii=True))


if __name__ == '__main__':
    main()
