import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from canopy_observer import _build_parser, main, prune_reports


class RetentionTests(unittest.TestCase):
    def test_only_expired_reports_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ['canopy-status-20260801T000000Z.json',
                     'canopy-status-20260826T000000Z.json',
                     'canopy-status-20260924T000000Z.json',
                     'canopy-status-20270101T000000Z.json',
                     'canopy-status-20260230T000000Z.json',
                     'latest.json', 'height-state.json', 'other.json']
            for name in names:
                (root / name).write_text('{}')
            keep = root / 'canopy-status-20200101T000000Z.json'
            keep.write_text('{}')
            link = root / 'canopy-status-20200102T000000Z.json'
            link.symlink_to(root / 'other.json')
            nested = root / 'canopy-status-20200103T000000Z.json'
            nested.mkdir()
            (nested / names[0]).write_text('{}')
            prune_reports(root, 30, keep, datetime(2026, 9, 25, tzinfo=timezone.utc))
            self.assertFalse((root / names[0]).exists())
            for name in names[1:]:
                self.assertTrue((root / name).exists(), name)
            self.assertTrue(keep.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue((nested / names[0]).exists())

    def test_disabled_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'canopy-status-20000101T000000Z.json'
            path.write_text('{}')
            prune_reports(Path(tmp), 0, Path(tmp) / 'latest.json')
            self.assertTrue(path.exists())

    def test_configuration(self):
        with patch.dict(os.environ, {'CANOPY_RETENTION_DAYS': '7'}):
            self.assertEqual(_build_parser().parse_args([]).retention_days, 7)
            self.assertEqual(_build_parser().parse_args(['--retention-days', '0']).retention_days, 0)
            for value in ['-1', '1.5', 'nan']:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    _build_parser().parse_args(['--retention-days', value])
        with patch.dict(os.environ, {'CANOPY_RETENTION_DAYS': '-1'}):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _build_parser().parse_args([])

    def test_cleanup_gated_by_successful_save(self):
        for mode in ('normal', 'no-report', 'healthcheck', 'save-failed', 'state-failed', 'cleanup-failed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                report = {'status': 'OK', 'checks': []}
                with patch('canopy_observer.load_dotenv'), patch('canopy_observer.collect_report', return_value=report), \
                     patch('canopy_observer.evaluate_height_progress', return_value={'height': 1}), \
                     patch('canopy_observer._save_report', return_value=Path(tmp)/'new.json') as save, \
                     patch('canopy_observer._save_height_state') as state, \
                     patch('canopy_observer.prune_reports') as prune, \
                     patch('canopy_observer.check_report_health', return_value=(True, 'OK')), \
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                    if mode == 'save-failed': save.side_effect = OSError('disk full')
                    if mode == 'state-failed': state.side_effect = OSError('disk full')
                    if mode == 'cleanup-failed': prune.side_effect = PermissionError('denied')
                    args = ['--report-dir', tmp, '--retention-days', '7']
                    if mode in ('no-report', 'healthcheck'): args.append('--'+mode)
                    result = main(args)
                    if mode in ('normal', 'cleanup-failed'):
                        prune.assert_called_once_with(Path(tmp), 7, Path(tmp)/'new.json')
                        self.assertEqual(result, 0)
                    else:
                        prune.assert_not_called()
                    if mode == 'cleanup-failed': self.assertIn('cleanup failed', err.getvalue())
