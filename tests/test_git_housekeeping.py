"""All Git execution is mocked: never stage or commit the working repository."""
import inspect
import json
import subprocess
import unittest
from unittest.mock import patch

import git_housekeeping as housekeeping


def response(code=0, output=""):
    return subprocess.CompletedProcess([], code, output, "raw private diagnostic")


class CommitTest(unittest.TestCase):
    def success(self, *, staged=1, commit_code=0, status=""):
        replies = [response(output="Existing User"), response(output="existing@example.test"),
                   response(output="app.py\x00"), response(), response(staged)]
        if staged == 1:
            replies.append(response(commit_code))
        return replies + [response(output="a" * 40 + "\n"), response(output=status)]

    def test_fixed_repository_and_literal_message(self):
        message = "--author=Other $(arbitrary command)\nCommit details"
        with patch.object(housekeeping.subprocess, "run", side_effect=self.success()) as run:
            result = housekeeping.commit(message)
        self.assertEqual(result, dict(ok=True, committed=True, commit_hash="a" * 12, clean=True))
        self.assertEqual(list(inspect.signature(housekeeping.commit).parameters), ["message"])
        commands = []
        for call in run.call_args_list:
            command = call.args[0]
            commands.append(command[9:])
            self.assertEqual(command[:9], ["/usr/bin/git", "--git-dir",
                "/home/ermis/projects/epoptia-bridge/.git", "--work-tree",
                "/home/ermis/projects/epoptia-bridge", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgSign=false"])
            self.assertEqual(call.kwargs["cwd"], "/home/ermis/projects/epoptia-bridge")
            self.assertEqual(call.kwargs["env"], {"HOME": "/home/ermis", "PATH": "/usr/bin:/bin"})
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn(["add", "--all", "--", "."], commands)
        self.assertEqual([c for c in commands if c[0] == "commit"],
                         [["commit", "--no-verify", "--message", message]])
        self.assertEqual([c for c in commands if c[0] == "config"],
                         [["config", "--get", "user.name"], ["config", "--get", "user.email"]])

    def test_empty_message_rejected_before_git(self):
        for message in ("", " \n\t", None, 42, "a\x00b"):
            with self.subTest(message=message), patch.object(housekeeping.subprocess, "run") as run:
                self.assertFalse(housekeeping.commit(message)["ok"])
                run.assert_not_called()

    def test_missing_identity_does_not_stage_or_write_config(self):
        for replies in ([response(1)], [response(output=" \n")],
                        [response(output="Existing User"), response(1)]):
            with patch.object(housekeeping.subprocess, "run", side_effect=replies) as run:
                result = housekeeping.commit("Commit")
            self.assertEqual(result["error"], "Git identity is missing")
            self.assertFalse(result["committed"])
            for call in run.call_args_list:
                self.assertEqual(call.args[0][9:11], ["config", "--get"])

    def test_safe_output_on_failure_and_dirty_status(self):
        replies = self.success(commit_code=1, status="?? private-filename\n")
        replies[-2] = response(output="invalid hash with private data")
        with patch.object(housekeeping.subprocess, "run", side_effect=replies):
            result = housekeeping.commit("private message")
        self.assertEqual(result, dict(ok=False, committed=False, commit_hash=None,
                                     clean=False, error="Git commit failed"))
        self.assertNotIn("private", json.dumps(result))

    def test_no_changes_does_not_create_commit(self):
        with patch.object(housekeeping.subprocess, "run", side_effect=self.success(staged=0)) as run:
            result = housekeeping.commit("Commit")
        self.assertTrue(result["ok"])
        self.assertFalse(result["committed"])
        self.assertFalse(any(c.args[0][9] == "commit" for c in run.call_args_list))

    def test_sensitive_paths_rejected_without_staging(self):
        for path in (".env", "nested/.env.local", "credentials.json", "key.pem", ".ssh/id_rsa"):
            with self.subTest(path=path), patch.object(housekeeping.subprocess, "run",
                    side_effect=[response(output="Name"), response(output="Email"),
                                 response(output=path + "\x00")]) as run:
                result = housekeeping.commit("Commit")
            self.assertEqual(result["error"], "Sensitive paths prevent committing")
            self.assertEqual(run.call_count, 3)

    def test_exceptions_do_not_escape(self):
        for error in (OSError("private details"),
                      subprocess.TimeoutExpired("private command", 30, output="private output")):
            with patch.object(housekeeping.subprocess, "run", side_effect=error):
                result = housekeeping.commit("Commit")
            self.assertFalse(result["ok"])
            self.assertNotIn("private", json.dumps(result))

    def test_staging_failure_does_not_commit(self):
        replies = self.success()
        replies[3:] = [response(1), response(1), response(output=" M app.py\n")]
        with patch.object(housekeeping.subprocess, "run", side_effect=replies) as run:
            result = housekeeping.commit("Commit")
        self.assertEqual(result["error"], "Git staging failed")
        self.assertFalse(result["clean"])
        self.assertFalse(any(c.args[0][9] == "commit" for c in run.call_args_list))
