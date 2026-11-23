"""Offline checks of Weather Extractor's NOAA AORC dependency installer (pip is faked)."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from weather_extractor import dependencies


class FakeProcess:
    def __init__(self, code=0, output="", hang=False):
        self.code, self.output, self.hang, self.killed = code, output, hang, False

    def communicate(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("pip", timeout)
        return self.output, None

    def kill(self):
        self.killed = True

    @property
    def returncode(self):
        return self.code


class FakePopen:
    def __init__(self, **process):
        self.process, self.commands = process, []

    def __call__(self, command, **_options):
        self.commands.append(command)
        return FakeProcess(**self.process)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.target = Path(self.temp.name) / "libraries"
        self.path = list(sys.path)

    def tearDown(self):
        sys.path[:] = self.path
        self.temp.cleanup()

    def test_pinned_set_covers_the_aorc_modules(self):
        names = {pin.split("==")[0].lower() for pin in dependencies.pins()}
        self.assertTrue(all("==" in pin for pin in dependencies.pins()), "every library pinned")
        self.assertLessEqual(set(dependencies.AORC_MODULES), names)

    def test_wheels_first_then_source_only_packages(self):
        wheels, sources = dependencies.pip_commands(self.target, python="python")
        for command in (wheels, sources):
            self.assertEqual(command[:4], ["python", "-m", "pip", "install"])
            self.assertIn("--no-deps", command)
            self.assertEqual(command[command.index("--target") + 1], str(self.target))
        self.assertIn("--only-binary=:all:", wheels)
        self.assertIn("numcodecs==0.15.1", wheels)
        self.assertNotIn("--only-binary=:all:", sources)
        self.assertEqual([pin for pin in sources if "==" in pin], ["asciitree==0.3.3"])

    @patch("weather_extractor.dependencies.missing", return_value=[])
    def test_install_adds_the_folder_to_the_path(self, _missing):
        popen = FakePopen()
        messages = []
        folder = dependencies.install(self.target, progress=lambda p, m: messages.append(p), popen=popen)
        self.assertEqual(folder, self.target)
        self.assertIn(str(self.target), sys.path)
        self.assertEqual(len(popen.commands), 2)
        self.assertEqual(messages[-1], 100.0)

    def test_pip_failure_shows_its_last_lines(self):
        output = "\n".join(f"line {n}" for n in range(20)) + "\nERROR: No matching distribution found"
        with self.assertRaises(RuntimeError) as raised:
            dependencies.install(self.target, popen=FakePopen(code=1, output=output))
        self.assertIn("pypi.org", str(raised.exception))
        self.assertIn("No matching distribution", str(raised.exception))
        self.assertNotIn("line 3\n", str(raised.exception))

    def test_cancel_stops_pip(self):
        with self.assertRaises(InterruptedError):
            dependencies.install(self.target, is_canceled=lambda: True, popen=FakePopen(hang=True))

    def test_one_folder_per_python_version(self):
        folder = dependencies.library_dir()
        self.assertEqual(folder.name, f"python{sys.version_info.major}{sys.version_info.minor}")
        self.assertEqual(folder.parent.parent.name, "weather_extractor")


if __name__ == "__main__":
    unittest.main()
