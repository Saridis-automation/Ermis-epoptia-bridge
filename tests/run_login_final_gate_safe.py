"""Explicit mocked/source-only final-gate suite; never run live acceptance tests."""
import subprocess
import unittest
from unittest.mock import patch


TESTS = (
    'tests.test_login_final_gate',
    'tests.test_login_first_install',
    'tests.test_login_source_preflight',
    'tests.test_login_kernel_stream',
    'tests.test_login_source_manifest.SourceManifest.test_allowlist_and_generator_are_canonical',
    'tests.test_login_source_manifest.SourceManifest.test_source_reader_type_mode_and_no_follow',
    'tests.test_login_source_manifest.SourceManifest.test_errno_is_preserved_without_message',
    'tests.test_login_parser_plumbing.Plumbing.test_exact_candidate_and_status_only_classification',
    'tests.test_login_parser_plumbing.Plumbing.test_failed_environment_never_runs_candidate',
    'tests.test_login_diagnose_readonly.TransactionEvidence',
    'tests.test_login_diagnose_readonly.ConflictScan',
    'tests.test_login_diagnose_readonly.Primitives.test_parser_commands_are_nonloading_and_noncaching',
    'tests.test_login_diagnose_readonly.Primitives.test_help_version_and_stderr_are_not_option_contracts',
    'tests.test_login_diagnose_readonly.Primitives.test_parser_missing_timeout_and_exec_failure',
    'tests.test_login_diagnose_readonly.Primitives.test_duplicate_json_rejected',
    'tests.test_login_diagnose_readonly.Primitives.test_residue_names_never_opened',
    'tests.test_login_diagnose_readonly.Primitives.test_noatime_and_symlink_special_rejection',
    'tests.test_login_diagnose_readonly.Primitives.test_source_manifest_and_fixed_entries',
)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromNames(TESTS)
    with patch.object(subprocess, 'run', side_effect=AssertionError('unmocked command forbidden')), \
            patch.object(subprocess, 'Popen', side_effect=AssertionError('unmocked process forbidden')):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
