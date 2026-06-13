"""Thin wrapper around the AWS CLI invoked as a subprocess.

Avoids a boto3 dependency: the AWS CLI is already required for credentials.
Read commands always run; mutating commands are skipped (and printed) when
dry_run is enabled.
"""

import json
import shutil
import subprocess


class AwsError(RuntimeError):
    """An AWS CLI call returned a non-zero exit code."""

    def __init__(self, args, returncode, stderr):
        self.args = args
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"aws {' '.join(args)} failed ({returncode}): {self.stderr}")


class Aws:
    def __init__(self, region=None, dry_run=False):
        self.region = region
        self.dry_run = dry_run

    def ensure_cli(self):
        if shutil.which("aws") is None:
            raise AwsError(["--version"], 127, "AWS CLI not found on PATH")

    def _base(self, args):
        cmd = ["aws"] + list(args)
        if self.region and "--region" not in args:
            cmd += ["--region", self.region]
        return cmd

    def run(self, args, mutating=False, json_output=True, check=True):
        """Run an AWS CLI command.

        mutating: when True and dry_run is on, the command is printed and
                  skipped instead of executed.
        json_output: append `--output json` and parse the result.
        Returns parsed JSON (dict/list), raw stdout str, or None (dry-run skip).
        """
        args = list(args)
        if json_output and "--output" not in args:
            args += ["--output", "json"]

        cmd = self._base(args)

        if mutating and self.dry_run:
            print(f"  [dry-run] {' '.join(cmd)}")
            return None

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            if check:
                raise AwsError(args, proc.returncode, proc.stderr)
            return None

        out = proc.stdout.strip()
        if not json_output:
            return out
        if not out:
            return {}
        return json.loads(out)

    # -- convenience -----------------------------------------------------

    def caller_identity(self):
        return self.run(["sts", "get-caller-identity"])

    def configured_region(self):
        """Region from `aws configure get region`, or None."""
        out = self.run(
            ["configure", "get", "region"], json_output=False, check=False
        )
        return out or None
