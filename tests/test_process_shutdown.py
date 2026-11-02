"""Do not restore fan registers while a native worker thread remains alive."""

import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from nitrofan.nbfc_guardian import process_identity, stop_process_group


class TestProcessIdentity(unittest.TestCase):
    def test_zombie_leader_with_live_threads_retains_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = root / 'proc/123'
            task = process / 'task/124'
            task.mkdir(parents=True)
            fields = ['Z'] + ['0'] * 18 + ['456']
            (process / 'stat').write_text('123 (nbfc_service) ' + ' '.join(fields))
            (task / 'stat').write_text('124 (native worker) S')
            with patch('nitrofan.nbfc_guardian.Path', side_effect=lambda path: root / path.lstrip('/')):
                self.assertEqual(process_identity(123), '456')
                (task / 'stat').write_text('124 (native worker) Z')
                self.assertIsNone(process_identity(123))

    def test_changed_identity_is_never_signalled(self):
        with patch('nitrofan.nbfc_guardian.process_identity', return_value='different'), \
                patch('nitrofan.nbfc_guardian.os.killpg') as signal_group:
            with self.assertRaisesRegex(RuntimeError, 'changed NBFC process identity'):
                stop_process_group(123, 'original')
            signal_group.assert_not_called()


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc'),
                     'Requires Linux procfs and a C compiler; no hardware access')
class TestThreadedShutdown(unittest.TestCase):
    def test_zombie_leader_does_not_hide_a_running_native_thread(self):
        source = r'''
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static void *wait_for_signal(void *argument) {
    (void)argument;
    for (;;) {
        pause();
    }
    return NULL;
}

int main(void) {
    pthread_t worker;
    signal(SIGTERM, SIG_IGN);
    if (pthread_create(&worker, NULL, wait_for_signal, NULL) != 0) {
        return EXIT_FAILURE;
    }
    puts("ready");
    fflush(stdout);
    pthread_exit(NULL);
    return EXIT_SUCCESS;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / 'native-thread-test'
            subprocess.run(['cc', '-Wall', '-Wextra', '-Werror', '-pthread', '-x', 'c',
                            '-o', str(binary), '-'], input=source, text=True, check=True, timeout=20)
            with subprocess.Popen([str(binary)], start_new_session=True,
                                  stdout=subprocess.PIPE) as process:
                try:
                    self.assertTrue(select.select([process.stdout], [], [], 5)[0])
                    self.assertEqual(process.stdout.readline(), b'ready\n')
                    deadline = time.monotonic() + 5
                    leader_stat = Path(f'/proc/{process.pid}/stat')
                    while leader_stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                        if time.monotonic() >= deadline:
                            self.fail('The fixture leader did not exit')
                        time.sleep(0.01)
                    identity = process_identity(process.pid)
                    self.assertIsNotNone(identity, 'Live worker must retain process ownership')
                    stop_process_group(process.pid, identity)
                    self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
