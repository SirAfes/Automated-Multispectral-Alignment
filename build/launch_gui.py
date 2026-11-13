"""PyInstaller entry point for the Windows executable.

`Automated_Multispectral_Alignment.exe --self-test REPORT.txt` imports every
module the program needs, writes the result to REPORT.txt and exits with 0 on
success. The build workflows run it, so an executable that cannot start is
never published.
"""

import sys


def self_test(report_path):
    lines, ok = [], True
    try:
        import importlib
        import pkgutil

        import msalign

        names = ["msalign"] + [f"msalign.{m.name}" for m in pkgutil.iter_modules(msalign.__path__)]
        for name in names:
            importlib.import_module(name)
        lines.append(f"msalign {msalign.__version__}: {len(names)} modules")

        import cv2
        import rawpy
        import tifffile

        lines.append(f"OpenCV {cv2.__version__}, AKAZE: {hasattr(cv2, 'AKAZE_create')}")
        lines.append(f"rawpy {rawpy.__version__}, LibRaw {rawpy.libraw_version}")
        lines.append(f"tifffile {tifffile.__version__}")
        import tkinter  # noqa: F401
        lines.append("tkinter: ok")

        import concurrent.futures
        import multiprocessing
        import os

        ctx = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(1, mp_context=ctx) as pool:
            child = pool.submit(os.getpid).result(timeout=120)
        lines.append(f"worker process: ok (pid {child})")
    except Exception as exc:  # report anything that would stop the program
        ok = False
        import traceback

        lines.append("FAILED: " + "".join(traceback.format_exception(exc)))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    # Must come first: in the frozen EXE, worker processes for parallel
    # processing start this same program; freeze_support() turns them into
    # workers instead of opening another window.
    import multiprocessing

    multiprocessing.freeze_support()

    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        sys.exit(self_test(sys.argv[2]))

    from msalign.gui import main

    main()
