"""The install payload excludes stale build attestations and compiled fixtures."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.install_backend import copy_tree, install_dependencies, validate_installation, SERVICE_UNITS


class TestInstallPayload(unittest.TestCase):
    def test_incomplete_observations_do_not_misreport_installation_failure(self):
        with patch('tools.install_backend.subprocess.run',
                   return_value=subprocess.CompletedProcess(['validate'], 2)):
            validate_installation()

    def test_real_validation_failure_still_fails_installer(self):
        with patch('tools.install_backend.subprocess.run',
                   return_value=subprocess.CompletedProcess(['validate'], 1)):
            with self.assertRaises(subprocess.CalledProcessError):
                validate_installation()
    def test_dependency_install_never_waits_for_redirected_terminal_input(self):
        with patch('tools.install_backend.run') as run:
            install_dependencies()
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(call.kwargs['env']['DEBIAN_FRONTEND'], 'noninteractive')
            self.assertEqual(call.kwargs['env']['NEEDRESTART_MODE'], 'l')
            self.assertIn('Dpkg::Use-Pty=0', call.args)

    def test_sleep_observer_is_part_of_installed_units(self):
        self.assertIn('nitrofan-suspend-check.service', SERVICE_UNITS)

    def test_old_manifest_and_machine_outputs_are_not_installed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / 'source', root / 'destination'
            source.mkdir()
            for filename in ('ec_guard.c', 'guarded_nbfc_manifest.json', 'ec_guard.o',
                             'libnbfc_ec_guard.so', 'libnbfc_ec_guard.dylib', 'test_ec_guard'):
                (source / filename).write_text('old artifact')
            (source / 'bin').mkdir()
            (source / 'bin/nbfc_service').write_text('old binary')
            with patch('tools.install_backend.check_destination'):
                copy_tree(source, destination)
            self.assertEqual([path.name for path in destination.iterdir()], ['ec_guard.c'])

    def test_symlink_source_is_rejected_instead_of_followed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / 'source', root / 'destination'
            source.mkdir()
            (root / 'outside').write_text('not in payload')
            (source / 'policy.json').symlink_to(root / 'outside')
            with patch('tools.install_backend.check_destination'):
                with self.assertRaisesRegex(RuntimeError, 'symlink'):
                    copy_tree(source, destination)
            self.assertFalse((destination / 'policy.json').exists())


if __name__ == '__main__':
    unittest.main()
