import logging
import os
import subprocess
import sys
import tempfile

from codexapi import Task, agent

logger = logging.getLogger(__name__)


class HelloWorldTask(Task):
    def __init__(self):
        prompt = (
            "Create a hello world program named hello.py in this directory. "
            "Use Python and print a short greeting. Do not touch anything "
            "outside this directory."
        )
        super().__init__(prompt)
        self._tmpdir = None

    def set_up(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cwd = self._tmpdir.name
        self.agent.cwd = self.cwd
        logger.debug("set_up: created %s", self.cwd)

    def tear_down(self):
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
            logger.debug("tear_down: deleted %s", self.cwd)

    def check(self, output=None):
        logger.debug("check: checking %s contains hello.py", self.cwd)
        hello_path = os.path.join(self.cwd, "hello.py")
        if not os.path.exists(hello_path):
            return "hello.py was not created."

        logger.debug("check: running hello.py in %s", self.cwd)
        result = subprocess.run(
            [sys.executable, "hello.py"],
            cwd=self.cwd,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            return f"hello.py failed to run:\n{result.stderr.strip()}"

        output = (result.stdout or "").strip()
        logger.debug("check: output from hello.py: %s", output)
        verdict = agent(
            "Does the following output contain a greeting? "
            "Answer only yes or no.\n\n"
            f"{output}"
        )
        if verdict.strip().lower().startswith("yes"):
            return None

        return f"Output did not look like a greeting:\n{output}"


def main():
    logging.basicConfig(level=logging.DEBUG)
    task = HelloWorldTask()
    logger.info("Running task")
    result = task(debug=True)
    logger.info(f"result: {result}")


if __name__ == "__main__":
    main()
