# Old name, kept so existing callers keep working (signlab_signcollect-stack#51).
# pythonCron configs and units on the core server may still start this path.
import os
import runpy

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_disk.py"),
               run_name="__main__")
