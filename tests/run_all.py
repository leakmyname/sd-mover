# -*- coding: utf-8 -*-
"""Run every verification script: python tests/run_all.py [script ...]

These scripts only exercise real logic against fake drives, a temp dir and
offscreen Qt. They never touch a real SD card or external disk, and all test
files live under the system temp dir and are cleaned up afterwards.
"""
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)

ALL = [
    'verify_plan.py',
    'verify_acct.py',
    'verify_mixed.py',
    'verify_scatter.py',
    'verify_conc2.py',
]


def main():
    targets = sys.argv[1:] or ALL
    env = dict(os.environ)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['PYTHONIOENCODING'] = 'utf-8'
    failed = []
    for t in targets:
        path = t if (os.path.sep in t) else os.path.join(TESTS_DIR, t)
        if not os.path.exists(path):
            print('!! missing %s' % path, flush=True)
            failed.append(t)
            continue
        print('\n########## %s ##########' % t, flush=True)
        if subprocess.call([sys.executable, path], cwd=REPO_ROOT, env=env) != 0:
            failed.append(t)
    shutil.rmtree(os.path.join(tempfile.gettempdir(), 'sdm_verify'),
                  ignore_errors=True)
    print('\n========== summary ==========')
    print('scripts: %d   failed: %d' % (len(targets), len(failed)))
    for f in failed:
        print('  FAILED: %s' % f)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
