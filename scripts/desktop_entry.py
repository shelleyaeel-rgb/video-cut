"""PyInstaller entry point; worker commands use this same executable."""
import multiprocessing


if __name__ == '__main__':
    multiprocessing.freeze_support()
    from videocut.desktop import main
    raise SystemExit(main())
